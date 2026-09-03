"""Read-only live qualification of the Go2 decoder/chunker core.

This tool is deliberately not the production bridge.  It subscribes to the
robot's source topic, exercises an explicitly named candidate profile, writes
bounded evidence, and never creates a publisher or calls an AudioHub API.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import resource
import time

from .mic_bridge import Go2MicBridgeCore, MicIngressError
from .profiles import PROFILES, get_profile


class LiveCoreQualificationError(RuntimeError):
    """The live source or decoded stream did not satisfy the candidate gate."""


def percentile(values, q: float) -> float | None:
    items = sorted(float(value) for value in values)
    if not items:
        return None
    return items[min(len(items) - 1, math.ceil(q * len(items)) - 1)]


def evaluate_live_core(
    *,
    duration_s: float,
    source_frames: int,
    accepted_frames: int,
    rejected_frames: int,
    output_chunks: int,
    discontinuities: int,
    payload_lengths: set[int],
    chunk_lengths: set[int],
    time_frame_step: int | None,
    time_frame_step_outliers: int,
    decode_latencies_ms,
    source_to_chunk_latencies_ms,
) -> dict:
    duration_s = float(duration_s)
    expected_chunks = accepted_frames // 5
    decode_p95 = percentile(decode_latencies_ms, 0.95)
    chunk_p95 = percentile(source_to_chunk_latencies_ms, 0.95)
    chunk_p99 = percentile(source_to_chunk_latencies_ms, 0.99)
    chunk_max = max(source_to_chunk_latencies_ms, default=None)
    checks = {
        "duration_at_least_10s": duration_s >= 10.0,
        "source_rate_49_to_51hz": 49.0 <= source_frames / duration_s <= 51.0,
        "all_source_frames_accepted": accepted_frames == source_frames,
        "zero_rejected_frames": rejected_frames == 0,
        "zero_discontinuities": discontinuities == 0,
        "exact_160_byte_source_frames": payload_lengths == {160},
        "exact_3200_byte_output_chunks": chunk_lengths == {3_200},
        "exact_five_to_one_chunking": output_chunks == expected_chunks,
        "single_positive_source_step": time_frame_step is not None and time_frame_step > 0,
        "zero_source_step_outliers": time_frame_step_outliers == 0,
        "decode_p95_under_1ms": decode_p95 is not None and decode_p95 < 1.0,
        # A 100 ms output chunk cannot be complete before five 20 ms source
        # frames arrive.  Allow one additional source period at p99 for the
        # measured scheduler-burst pattern, while retaining a separate 150 ms
        # hard maximum.
        "source_to_chunk_p99_under_120ms": chunk_p99 is not None and chunk_p99 < 120.0,
        "source_to_chunk_max_under_150ms": chunk_max is not None and chunk_max < 150.0,
    }
    return {
        "checks": checks,
        "failures": [name for name, passed in checks.items() if not passed],
        "source_rate_hz": source_frames / duration_s,
        "expected_output_chunks": expected_chunks,
        "time_frame_step_mode": time_frame_step,
        "time_frame_step_outliers": time_frame_step_outliers,
        "decode_ms": {
            "p50": percentile(decode_latencies_ms, 0.50),
            "p95": decode_p95,
            "p99": percentile(decode_latencies_ms, 0.99),
            "max": max(decode_latencies_ms, default=None),
        },
        "source_to_chunk_ms": {
            "p50": percentile(source_to_chunk_latencies_ms, 0.50),
            "p95": chunk_p95,
            "p99": chunk_p99,
            "max": chunk_max,
        },
    }


def _write_new_private(path: str | Path, document: dict) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise LiveCoreQualificationError(
            f"output already exists; refusing overwrite: {output}") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(rendered)


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-s", type=float, default=120.0)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--qos-reliability", choices=("reliable", "best_effort"),
        default="reliable")
    options = parser.parse_args(args)
    if not math.isfinite(options.duration_s) or not 10.0 <= options.duration_s <= 1_800.0:
        parser.error("--duration-s must be finite and in [10, 1800]")
    if os.environ.get("ROS_DOMAIN_ID") != "0":
        parser.error("live Go2 qualification requires ROS_DOMAIN_ID=0")
    if os.environ.get("FEROX_DDS_INTERFACE") != "eth0":
        parser.error("live Go2 qualification requires FEROX_DDS_INTERFACE=eth0")
    if os.environ.get("RMW_IMPLEMENTATION") != "rmw_cyclonedds_cpp":
        parser.error("live Go2 qualification requires rmw_cyclonedds_cpp")
    if Path(options.output).exists() or Path(options.output).is_symlink():
        parser.error("output already exists; refusing overwrite")

    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from unitree_go.msg import AudioData
    except ImportError as exc:  # pragma: no cover - ROS target only
        raise RuntimeError("live Go2 qualification requires ROS Humble interfaces") from exc

    profile = get_profile(options.profile)
    core = Go2MicBridgeCore(profile, max_receive_gap_s=0.1)
    rclpy.init()
    node = Node("go2_audio_live_core_qualification")
    payload_lengths: set[int] = set()
    chunk_lengths: set[int] = set()
    previous_time_frame: int | None = None
    time_frame_step: int | None = None
    time_frame_step_outliers = 0
    source_frames = 0
    rejected_frames = 0
    errors: list[str] = []

    def on_audio(message) -> None:
        nonlocal source_frames, rejected_frames
        nonlocal previous_time_frame, time_frame_step, time_frame_step_outliers
        received_s = time.monotonic()
        payload = bytes(message.data)
        current_time_frame = int(message.time_frame)
        source_frames += 1
        payload_lengths.add(len(payload))
        if previous_time_frame is not None:
            current_step = current_time_frame - previous_time_frame
            if time_frame_step is None:
                time_frame_step = current_step
            elif current_step != time_frame_step:
                time_frame_step_outliers += 1
        previous_time_frame = current_time_frame
        try:
            chunks = core.ingest(
                payload,
                time_frame=current_time_frame,
                receive_steady_s=received_s,
                receive_time_ns=time.monotonic_ns(),
            )
            chunk_lengths.update(len(chunk.payload) for chunk in chunks)
        except MicIngressError as exc:
            rejected_frames += 1
            if len(errors) < 20:
                errors.append(str(exc))

    qos = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=32,
        reliability=(
            ReliabilityPolicy.RELIABLE
            if options.qos_reliability == "reliable"
            else ReliabilityPolicy.BEST_EFFORT),
    )
    started_wall = time.monotonic()
    started_cpu = time.process_time()
    try:
        discovery_deadline = time.monotonic() + 15.0
        while time.monotonic() < discovery_deadline and node.count_publishers(
                "/audiosender") == 0:
            rclpy.spin_once(node, timeout_sec=0.1)
        if node.count_publishers("/audiosender") != 1:
            raise LiveCoreQualificationError(
                "expected exactly one /audiosender publisher")
        node.create_subscription(AudioData, "/audiosender", on_audio, qos)
        started_wall = time.monotonic()
        started_cpu = time.process_time()
        deadline = time.monotonic() + options.duration_s
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        elapsed_s = time.monotonic() - started_wall
        cpu_s = time.process_time() - started_cpu
        node.destroy_node()
        rclpy.shutdown()

    evaluation = evaluate_live_core(
        duration_s=elapsed_s,
        source_frames=source_frames,
        accepted_frames=core.accepted_source_frames,
        rejected_frames=rejected_frames,
        output_chunks=core.output_chunks,
        discontinuities=core.discontinuities,
        payload_lengths=payload_lengths,
        chunk_lengths=chunk_lengths,
        time_frame_step=time_frame_step,
        time_frame_step_outliers=time_frame_step_outliers,
        decode_latencies_ms=list(core.decode_latencies_ms),
        source_to_chunk_latencies_ms=list(core.source_to_chunk_latencies_ms),
    )
    report = {
        "schema_version": 1,
        "evidence_class": "live_go2_decoder_chunker_candidate_only",
        "profile": options.profile,
        "qos_reliability": options.qos_reliability,
        "source_topic": "/audiosender",
        "requested_duration_s": options.duration_s,
        "elapsed_s": elapsed_s,
        "process_cpu_s": cpu_s,
        "process_cpu_percent_of_one_core": 100.0 * cpu_s / elapsed_s,
        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "source_frames": source_frames,
        "accepted_frames": core.accepted_source_frames,
        "rejected_frames": rejected_frames,
        "output_chunks": core.output_chunks,
        "discontinuities": core.discontinuities,
        "payload_lengths": sorted(payload_lengths),
        "chunk_lengths": sorted(chunk_lengths),
        "bounded_decode_window_samples": len(core.decode_latencies_ms),
        "bounded_chunk_window_samples": len(core.source_to_chunk_latencies_ms),
        "errors": errors,
        "evaluation": evaluation,
        "verdict": "PASS" if not evaluation["failures"] else "FAIL",
        "boundary": (
            "Read-only candidate-profile decode/chunk qualification. No ROS "
            "publisher, speaker request, motion/control, or human speech claim."),
    }
    core.close()
    _write_new_private(options.output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["verdict"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover - target entry point
    main()
