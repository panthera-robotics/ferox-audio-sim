"""Read-only capture and summary of the Go2 ``/audiosender`` wire shape."""
from __future__ import annotations

import argparse
from collections import Counter
import base64
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time
from typing import Iterable


class DiscoveryProbeError(ValueError):
    """The read-only observation cannot support a hardware profile decision."""


@dataclass(frozen=True)
class ObservedFrame:
    receive_steady_s: float
    time_frame: int
    payload: bytes


def _frame_capture_bytes(frames: Iterable[ObservedFrame]) -> bytes:
    lines = []
    for frame in frames:
        lines.append(json.dumps({
            "receive_steady_s": round(float(frame.receive_steady_s), 9),
            "time_frame": int(frame.time_frame),
            "payload_b64": base64.b64encode(bytes(frame.payload)).decode("ascii"),
        }, separators=(",", ":"), sort_keys=True))
    if not lines:
        raise DiscoveryProbeError("cannot serialize an empty Go2 audio capture")
    return ("\n".join(lines) + "\n").encode()


def summarize_frames(
    frames: Iterable[ObservedFrame],
    *,
    duration_s: float,
    subscriber_reliability: str = "reliable",
) -> dict:
    items = list(frames)
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise DiscoveryProbeError("duration_s must be finite and positive")
    if subscriber_reliability not in {"best_effort", "reliable"}:
        raise DiscoveryProbeError("subscriber_reliability is invalid")
    if not items:
        raise DiscoveryProbeError("no Go2 audio frames were observed")
    receive = [float(item.receive_steady_s) for item in items]
    source = [int(item.time_frame) for item in items]
    if any(not math.isfinite(value) or value < 0 for value in receive):
        raise DiscoveryProbeError("a receive timestamp is invalid")
    if any(value < 0 for value in source):
        raise DiscoveryProbeError("a source timestamp is invalid")
    intervals = [
        (right - left) * 1000.0 for left, right in zip(receive, receive[1:])]
    if any(value < 0 for value in intervals):
        raise DiscoveryProbeError("receive time moved backwards")
    sizes = [len(item.payload) for item in items]
    mode_bytes = Counter(sizes).most_common(1)[0][0]
    nonempty = sum(size > 0 for size in sizes)
    payload_digest = hashlib.sha256()
    for item in items:
        payload_digest.update(len(item.payload).to_bytes(4, "little"))
        payload_digest.update(item.payload)
    p95 = 0.0
    p99 = 0.0
    if intervals:
        ordered = sorted(intervals)
        p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
        p99 = ordered[max(0, math.ceil(0.99 * len(ordered)) - 1)]
    source_steps = [right - left for left, right in zip(source, source[1:])]
    positive_source_steps = [value for value in source_steps if value > 0]
    source_step_mode = (
        Counter(positive_source_steps).most_common(1)[0][0]
        if positive_source_steps else None
    )
    source_step_outliers = (
        sum(value != source_step_mode for value in source_steps)
        if source_step_mode is not None else len(source_steps)
    )
    expected_interval_ms = (
        1000.0 * duration_s / len(items) if items else 0.0)
    receive_gap_count = sum(
        value > expected_interval_ms * 2.0 for value in intervals)
    receive_burst_count = sum(
        value < expected_interval_ms * 0.5 for value in intervals)
    return {
        "schema_version": 2,
        "source_topic": "/audiosender",
        "source_type": "unitree_go/msg/AudioData",
        "subscriber_reliability": subscriber_reliability,
        "duration_s": round(float(duration_s), 6),
        "frame_count": len(items),
        "effective_rate_hz": round(len(items) / duration_s, 6),
        "nonempty_ratio": round(nonempty / len(items), 9),
        "payload_bytes_mode": mode_bytes,
        "payload_bytes_min": min(sizes),
        "payload_bytes_max": max(sizes),
        "interval_p50_ms": round(statistics.median(intervals), 6) if intervals else 0.0,
        "interval_p95_ms": round(p95, 6),
        "interval_p99_ms": round(p99, 6),
        "interval_max_ms": round(max(intervals), 6) if intervals else 0.0,
        "receive_gap_count": receive_gap_count,
        "receive_gap_fraction": round(
            receive_gap_count / max(1, len(intervals)), 9),
        "receive_burst_count": receive_burst_count,
        "receive_burst_fraction": round(
            receive_burst_count / max(1, len(intervals)), 9),
        "time_frame_monotonic": all(right > left for left, right in zip(source, source[1:])),
        "time_frame_step_mode": source_step_mode,
        "time_frame_step_outlier_count": source_step_outliers,
        "framed_payload_sha256": payload_digest.hexdigest(),
        "capture_sha256": hashlib.sha256(_frame_capture_bytes(items)).hexdigest(),
        "interpretation": "none",
    }


def _write_new_private(path: str | Path, data: bytes) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise DiscoveryProbeError(
            f"output already exists; refusing overwrite: {output}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except Exception:
        try:
            output.unlink()
        except OSError:
            pass
        raise


def _require_distinct_new_outputs(*paths: str | Path) -> tuple[Path, ...]:
    outputs = tuple(Path(path) for path in paths)
    if len({str(path.absolute()) for path in outputs}) != len(outputs):
        raise DiscoveryProbeError("capture outputs must be distinct paths")
    existing = [str(path) for path in outputs if path.exists() or path.is_symlink()]
    if existing:
        raise DiscoveryProbeError(
            "output already exists; refusing partial capture: " + ", ".join(existing))
    return outputs


def write_observation(path: str | Path, observation: dict) -> None:
    _write_new_private(
        path, (json.dumps(observation, indent=2, sort_keys=True) + "\n").encode())


def write_frame_capture(path: str | Path, frames: Iterable[ObservedFrame]) -> None:
    """Write newline-delimited source frames without assigning a codec."""
    _write_new_private(path, _frame_capture_bytes(frames))


def write_capture_bundle(
    observation_path: str | Path,
    frames_path: str | Path,
    observation: dict,
    frames: Iterable[ObservedFrame],
) -> None:
    """Create both discovery artifacts or roll back the artifact created first."""
    observation_output, frames_output = _require_distinct_new_outputs(
        observation_path, frames_path)
    items = list(frames)
    created: list[Path] = []
    try:
        write_frame_capture(frames_output, items)
        created.append(frames_output)
        write_observation(observation_output, observation)
        created.append(observation_output)
    except Exception:
        for output in created:
            try:
                output.unlink()
            except OSError:
                pass
        raise


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-s", type=float, default=15.0)
    parser.add_argument("--output", required=True)
    parser.add_argument("--frames-output", required=True)
    parser.add_argument(
        "--qos-reliability",
        choices=("best_effort", "reliable"),
        default="reliable",
    )
    options = parser.parse_args(args)
    if not 5.0 <= options.duration_s <= 120.0 or not math.isfinite(options.duration_s):
        parser.error("--duration-s must be finite and in [5, 120]")
    try:
        _require_distinct_new_outputs(options.output, options.frames_output)
    except DiscoveryProbeError as exc:
        parser.error(str(exc))
    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from unitree_go.msg import AudioData
    except ImportError as exc:  # pragma: no cover - ROS image only
        raise RuntimeError("Go2 audio discovery requires ROS Humble unitree_go") from exc
    rclpy.init()
    node = Node("go2_audio_readonly_discovery")
    frames: list[ObservedFrame] = []

    def on_audio(message) -> None:
        frames.append(ObservedFrame(
            receive_steady_s=time.monotonic(),
            time_frame=int(message.time_frame),
            payload=bytes(message.data),
        ))

    audio_qos = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=32,
        reliability=(
            ReliabilityPolicy.RELIABLE
            if options.qos_reliability == "reliable"
            else ReliabilityPolicy.BEST_EFFORT
        ),
    )
    node.create_subscription(AudioData, "/audiosender", on_audio, audio_qos)
    started = time.monotonic()
    try:
        while rclpy.ok() and time.monotonic() - started < options.duration_s:
            rclpy.spin_once(node, timeout_sec=0.1)
        elapsed = time.monotonic() - started
        observation = summarize_frames(
            frames,
            duration_s=elapsed,
            subscriber_reliability=options.qos_reliability,
        )
        write_capture_bundle(options.output, options.frames_output, observation, frames)
        print(json.dumps(observation, sort_keys=True))
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":  # pragma: no cover - exercised on the ROS target
    main()
