"""Frame-level diagnosis for the Go2 ``/audiosender`` timing envelope.

This module deliberately separates receive cadence from one-way latency.  The
Unitree ``AudioData`` contract has no clock-domain declaration, so subtracting
``time_frame`` from a host monotonic clock would fabricate an absolute latency.
Two simultaneous, independent subscribers can still localize batching to a
point upstream of their ROS callbacks.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path


POLICY_ID = "ferox-go2-audio-strict-timing-v1"
STRICT_RECEIVE_INTERVAL_P95_MS = 40.0
EXPECTED_SOURCE_STEP = 200_000
EXPECTED_SOURCE_INTERVAL_MS = 20.0


class StrictTimingError(ValueError):
    """Raw timing evidence is malformed or cannot support the diagnosis."""


def _number_at_least(value: object, minimum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= minimum
    )


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise StrictTimingError("a timing percentile requires at least one sample")
    ordered = sorted(values)
    index = max(0, math.ceil(float(fraction) * len(ordered)) - 1)
    return ordered[index]


def _regular_file(path: str | Path, *, maximum_bytes: int) -> tuple[Path, bytes]:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise StrictTimingError(f"evidence must be a regular non-symlink file: {source}")
    payload = source.read_bytes()
    if not payload or len(payload) > maximum_bytes:
        raise StrictTimingError(f"evidence size is invalid: {source}")
    return source, payload


def _json_document(path: str | Path) -> tuple[dict[str, object], dict[str, object]]:
    source, payload = _regular_file(path, maximum_bytes=10_000_000)
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrictTimingError(f"invalid JSON evidence: {source}") from exc
    if isinstance(document, list):
        if len(document) != 1 or not isinstance(document[0], dict):
            raise StrictTimingError(
                f"JSON evidence array must contain exactly one object: {source}")
        document = document[0]
    if not isinstance(document, dict):
        raise StrictTimingError(f"JSON evidence must be an object: {source}")
    return document, {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _load_capture(path: str | Path) -> tuple[list[dict[str, object]], dict[str, object]]:
    source, payload = _regular_file(path, maximum_bytes=100_000_000)
    rows: list[dict[str, object]] = []
    payload_digest = hashlib.sha256()
    previous_receive: float | None = None
    previous_source: int | None = None
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line:
            raise StrictTimingError(f"blank capture line {line_number}: {source}")
        try:
            row = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StrictTimingError(
                f"invalid capture JSON at line {line_number}: {source}") from exc
        if not isinstance(row, dict) or set(row) != {
            "payload_b64", "receive_steady_s", "time_frame"
        }:
            raise StrictTimingError(
                f"capture line {line_number} has an invalid schema: {source}")
        receive = row["receive_steady_s"]
        time_frame = row["time_frame"]
        if (
            isinstance(receive, bool)
            or not isinstance(receive, (int, float))
            or not math.isfinite(float(receive))
            or float(receive) < 0.0
        ):
            raise StrictTimingError(f"invalid receive time at line {line_number}: {source}")
        if isinstance(time_frame, bool) or not isinstance(time_frame, int) or time_frame < 0:
            raise StrictTimingError(f"invalid source time at line {line_number}: {source}")
        try:
            frame_payload = base64.b64decode(row["payload_b64"], validate=True)
        except (TypeError, ValueError, binascii.Error) as exc:
            raise StrictTimingError(
                f"invalid payload at line {line_number}: {source}") from exc
        if len(frame_payload) != 160:
            raise StrictTimingError(
                f"capture payload is not 160 bytes at line {line_number}: {source}")
        if previous_receive is not None and float(receive) < previous_receive:
            raise StrictTimingError(f"receive clock moved backwards: {source}")
        if previous_source is not None and time_frame <= previous_source:
            raise StrictTimingError(f"source clock did not increase: {source}")
        payload_digest.update(len(frame_payload).to_bytes(4, "little"))
        payload_digest.update(frame_payload)
        rows.append({
            "receive_steady_s": float(receive),
            "time_frame": time_frame,
            "payload_sha256": hashlib.sha256(frame_payload).hexdigest(),
        })
        previous_receive = float(receive)
        previous_source = time_frame
    if len(rows) < 2:
        raise StrictTimingError(f"capture must contain at least two frames: {source}")
    return rows, {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "frame_count": len(rows),
        "framed_payload_sha256": payload_digest.hexdigest(),
    }


def _lane_metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    receive = [float(row["receive_steady_s"]) for row in rows]
    source = [int(row["time_frame"]) for row in rows]
    intervals = [1000.0 * (right - left) for left, right in zip(receive, receive[1:])]
    source_steps = [right - left for left, right in zip(source, source[1:])]
    source_elapsed_ms = [
        (value - source[0]) * EXPECTED_SOURCE_INTERVAL_MS / EXPECTED_SOURCE_STEP
        for value in source
    ]
    receive_elapsed_ms = [1000.0 * (value - receive[0]) for value in receive]
    mean_source = sum(source_elapsed_ms) / len(source_elapsed_ms)
    mean_receive = sum(receive_elapsed_ms) / len(receive_elapsed_ms)
    denominator = sum((value - mean_source) ** 2 for value in source_elapsed_ms)
    if denominator <= 0.0:
        raise StrictTimingError("source clock span is zero")
    slope = sum(
        (left - mean_source) * (right - mean_receive)
        for left, right in zip(source_elapsed_ms, receive_elapsed_ms)
    ) / denominator
    intercept = mean_receive - slope * mean_source
    residuals = [
        measured - (intercept + slope * expected)
        for expected, measured in zip(source_elapsed_ms, receive_elapsed_ms)
    ]
    classes = [
        "burst" if value < EXPECTED_SOURCE_INTERVAL_MS * 0.5
        else "stall" if value > EXPECTED_SOURCE_INTERVAL_MS * 1.5
        else "paced"
        for value in intervals
    ]
    return {
        "frame_count": len(rows),
        "first_time_frame": source[0],
        "last_time_frame": source[-1],
        "source_step_mode": max(set(source_steps), key=source_steps.count),
        "source_step_outlier_count": sum(value != EXPECTED_SOURCE_STEP for value in source_steps),
        "receive_interval_p50_ms": _percentile(intervals, 0.50),
        "receive_interval_p95_ms": _percentile(intervals, 0.95),
        "receive_interval_p99_ms": _percentile(intervals, 0.99),
        "receive_interval_max_ms": max(intervals),
        "receive_burst_count": classes.count("burst"),
        "receive_stall_count": classes.count("stall"),
        "receive_paced_count": classes.count("paced"),
        "clock_rate_error_ppm": (slope - 1.0) * 1_000_000.0,
        "relative_queueing_p05_p95_span_ms": (
            _percentile(residuals, 0.95) - _percentile(residuals, 0.05)
        ),
        "relative_queueing_full_span_ms": max(residuals) - min(residuals),
        "classes": classes,
        "intervals": intervals,
        "source_timeline": source,
    }


def _container_identity(
    document: Mapping[str, object], *, expected_reliability: str
) -> dict[str, object]:
    container_id = str(document.get("Id") or "").strip().lower()
    name = str(document.get("Name") or "").strip()
    host_config = document.get("HostConfig")
    config = document.get("Config")
    state = document.get("State")
    if (
        len(container_id) != 64
        or any(character not in "0123456789abcdef" for character in container_id)
        or not name.startswith("/")
        or not isinstance(host_config, Mapping)
        or not isinstance(config, Mapping)
        or not isinstance(state, Mapping)
    ):
        raise StrictTimingError("container inspect evidence is missing identity fields")
    command_parts = config.get("Cmd")
    if not (
        isinstance(command_parts, list)
        and all(isinstance(value, str) for value in command_parts)
    ):
        raise StrictTimingError("container inspect evidence is missing command fields")
    command = " ".join(command_parts)
    collector_implementation = (
        "rclcpp_native" if "go2_audio_native_timing_probe" in command
        else "rclpy" if "go2_audio_readonly_discovery" in command
        else "unknown"
    )
    cap_drop = host_config.get("CapDrop")
    security_options = host_config.get("SecurityOpt")
    return {
        "id": container_id,
        "name": name,
        "image": str(config.get("Image") or ""),
        "network_mode": host_config.get("NetworkMode"),
        "read_only_root": host_config.get("ReadonlyRootfs") is True,
        "all_capabilities_dropped": (
            isinstance(cap_drop, list) and "ALL" in cap_drop
        ),
        "no_new_privileges": (
            isinstance(security_options, list)
            and any(
                str(value).strip().lower() in {
                    "no-new-privileges",
                    "no-new-privileges:true",
                    "no-new-privileges=true",
                }
                for value in security_options
            )
        ),
        "clean_exit": (
            state.get("ExitCode") == 0 and state.get("OOMKilled") is False
        ),
        "collector_implementation": collector_implementation,
        "readonly_collector_command": (
            collector_implementation in {"rclpy", "rclcpp_native"}
            and f"--qos-reliability {expected_reliability}" in command
            and "--frames-output " in command
            and (
                collector_implementation == "rclpy"
                or "--metadata-output " in command
            )
            and all(forbidden not in command for forbidden in (
                "speaker_probe", "audiohub/request", "audioreceiver",
            ))
        ),
    }


def evaluate_strict_timing(
    *,
    reliable_rows: list[dict[str, object]],
    best_effort_rows: list[dict[str, object]],
    reliable_capture: Mapping[str, object],
    best_effort_capture: Mapping[str, object],
    reliable_observation: Mapping[str, object],
    best_effort_observation: Mapping[str, object],
    reliable_container: Mapping[str, object],
    best_effort_container: Mapping[str, object],
    inputs: Mapping[str, object] | None = None,
) -> dict[str, object]:
    reliable = _lane_metrics(reliable_rows)
    best_effort = _lane_metrics(best_effort_rows)
    reliable_identity = _container_identity(
        reliable_container, expected_reliability="reliable")
    best_effort_identity = _container_identity(
        best_effort_container, expected_reliability="best_effort")
    reliable_by_source = {
        int(row["time_frame"]): row for row in reliable_rows
    }
    best_effort_by_source = {
        int(row["time_frame"]): row for row in best_effort_rows
    }
    common_sources = sorted(set(reliable_by_source) & set(best_effort_by_source))
    overlap_ratio = len(common_sources) / min(len(reliable_rows), len(best_effort_rows))
    if len(common_sources) < 2:
        raise StrictTimingError("dual subscriber captures have no usable source overlap")
    reliable_aligned_rows = [reliable_by_source[value] for value in common_sources]
    best_effort_aligned_rows = [best_effort_by_source[value] for value in common_sources]
    reliable_aligned = _lane_metrics(reliable_aligned_rows)
    best_effort_aligned = _lane_metrics(best_effort_aligned_rows)
    common_payloads_equal = all(
        reliable_by_source[value].get("payload_sha256")
        == best_effort_by_source[value].get("payload_sha256")
        for value in common_sources
    )
    class_pairs = list(zip(reliable_aligned["classes"], best_effort_aligned["classes"]))
    class_agreement = sum(left == right for left, right in class_pairs) / len(class_pairs)
    interval_deltas = [
        abs(float(left) - float(right))
        for left, right in zip(
            reliable_aligned["intervals"], best_effort_aligned["intervals"])
    ]
    independent = reliable_identity["id"] != best_effort_identity["id"]
    same_host_network = (
        reliable_identity["network_mode"] == "host"
        and best_effort_identity["network_mode"] == "host"
    )
    reliable_p95 = float(reliable["receive_interval_p95_ms"])
    best_effort_p95 = float(best_effort["receive_interval_p95_ms"])
    upstream_batching = (
        independent
        and same_host_network
        and overlap_ratio >= 0.99
        and common_payloads_equal
        and class_agreement >= 0.99
        and _percentile(interval_deltas, 0.95) <= 2.0
        and reliable_p95 > STRICT_RECEIVE_INTERVAL_P95_MS
        and best_effort_p95 > STRICT_RECEIVE_INTERVAL_P95_MS
    )
    checks = {
        "reliable_raw_digest_matches_observation": (
            reliable_capture.get("sha256") == reliable_observation.get("capture_sha256")
        ),
        "best_effort_raw_digest_matches_observation": (
            best_effort_capture.get("sha256") == best_effort_observation.get("capture_sha256")
        ),
        "reliable_frame_count_matches_observation": (
            reliable_capture.get("frame_count") == reliable_observation.get("frame_count")
        ),
        "best_effort_frame_count_matches_observation": (
            best_effort_capture.get("frame_count") == best_effort_observation.get("frame_count")
        ),
        "reliable_payload_digest_matches_observation": (
            reliable_capture.get("framed_payload_sha256")
            == reliable_observation.get("framed_payload_sha256")
        ),
        "best_effort_payload_digest_matches_observation": (
            best_effort_capture.get("framed_payload_sha256")
            == best_effort_observation.get("framed_payload_sha256")
        ),
        "dual_lane_source_overlap_at_least_99pct": overlap_ratio >= 0.99,
        "dual_lane_overlapping_payloads_equal": common_payloads_equal,
        "independent_subscriber_containers": independent,
        "subscriber_containers_used_same_image": (
            bool(reliable_identity["image"])
            and reliable_identity["image"] == best_effort_identity["image"]
        ),
        "both_subscribers_used_host_network": same_host_network,
        "both_collectors_read_only_and_unprivileged": all(
            identity[field]
            for identity in (reliable_identity, best_effort_identity)
            for field in (
                "read_only_root", "all_capabilities_dropped",
                "no_new_privileges", "readonly_collector_command",
            )
        ),
        "both_collectors_exited_cleanly": (
            reliable_identity["clean_exit"] and best_effort_identity["clean_exit"]
        ),
        "observation_reliability_matches_lanes": (
            reliable_observation.get("subscriber_reliability") == "reliable"
            and best_effort_observation.get("subscriber_reliability") == "best_effort"
        ),
        "both_observations_cover_at_least_120s": (
            _number_at_least(reliable_observation.get("duration_s"), 120.0)
            and _number_at_least(best_effort_observation.get("duration_s"), 120.0)
        ),
        "receive_event_class_agreement_at_least_99pct": class_agreement >= 0.99,
        "receive_interval_delta_p95_at_most_2ms": _percentile(interval_deltas, 0.95) <= 2.0,
        "reliable_source_step_exact": (
            reliable["source_step_mode"] == EXPECTED_SOURCE_STEP
            and reliable["source_step_outlier_count"] == 0
        ),
        "best_effort_source_step_exact": (
            best_effort["source_step_mode"] == EXPECTED_SOURCE_STEP
            and best_effort["source_step_outlier_count"] == 0
        ),
        "reliable_receive_cadence_p95_at_most_40ms": (
            reliable_p95 <= STRICT_RECEIVE_INTERVAL_P95_MS
        ),
        "best_effort_receive_cadence_p95_at_most_40ms": (
            best_effort_p95 <= STRICT_RECEIVE_INTERVAL_P95_MS
        ),
    }
    integrity_names = {
        name for name in checks if "receive_cadence_p95" not in name
    }
    integrity_passed = all(checks[name] for name in integrity_names)
    cadence_passed = integrity_passed and all(checks.values())
    lane_fields = (
        "frame_count", "first_time_frame", "last_time_frame", "source_step_mode",
        "source_step_outlier_count", "receive_interval_p50_ms",
        "receive_interval_p95_ms", "receive_interval_p99_ms",
        "receive_interval_max_ms", "receive_burst_count", "receive_stall_count",
        "receive_paced_count", "clock_rate_error_ppm",
        "relative_queueing_p05_p95_span_ms", "relative_queueing_full_span_ms",
    )
    return {
        "schema_version": 1,
        "policy_id": POLICY_ID,
        "policy_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "evidence_class": "go2_audio_dual_subscriber_strict_timing",
        "inputs": dict(inputs or {}),
        "thresholds": {
            "receive_interval_p95_max_ms": STRICT_RECEIVE_INTERVAL_P95_MS,
            "source_step": EXPECTED_SOURCE_STEP,
            "source_interval_ms": EXPECTED_SOURCE_INTERVAL_MS,
            "cross_lane_event_class_agreement_min": 0.99,
            "cross_lane_interval_delta_p95_max_ms": 2.0,
        },
        "subscriber_containers": {
            "reliable": reliable_identity,
            "best_effort": best_effort_identity,
        },
        "lanes": {
            "reliable": {field: reliable[field] for field in lane_fields},
            "best_effort": {field: best_effort[field] for field in lane_fields},
        },
        "cross_lane": {
            "common_frame_count": len(common_sources),
            "source_overlap_ratio": overlap_ratio,
            "event_class_agreement": class_agreement,
            "interval_delta_p95_ms": _percentile(interval_deltas, 0.95),
            "interval_delta_max_ms": max(interval_deltas),
        },
        "checks": checks,
        "failures": [name for name, passed in checks.items() if not passed],
        "evidence_integrity_passed": integrity_passed,
        "strict_receive_cadence_passed": cadence_passed,
        "upstream_of_independent_subscriber_processes_batching_indicated": (
            upstream_batching and integrity_passed
        ),
        "subscriber_qos_change_supported_as_remediation": False,
        "one_way_latency_measured": False,
        "absolute_latency_gate_passed": False,
        "passed": False,
        "production_ready": False,
        "speaker_enable_authorized": False,
        "control_authorized": False,
        "boundary": (
            "Receive inter-arrival is cadence/jitter, not absolute one-way latency. "
            "AudioData does not declare a source clock domain and this evidence has no "
            "clock synchronization. Matching bursts in independent subscriber processes "
            "localize batching upstream of their ROS callbacks but do not distinguish the "
            "robot publisher, network, kernel, or DDS receive path. No threshold was relaxed."
        ),
    }


def _write_new_private(path: str | Path, document: Mapping[str, object]) -> None:
    output = Path(path)
    if output.exists() or output.is_symlink():
        raise StrictTimingError(f"output already exists; refusing overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write((json.dumps(document, indent=2, sort_keys=True) + "\n").encode())


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    for name in (
        "reliable-frames", "best-effort-frames", "reliable-observation",
        "best-effort-observation", "reliable-container-inspect",
        "best-effort-container-inspect",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--output", required=True)
    options = parser.parse_args(args)
    reliable_rows, reliable_capture = _load_capture(options.reliable_frames)
    best_effort_rows, best_effort_capture = _load_capture(options.best_effort_frames)
    documents: dict[str, dict[str, object]] = {}
    bindings: dict[str, dict[str, object]] = {
        "reliable_frames": reliable_capture,
        "best_effort_frames": best_effort_capture,
    }
    for name, path in (
        ("reliable_observation", options.reliable_observation),
        ("best_effort_observation", options.best_effort_observation),
        ("reliable_container_inspect", options.reliable_container_inspect),
        ("best_effort_container_inspect", options.best_effort_container_inspect),
    ):
        documents[name], bindings[name] = _json_document(path)
    report = evaluate_strict_timing(
        reliable_rows=reliable_rows,
        best_effort_rows=best_effort_rows,
        reliable_capture=reliable_capture,
        best_effort_capture=best_effort_capture,
        reliable_observation=documents["reliable_observation"],
        best_effort_observation=documents["best_effort_observation"],
        reliable_container=documents["reliable_container_inspect"],
        best_effort_container=documents["best_effort_container_inspect"],
        inputs=bindings,
    )
    _write_new_private(options.output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["strict_receive_cadence_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
