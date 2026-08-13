"""One-shot, supervised Go2 speaker qualification probe."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
import wave

from .audiohub_transaction import AudioHubTransaction
from .speaker_protocol import AudioHubPlan, build_audiohub_plan


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROBOT_ID = re.compile(r"^go2_[0-9]{2}$")
_PORTABLE = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}$")


class SpeakerProbeError(ValueError):
    """A speaker probe input or transaction is not safe to execute."""


def canonicalize_probe_wav(
    path: str | Path,
    *,
    max_duration_s: float = 2.0,
) -> tuple[AudioHubPlan, dict]:
    """Validate a bounded PCM WAV and construct its canonical on-wire bytes."""
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise SpeakerProbeError("speaker probe WAV must be a regular non-symlink file")
    if not 0.1 <= float(max_duration_s) <= 2.0 or not math.isfinite(max_duration_s):
        raise SpeakerProbeError("speaker probe duration bound must be in [0.1, 2.0]")
    raw = source.read_bytes()
    if len(raw) > 200_000:
        raise SpeakerProbeError("speaker probe WAV is unexpectedly large")
    try:
        with wave.open(io.BytesIO(raw), "rb") as reader:
            if reader.getcomptype() != "NONE":
                raise SpeakerProbeError("speaker probe WAV must be uncompressed PCM")
            if (
                reader.getframerate(), reader.getnchannels(), reader.getsampwidth()
            ) != (22_050, 1, 2):
                raise SpeakerProbeError(
                    "speaker probe WAV must be 22050 Hz mono S16_LE")
            frame_count = reader.getnframes()
            if not 1 <= frame_count <= int(22_050 * float(max_duration_s)):
                raise SpeakerProbeError("speaker probe WAV exceeds the duration bound")
            pcm = reader.readframes(frame_count)
            if len(pcm) != frame_count * 2:
                raise SpeakerProbeError("speaker probe WAV PCM payload is truncated")
    except (EOFError, wave.Error) as exc:
        raise SpeakerProbeError(f"speaker probe WAV is malformed: {exc}") from exc
    plan = build_audiohub_plan(pcm, stream_id="supervised-speaker-probe")
    on_wire_sha256 = hashlib.sha256(plan.wav_bytes).hexdigest()
    return plan, {
        "input_wav_sha256": hashlib.sha256(raw).hexdigest(),
        "test_wav_sha256": on_wire_sha256,
        "duration_s": round(frame_count / 22_050.0, 6),
        "request_count": len(plan.requests),
    }


def prepare_probe_wav(
    path: str | Path,
    *,
    expected_on_wire_sha256: str,
    max_duration_s: float = 2.0,
) -> tuple[AudioHubPlan, dict]:
    """Bind the exact canonical WAV to an independently reviewed SHA-256."""
    if not _SHA256.fullmatch(str(expected_on_wire_sha256)):
        raise SpeakerProbeError("expected on-wire WAV SHA-256 is invalid")
    plan, metadata = canonicalize_probe_wav(
        path, max_duration_s=max_duration_s)
    on_wire_sha256 = metadata["test_wav_sha256"]
    if on_wire_sha256 != expected_on_wire_sha256:
        raise SpeakerProbeError(
            "canonical on-wire WAV SHA-256 mismatch: "
            f"expected {expected_on_wire_sha256}, got {on_wire_sha256}")
    return plan, metadata


def _preflight_output(path: str | Path) -> Path:
    output = Path(path)
    if output.exists() or output.is_symlink():
        raise SpeakerProbeError(f"output already exists; refusing overwrite: {output}")
    if not output.parent.is_dir() or not os.access(output.parent, os.W_OK):
        raise SpeakerProbeError("speaker probe output directory is not writable")
    return output


def _write_private(path: Path, data: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SpeakerProbeError(f"output appeared during probe: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _write_result(path: Path, result: dict) -> None:
    _write_private(
        path, (json.dumps(result, indent=2, sort_keys=True) + "\n").encode())


def _replace_owned_result(path: Path, result: dict) -> None:
    if not path.is_file() or path.is_symlink():
        raise SpeakerProbeError("speaker probe attempt journal is not a regular file")
    flags = os.O_WRONLY | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write((json.dumps(result, indent=2, sort_keys=True) + "\n").encode())


def prepare_main(args=None) -> None:
    """Offline-only canonicalization step; imports no ROS and publishes nothing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True)
    parser.add_argument("--output", required=True)
    options = parser.parse_args(args)
    try:
        output = _preflight_output(options.output)
        plan, metadata = canonicalize_probe_wav(options.wav)
        _write_private(output, plan.wav_bytes)
    except SpeakerProbeError as exc:
        parser.error(str(exc))

    print(json.dumps({
        "output": str(output),
        **metadata,
    }, sort_keys=True))


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True)
    parser.add_argument("--expected-on-wire-sha256", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--robot-id", required=True)
    parser.add_argument("--runtime-firmware", required=True)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--confirm-supervised-safe-volume", action="store_true")
    parser.add_argument("--response-timeout-s", type=float, default=2.0)
    options = parser.parse_args(args)
    if not _ROBOT_ID.fullmatch(options.robot_id):
        parser.error("--robot-id must look like go2_02")
    if not _PORTABLE.fullmatch(options.runtime_firmware):
        parser.error("--runtime-firmware must be an explicit portable fingerprint")
    if not _PORTABLE.fullmatch(options.operator_id):
        parser.error("--operator-id is invalid")
    if not options.confirm_supervised_safe_volume:
        parser.error("--confirm-supervised-safe-volume is required for hardware output")
    if os.environ.get("ROS_DOMAIN_ID") != "0":
        parser.error("speaker qualification requires ROS_DOMAIN_ID=0")
    if os.environ.get("RMW_IMPLEMENTATION") != "rmw_cyclonedds_cpp":
        parser.error("speaker qualification requires rmw_cyclonedds_cpp")
    if not os.environ.get("FEROX_DDS_INTERFACE"):
        parser.error("speaker qualification requires an explicit FEROX_DDS_INTERFACE")
    if not os.environ.get("CYCLONEDDS_URI"):
        parser.error("speaker qualification requires a rendered CYCLONEDDS_URI")
    try:
        output = _preflight_output(options.output)
        plan, metadata = prepare_probe_wav(
            options.wav,
            expected_on_wire_sha256=options.expected_on_wire_sha256,
        )
    except SpeakerProbeError as exc:
        parser.error(str(exc))

    attempt = {
        "schema_version": 1,
        "status": "started_not_authorizing",
        "robot_id": options.robot_id,
        "runtime_firmware": options.runtime_firmware,
        "operator_id": options.operator_id,
        "test_wav_sha256": metadata["test_wav_sha256"],
        "started_utc": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"),
        "hardware_publish_started": False,
    }
    _write_result(output, attempt)

    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy
        from unitree_api.msg import Request, Response
    except ImportError as exc:  # pragma: no cover - ROS image only
        raise RuntimeError("Go2 speaker probe requires ROS Humble unitree_api") from exc

    rclpy.init()
    node = Node("go2_audio_supervised_speaker_probe")
    request_topic = "/api/audiohub/request"
    response_topic = "/api/audiohub/response"
    qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
    publisher = node.create_publisher(Request, request_topic, qos)
    transaction = AudioHubTransaction(
        timeout_s=options.response_timeout_s,
        identity_seed=secrets.randbelow(2**31),
    )

    def on_response(message) -> None:
        transaction.acknowledge(
            identity=int(message.header.identity.id),
            api_id=int(message.header.identity.api_id),
            status=int(message.header.status.code),
        )

    subscription = node.create_subscription(
        Response, response_topic, on_response, qos)
    try:
        discovery_deadline = time.monotonic() + 5.0
        while rclpy.ok() and (
            publisher.get_subscription_count() < 1
            or subscription.get_publisher_count() < 1
        ):
            if time.monotonic() >= discovery_deadline:
                raise RuntimeError(
                    "Go2 audiohub request subscriber/response publisher pair not discovered")
            rclpy.spin_once(node, timeout_sec=0.1)
        attempt["hardware_publish_started"] = True
        _replace_owned_result(output, attempt)
        transaction.submit(plan)
        while rclpy.ok() and transaction.busy and not transaction.latched_fault:
            now = time.monotonic()
            transaction.check_timeout(now)
            pending = transaction.dispatch_next(now)
            if pending is not None:
                request = Request()
                request.header.identity.id = pending.identity
                request.header.identity.api_id = pending.api_id
                request.parameter = pending.parameter
                request.binary = []
                publisher.publish(request)
            rclpy.spin_once(node, timeout_sec=0.01)
        if transaction.latched_fault:
            raise RuntimeError(transaction.latched_fault)
        if transaction.completed_total != 1:
            raise RuntimeError("speaker probe did not complete exactly one upload")
        # Deliberately keep observing before the human signs off no delayed replay.
        post_deadline = time.monotonic() + 10.0
        while rclpy.ok() and time.monotonic() < post_deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        result = {
            "probe_metadata": {
                "schema_version": 1,
                "robot_id": options.robot_id,
                "runtime_firmware": options.runtime_firmware,
                "completed_utc": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"),
                "input_wav_sha256": metadata["input_wav_sha256"],
                "duration_s": metadata["duration_s"],
                "request_count": metadata["request_count"],
                "responses_ok": transaction.responses_ok_total,
                "post_playback_observation_s": 10.0,
            },
            "speaker_probe": {
                "protocol": "audiohub_v1",
                "start_api_id": 4001,
                "block_api_id": 4003,
                "request_topic": request_topic,
                "response_topic": response_topic,
                "api_errors": 0,
                "operator_heard_test_phrase": False,
                "operator_confirmed_no_repeat": False,
                "operator_confirmed_no_truncation": False,
                "operator_confirmed_no_delayed_replay_10s": False,
                "operator_id": options.operator_id,
                "test_wav_sha256": metadata["test_wav_sha256"],
                "sample_rate": 22_050,
                "channels": 1,
                "sample_width": 2,
            },
        }
        _replace_owned_result(output, result)
        print(json.dumps(result, sort_keys=True))
        print("Operator confirmation fields remain false; review and edit only after listening.")
    except Exception as exc:
        attempt.update({
            "status": "failed_not_authorizing",
            "failed_utc": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"),
            "failure": " ".join(str(exc).split())[:256],
        })
        _replace_owned_result(output, attempt)
        raise
    finally:
        node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
