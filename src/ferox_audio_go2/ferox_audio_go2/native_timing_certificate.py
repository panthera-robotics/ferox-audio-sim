"""Certify an rclcpp-native, read-only Go2 ``/audiosender`` capture.

The native probe records middleware timestamps that rclpy does not expose in
the existing collector.  This certificate binds those records to the raw frame
capture, but deliberately does not reinterpret receive cadence as end-to-end
latency or authorize the speaker, AudioHub, or robot control.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import statistics
from collections.abc import Mapping
from pathlib import Path

from .discovery_probe import ObservedFrame, summarize_frames


POLICY_ID = "ferox-go2-audio-native-timing-v1"
_FRAME_KEYS = {"payload_b64", "receive_steady_s", "time_frame"}
_HEADER_KEYS = {
    "capture_start_steady_ns", "capture_start_system_ns", "collector",
    "host_boot_id", "publisher_created", "qos_reliability", "record_type",
    "requested_duration_s", "schema_version", "source_topic",
    "speaker_or_audiohub_expected", "supervised_speaker_capture_token",
}
_METADATA_FRAME_KEYS = {
    "callback_steady_ns", "callback_system_ns", "from_intra_process",
    "publisher_gid_hex", "record_type", "rmw_received_timestamp_ns",
    "rmw_source_timestamp_ns", "time_frame",
}
_TRAILER_KEYS = {
    "capture_end_steady_ns", "capture_end_system_ns", "elapsed_s",
    "frame_count", "record_type", "speaker_or_audiohub_called",
}
_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class NativeTimingCertificateError(ValueError):
    """Native frame or middleware timing evidence is malformed."""


def _regular_bytes(path: str | Path, *, maximum_bytes: int) -> tuple[Path, bytes]:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise NativeTimingCertificateError(
            f"evidence must be a regular non-symlink file: {source}")
    payload = source.read_bytes()
    if not payload or len(payload) > maximum_bytes:
        raise NativeTimingCertificateError(f"evidence size is invalid: {source}")
    return source, payload


def _json_lines(payload: bytes, *, label: str) -> list[dict[str, object]]:
    documents: list[dict[str, object]] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line:
            raise NativeTimingCertificateError(
                f"blank {label} line at line {line_number}")
        try:
            document = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NativeTimingCertificateError(
                f"invalid {label} JSON at line {line_number}") from exc
        if not isinstance(document, dict):
            raise NativeTimingCertificateError(
                f"{label} line {line_number} must be an object")
        documents.append(document)
    return documents


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise NativeTimingCertificateError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: object, *, label: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeTimingCertificateError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise NativeTimingCertificateError(f"{label} must be finite and >= {minimum}")
    return result


def _load_frames(path: str | Path) -> tuple[list[ObservedFrame], dict[str, object]]:
    source, payload = _regular_bytes(path, maximum_bytes=100_000_000)
    documents = _json_lines(payload, label="frame capture")
    frames: list[ObservedFrame] = []
    for line_number, document in enumerate(documents, start=1):
        if set(document) != _FRAME_KEYS:
            raise NativeTimingCertificateError(
                f"frame capture line {line_number} has an invalid schema")
        receive = _number(
            document["receive_steady_s"], label="receive_steady_s")
        time_frame = _integer(document["time_frame"], label="time_frame")
        try:
            frame_payload = base64.b64decode(document["payload_b64"], validate=True)
        except (TypeError, ValueError, binascii.Error) as exc:
            raise NativeTimingCertificateError(
                f"invalid payload at frame line {line_number}") from exc
        if len(frame_payload) != 160:
            raise NativeTimingCertificateError(
                f"frame payload is not 160 bytes at line {line_number}")
        frames.append(ObservedFrame(receive, time_frame, frame_payload))
    if len(frames) < 2:
        raise NativeTimingCertificateError("at least two native frames are required")
    return frames, {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "frame_count": len(frames),
    }


def _load_metadata(
    path: str | Path,
) -> tuple[dict[str, object], list[dict[str, object]], dict[str, object], dict[str, object]]:
    source, payload = _regular_bytes(path, maximum_bytes=100_000_000)
    documents = _json_lines(payload, label="native metadata")
    if len(documents) < 4:
        raise NativeTimingCertificateError(
            "native metadata requires a header, at least two frames, and a trailer")
    header, *middle, trailer = documents
    if set(header) != _HEADER_KEYS or header.get("record_type") != "capture_start":
        raise NativeTimingCertificateError("native metadata header schema is invalid")
    if set(trailer) != _TRAILER_KEYS or trailer.get("record_type") != "capture_end":
        raise NativeTimingCertificateError("native metadata trailer schema is invalid")
    for line_number, document in enumerate(middle, start=2):
        if (
            set(document) != _METADATA_FRAME_KEYS
            or document.get("record_type") != "frame"
        ):
            raise NativeTimingCertificateError(
                f"native frame metadata schema is invalid at line {line_number}")
    return header, middle, trailer, {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "frame_record_count": len(middle),
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _interval_metrics(values_ns: list[int]) -> dict[str, object]:
    positive = [value for value in values_ns if value > 0]
    intervals = [
        (right - left) / 1_000_000.0
        for left, right in zip(positive, positive[1:])
    ]
    valid = [value for value in intervals if value >= 0.0]
    return {
        "nonzero_count": len(positive),
        "nonzero_fraction": len(positive) / max(1, len(values_ns)),
        "backwards_count": sum(value < 0.0 for value in intervals),
        "interval_p50_ms": statistics.median(valid) if valid else None,
        "interval_p95_ms": _percentile(valid, 0.95),
        "interval_p99_ms": _percentile(valid, 0.99),
        "interval_max_ms": max(valid) if valid else None,
    }


def certify_native_timing(
    *,
    frames: list[ObservedFrame],
    frame_binding: Mapping[str, object],
    header: Mapping[str, object],
    metadata_rows: list[Mapping[str, object]],
    trailer: Mapping[str, object],
    metadata_binding: Mapping[str, object],
) -> dict[str, object]:
    if header.get("schema_version") != 1:
        raise NativeTimingCertificateError("native metadata schema_version must be 1")
    if (
        header.get("collector") != "rclcpp_native"
        or header.get("source_topic") != "/audiosender"
        or header.get("publisher_created") is not False
    ):
        raise NativeTimingCertificateError("native collector safety identity is invalid")
    reliability = header.get("qos_reliability")
    if reliability not in {"reliable", "best_effort"}:
        raise NativeTimingCertificateError("native collector reliability is invalid")
    requested_duration_s = _number(
        header.get("requested_duration_s"), label="requested_duration_s")
    if not 5.0 <= requested_duration_s <= 120.0:
        raise NativeTimingCertificateError("requested duration is outside [5, 120]")
    boot_id = header.get("host_boot_id")
    if not isinstance(boot_id, str) or _BOOT_ID.fullmatch(boot_id) is None:
        raise NativeTimingCertificateError("native host boot ID is invalid")
    if (
        header.get("speaker_or_audiohub_expected") is not False
        or header.get("supervised_speaker_capture_token") is not None
        or trailer.get("speaker_or_audiohub_called") is not False
    ):
        raise NativeTimingCertificateError("native collector safety boundary is invalid")
    if _integer(trailer.get("frame_count"), label="frame_count") != len(frames):
        raise NativeTimingCertificateError("native trailer frame count mismatch")
    if len(metadata_rows) != len(frames):
        raise NativeTimingCertificateError("native frame and metadata counts differ")

    start_steady = _integer(
        header.get("capture_start_steady_ns"), label="capture_start_steady_ns")
    end_steady = _integer(
        trailer.get("capture_end_steady_ns"), label="capture_end_steady_ns")
    start_system = _integer(
        header.get("capture_start_system_ns"), label="capture_start_system_ns")
    end_system = _integer(
        trailer.get("capture_end_system_ns"), label="capture_end_system_ns")
    elapsed_s = _number(trailer.get("elapsed_s"), label="elapsed_s")
    if end_steady <= start_steady or end_system <= start_system:
        raise NativeTimingCertificateError("native capture clocks did not advance")
    calculated_elapsed_s = (end_steady - start_steady) / 1_000_000_000.0
    if abs(calculated_elapsed_s - elapsed_s) > 1e-6:
        raise NativeTimingCertificateError("native capture elapsed time mismatch")

    callback_steady: list[int] = []
    callback_system: list[int] = []
    rmw_source: list[int] = []
    rmw_received: list[int] = []
    publisher_gids: set[str] = set()
    intra_process_count = 0
    for index, (frame, row) in enumerate(zip(frames, metadata_rows), start=1):
        if _integer(row.get("time_frame"), label="metadata time_frame") != frame.time_frame:
            raise NativeTimingCertificateError(
                f"native frame/metadata source timestamp mismatch at frame {index}")
        steady = _integer(
            row.get("callback_steady_ns"), label="callback_steady_ns")
        system = _integer(
            row.get("callback_system_ns"), label="callback_system_ns")
        if abs(round(frame.receive_steady_s * 1_000_000_000.0) - steady) > 1:
            raise NativeTimingCertificateError(
                f"native frame/metadata callback timestamp mismatch at frame {index}")
        gid = str(row.get("publisher_gid_hex") or "").lower()
        if len(gid) != 48 or any(character not in "0123456789abcdef" for character in gid):
            raise NativeTimingCertificateError(
                f"invalid publisher GID at native frame {index}")
        if not isinstance(row.get("from_intra_process"), bool):
            raise NativeTimingCertificateError(
                f"invalid intra-process flag at native frame {index}")
        callback_steady.append(steady)
        callback_system.append(system)
        rmw_source.append(_integer(
            row.get("rmw_source_timestamp_ns"), label="rmw_source_timestamp_ns"))
        rmw_received.append(_integer(
            row.get("rmw_received_timestamp_ns"), label="rmw_received_timestamp_ns"))
        publisher_gids.add(gid)
        intra_process_count += int(row["from_intra_process"] is True)

    if any(right < left for left, right in zip(callback_steady, callback_steady[1:])):
        raise NativeTimingCertificateError("native callback steady clock moved backwards")
    if any(right < left for left, right in zip(callback_system, callback_system[1:])):
        raise NativeTimingCertificateError("native callback system clock moved backwards")
    if not (
        start_steady <= callback_steady[0] <= callback_steady[-1] <= end_steady
        and start_system <= callback_system[0] <= callback_system[-1] <= end_system
    ):
        raise NativeTimingCertificateError("native callback timestamps escape capture bounds")

    observation = summarize_frames(
        frames, duration_s=elapsed_s, subscriber_reliability=str(reliability))
    semantic_capture_sha256 = observation["capture_sha256"]
    observation["capture_sha256"] = frame_binding.get("sha256")

    dispatch_delay_ms = [
        (callback - received) / 1_000_000.0
        for callback, received in zip(callback_system, rmw_received)
        if received > 0 and 0 <= callback - received <= 10_000_000_000
    ]
    gid_digest = hashlib.sha256(
        "\n".join(sorted(publisher_gids)).encode()).hexdigest()
    observation.update({
        "collector_implementation": "rclcpp_native",
        "native_timing_policy_id": POLICY_ID,
        "native_timing_policy_source_sha256": hashlib.sha256(
            Path(__file__).read_bytes()).hexdigest(),
        "native_timing_inputs": {
            "frames": dict(frame_binding),
            "metadata": dict(metadata_binding),
        },
        "native_timing": {
            "requested_duration_s": requested_duration_s,
            "capture_elapsed_s": elapsed_s,
            "callback_steady": _interval_metrics(callback_steady),
            "rmw_source": _interval_metrics(rmw_source),
            "rmw_received": _interval_metrics(rmw_received),
            "rmw_received_to_callback_system_valid_fraction": (
                len(dispatch_delay_ms) / len(frames)
            ),
            "rmw_received_to_callback_system_p50_ms": (
                statistics.median(dispatch_delay_ms) if dispatch_delay_ms else None
            ),
            "rmw_received_to_callback_system_p95_ms": (
                _percentile(dispatch_delay_ms, 0.95)
            ),
            "publisher_gid_distinct_count": len(publisher_gids),
            "publisher_gid_set_sha256": gid_digest,
            "from_intra_process_count": intra_process_count,
            "semantic_frame_capture_sha256": semantic_capture_sha256,
        },
        "native_evidence_integrity_passed": True,
        "one_way_latency_measured": False,
        "absolute_latency_gate_passed": False,
        "production_ready": False,
        "mic_enable_authorized": False,
        "speaker_enable_authorized": False,
        "control_authorized": False,
        "interpretation": (
            "RMW source/receive and callback timestamps are retained as separate "
            "observables. Their exact implementation points are middleware-specific; "
            "without a declared publisher clock domain and synchronization they do not "
            "establish absolute robot-to-subscriber latency."
        ),
    })
    return observation


def _write_new_private(path: str | Path, document: Mapping[str, object]) -> None:
    output = Path(path)
    if output.exists() or output.is_symlink():
        raise NativeTimingCertificateError(
            f"output already exists; refusing overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write((json.dumps(
                document, indent=2, sort_keys=True) + "\n").encode())
    except Exception:
        try:
            output.unlink()
        except OSError:
            pass
        raise


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--output", required=True)
    options = parser.parse_args(args)
    frames, frame_binding = _load_frames(options.frames)
    header, rows, trailer, metadata_binding = _load_metadata(options.metadata)
    certificate = certify_native_timing(
        frames=frames,
        frame_binding=frame_binding,
        header=header,
        metadata_rows=rows,
        trailer=trailer,
        metadata_binding=metadata_binding,
    )
    _write_new_private(options.output, certificate)
    print(json.dumps(certificate, indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    main()
