import base64
import hashlib
import json

import pytest

from ferox_audio_go2.strict_timing import (
    StrictTimingError,
    _json_document,
    _load_capture,
    evaluate_strict_timing,
    main,
)


def write_capture(path, intervals_ms):
    receive = 10.0
    rows = []
    for index in range(len(intervals_ms) + 1):
        payload = bytes([index % 251]) * 160
        rows.append(json.dumps({
            "payload_b64": base64.b64encode(payload).decode("ascii"),
            "receive_steady_s": receive,
            "time_frame": 1_000_000 + index * 200_000,
        }, separators=(",", ":"), sort_keys=True))
        if index < len(intervals_ms):
            receive += intervals_ms[index] / 1000.0
    path.write_text("\n".join(rows) + "\n")


def observation(path, reliability):
    rows, binding = _load_capture(path)
    return {
        "capture_sha256": binding["sha256"],
        "framed_payload_sha256": binding["framed_payload_sha256"],
        "frame_count": len(rows),
        "duration_s": 120.0,
        "subscriber_reliability": reliability,
    }


def inspect(container_id, name):
    reliability = "best_effort" if "best" in name else "reliable"
    return {
        "Id": container_id * 64,
        "Name": f"/{name}",
        "Config": {
            "Image": "ferox/audio-go2:test",
            "Cmd": [
                "go2_audio_readonly_discovery",
                f"--qos-reliability {reliability}",
                "--frames-output /evidence/frames.jsonl",
            ],
        },
        "HostConfig": {
            "NetworkMode": "host",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
        },
        "State": {"ExitCode": 0, "OOMKilled": False},
    }


def evaluate_pair(tmp_path, intervals):
    reliable_path = tmp_path / "reliable.jsonl"
    best_effort_path = tmp_path / "best-effort.jsonl"
    write_capture(reliable_path, intervals)
    write_capture(best_effort_path, [value + 0.1 for value in intervals])
    reliable_rows, reliable_binding = _load_capture(reliable_path)
    best_effort_rows, best_effort_binding = _load_capture(best_effort_path)
    return evaluate_strict_timing(
        reliable_rows=reliable_rows,
        best_effort_rows=best_effort_rows,
        reliable_capture=reliable_binding,
        best_effort_capture=best_effort_binding,
        reliable_observation=observation(reliable_path, "reliable"),
        best_effort_observation=observation(best_effort_path, "best_effort"),
        reliable_container=inspect("a", "reliable"),
        best_effort_container=inspect("b", "best-effort"),
    )


def test_independent_matching_bursts_localize_without_claiming_latency(tmp_path):
    intervals = [0.8, 20.5, 42.2, 0.9, 20.7, 42.1] * 20
    report = evaluate_pair(tmp_path, intervals)
    assert report["evidence_integrity_passed"] is True
    assert report["strict_receive_cadence_passed"] is False
    assert report["upstream_of_independent_subscriber_processes_batching_indicated"] is True
    assert report["subscriber_qos_change_supported_as_remediation"] is False
    assert report["one_way_latency_measured"] is False
    assert report["absolute_latency_gate_passed"] is False
    assert report["passed"] is False
    assert report["production_ready"] is False
    assert report["cross_lane"]["event_class_agreement"] == 1.0
    assert report["lanes"]["reliable"]["source_step_outlier_count"] == 0
    assert "reliable_receive_cadence_p95_at_most_40ms" in report["failures"]


def test_paced_capture_passes_cadence_but_not_absolute_latency(tmp_path):
    report = evaluate_pair(tmp_path, [20.0] * 120)
    assert report["strict_receive_cadence_passed"] is True
    assert report["upstream_of_independent_subscriber_processes_batching_indicated"] is False
    assert report["absolute_latency_gate_passed"] is False
    assert report["passed"] is False


def test_one_frame_stagger_is_aligned_by_source_timestamp(tmp_path):
    reliable_path = tmp_path / "reliable.jsonl"
    best_effort_path = tmp_path / "best-effort.jsonl"
    intervals = [0.8, 20.5, 42.2] * 50
    write_capture(reliable_path, intervals)
    write_capture(best_effort_path, [value + 0.1 for value in intervals[1:]])
    reliable_rows, reliable_binding = _load_capture(reliable_path)
    best_effort_rows, best_effort_binding = _load_capture(best_effort_path)
    # Shift the second source timeline by one frame while preserving payload identity
    # for the overlapping source values.
    for index, row in enumerate(best_effort_rows):
        row["time_frame"] += 200_000
        if index + 1 < len(reliable_rows):
            row["payload_sha256"] = reliable_rows[index + 1]["payload_sha256"]
    report = evaluate_strict_timing(
        reliable_rows=reliable_rows,
        best_effort_rows=best_effort_rows,
        reliable_capture=reliable_binding,
        best_effort_capture=best_effort_binding,
        reliable_observation=observation(reliable_path, "reliable"),
        best_effort_observation=observation(best_effort_path, "best_effort"),
        reliable_container=inspect("a", "reliable"),
        best_effort_container=inspect("b", "best-effort"),
    )
    assert report["cross_lane"]["source_overlap_ratio"] > 0.99
    assert report["checks"]["dual_lane_source_overlap_at_least_99pct"] is True


def test_raw_capture_rejects_schema_payload_and_symlink(tmp_path):
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text('{"receive_steady_s":1,"time_frame":1,"payload_b64":"AA==","extra":1}\n')
    with pytest.raises(StrictTimingError, match="schema"):
        _load_capture(malformed)
    target = tmp_path / "target.jsonl"
    write_capture(target, [20.0])
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(StrictTimingError, match="non-symlink"):
        _load_capture(link)


def test_docker_inspect_native_single_element_array_is_accepted(tmp_path):
    path = tmp_path / "inspect.json"
    path.write_text(json.dumps([inspect("a", "reliable")]))
    document, binding = _json_document(path)
    assert document["Name"] == "/reliable"
    assert binding["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(json.dumps([inspect("a", "one"), inspect("b", "two")]))
    with pytest.raises(StrictTimingError, match="exactly one"):
        _json_document(path)


@pytest.mark.parametrize(
    "security_option",
    ["no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"],
)
def test_docker_no_new_privileges_inspect_forms_are_accepted(
    tmp_path, security_option,
):
    reliable_path = tmp_path / "reliable.jsonl"
    best_effort_path = tmp_path / "best-effort.jsonl"
    write_capture(reliable_path, [20.0] * 120)
    write_capture(best_effort_path, [20.0] * 120)
    reliable_rows, reliable_binding = _load_capture(reliable_path)
    best_effort_rows, best_effort_binding = _load_capture(best_effort_path)
    reliable_inspect = inspect("a", "reliable")
    best_effort_inspect = inspect("b", "best-effort")
    reliable_inspect["HostConfig"]["SecurityOpt"] = [security_option]
    best_effort_inspect["HostConfig"]["SecurityOpt"] = [security_option]
    report = evaluate_strict_timing(
        reliable_rows=reliable_rows,
        best_effort_rows=best_effort_rows,
        reliable_capture=reliable_binding,
        best_effort_capture=best_effort_binding,
        reliable_observation=observation(reliable_path, "reliable"),
        best_effort_observation=observation(best_effort_path, "best_effort"),
        reliable_container=reliable_inspect,
        best_effort_container=best_effort_inspect,
    )
    assert report["checks"]["both_collectors_read_only_and_unprivileged"] is True


@pytest.mark.parametrize(
    "security_option",
    ["no-new-privileges:false", "no-new-privileges=false", "no-new-privileges-extra"],
)
def test_disabled_or_lookalike_no_new_privileges_is_rejected(
    tmp_path, security_option,
):
    reliable_path = tmp_path / "reliable.jsonl"
    best_effort_path = tmp_path / "best-effort.jsonl"
    write_capture(reliable_path, [20.0] * 120)
    write_capture(best_effort_path, [20.0] * 120)
    reliable_rows, reliable_binding = _load_capture(reliable_path)
    best_effort_rows, best_effort_binding = _load_capture(best_effort_path)
    reliable_inspect = inspect("a", "reliable")
    best_effort_inspect = inspect("b", "best-effort")
    reliable_inspect["HostConfig"]["SecurityOpt"] = [security_option]
    best_effort_inspect["HostConfig"]["SecurityOpt"] = [security_option]
    report = evaluate_strict_timing(
        reliable_rows=reliable_rows,
        best_effort_rows=best_effort_rows,
        reliable_capture=reliable_binding,
        best_effort_capture=best_effort_binding,
        reliable_observation=observation(reliable_path, "reliable"),
        best_effort_observation=observation(best_effort_path, "best_effort"),
        reliable_container=reliable_inspect,
        best_effort_container=best_effort_inspect,
    )
    assert report["checks"]["both_collectors_read_only_and_unprivileged"] is False


def test_native_rclcpp_collector_is_accepted_by_strict_identity(tmp_path):
    intervals = [20.0] * 120
    reliable_path = tmp_path / "reliable.jsonl"
    best_effort_path = tmp_path / "best-effort.jsonl"
    write_capture(reliable_path, intervals)
    write_capture(best_effort_path, intervals)
    reliable_rows, reliable_binding = _load_capture(reliable_path)
    best_effort_rows, best_effort_binding = _load_capture(best_effort_path)
    reliable_inspect = inspect("a", "reliable")
    best_effort_inspect = inspect("b", "best-effort")
    for document in (reliable_inspect, best_effort_inspect):
        reliability = "best_effort" if "best" in document["Name"] else "reliable"
        document["Config"]["Cmd"] = [
            "ros2 run ferox_audio_go2_native go2_audio_native_timing_probe",
            f"--qos-reliability {reliability}",
            "--frames-output /evidence/frames.jsonl",
            "--metadata-output /evidence/metadata.jsonl",
        ]
    report = evaluate_strict_timing(
        reliable_rows=reliable_rows,
        best_effort_rows=best_effort_rows,
        reliable_capture=reliable_binding,
        best_effort_capture=best_effort_binding,
        reliable_observation=observation(reliable_path, "reliable"),
        best_effort_observation=observation(best_effort_path, "best_effort"),
        reliable_container=reliable_inspect,
        best_effort_container=best_effort_inspect,
    )
    assert report["checks"]["both_collectors_read_only_and_unprivileged"] is True
    assert report["subscriber_containers"]["reliable"][
        "collector_implementation"] == "rclcpp_native"


def test_tampered_observation_fails_integrity(tmp_path):
    reliable_path = tmp_path / "reliable.jsonl"
    best_effort_path = tmp_path / "best-effort.jsonl"
    write_capture(reliable_path, [20.0] * 10)
    write_capture(best_effort_path, [20.1] * 10)
    reliable_rows, reliable_binding = _load_capture(reliable_path)
    best_effort_rows, best_effort_binding = _load_capture(best_effort_path)
    tampered = observation(reliable_path, "reliable")
    tampered["capture_sha256"] = "0" * 64
    report = evaluate_strict_timing(
        reliable_rows=reliable_rows,
        best_effort_rows=best_effort_rows,
        reliable_capture=reliable_binding,
        best_effort_capture=best_effort_binding,
        reliable_observation=tampered,
        best_effort_observation=observation(best_effort_path, "best_effort"),
        reliable_container=inspect("a", "reliable"),
        best_effort_container=inspect("b", "best-effort"),
    )
    assert report["evidence_integrity_passed"] is False
    assert report["strict_receive_cadence_passed"] is False
    assert report["upstream_of_independent_subscriber_processes_batching_indicated"] is False


def test_cli_binds_every_input_and_refuses_to_report_failed_cadence_as_pass(tmp_path):
    reliable_frames = tmp_path / "reliable.jsonl"
    best_effort_frames = tmp_path / "best-effort.jsonl"
    intervals = [0.8, 20.5, 42.2] * 50
    write_capture(reliable_frames, intervals)
    write_capture(best_effort_frames, [value + 0.1 for value in intervals])
    inputs = {
        "reliable-observation": observation(reliable_frames, "reliable"),
        "best-effort-observation": observation(best_effort_frames, "best_effort"),
        "reliable-container-inspect": inspect("a", "reliable"),
        "best-effort-container-inspect": inspect("b", "best-effort"),
    }
    paths = {}
    for name, document in inputs.items():
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(document))
    output = tmp_path / "strict.json"
    with pytest.raises(SystemExit) as exc:
        main([
            "--reliable-frames", str(reliable_frames),
            "--best-effort-frames", str(best_effort_frames),
            "--reliable-observation", str(paths["reliable-observation"]),
            "--best-effort-observation", str(paths["best-effort-observation"]),
            "--reliable-container-inspect", str(paths["reliable-container-inspect"]),
            "--best-effort-container-inspect", str(paths["best-effort-container-inspect"]),
            "--output", str(output),
        ])
    assert exc.value.code == 1
    report = json.loads(output.read_text())
    assert set(report["inputs"]) == {
        "reliable_frames", "best_effort_frames", "reliable_observation",
        "best_effort_observation", "reliable_container_inspect",
        "best_effort_container_inspect",
    }
    assert hashlib.sha256(reliable_frames.read_bytes()).hexdigest() == (
        report["inputs"]["reliable_frames"]["sha256"]
    )
