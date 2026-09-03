import base64
import hashlib
import json

import pytest

from ferox_audio_go2.native_timing_certificate import (
    NativeTimingCertificateError,
    _load_frames,
    _load_metadata,
    certify_native_timing,
    main,
)


def write_native_bundle(tmp_path, *, count=10, reliability="reliable"):
    frames_path = tmp_path / "frames.jsonl"
    metadata_path = tmp_path / "metadata.jsonl"
    steady_start = 10_000_000_000
    system_start = 1_800_000_000_000_000_000
    frame_lines = []
    metadata_lines = [json.dumps({
        "capture_start_steady_ns": steady_start,
        "capture_start_system_ns": system_start,
        "collector": "rclcpp_native",
        "host_boot_id": "01234567-89ab-cdef-0123-456789abcdef",
        "publisher_created": False,
        "qos_reliability": reliability,
        "record_type": "capture_start",
        "requested_duration_s": 5.0,
        "schema_version": 1,
        "source_topic": "/audiosender",
        "speaker_or_audiohub_expected": False,
        "supervised_speaker_capture_token": None,
    }, separators=(",", ":"), sort_keys=True)]
    for index in range(count):
        callback_steady = steady_start + 100_000_000 + index * 20_000_000
        callback_system = system_start + 100_000_000 + index * 20_000_000
        time_frame = 1_000_000 + index * 200_000
        payload = bytes([index % 251]) * 160
        frame_lines.append(
            "{\"payload_b64\":\""
            + base64.b64encode(payload).decode()
            + f"\",\"receive_steady_s\":{callback_steady / 1e9:.9f}"
            + f",\"time_frame\":{time_frame}}}"
        )
        metadata_lines.append(json.dumps({
            "callback_steady_ns": callback_steady,
            "callback_system_ns": callback_system,
            "from_intra_process": False,
            "publisher_gid_hex": "ab" * 24,
            "record_type": "frame",
            "rmw_received_timestamp_ns": callback_system - 1_000_000,
            "rmw_source_timestamp_ns": callback_system - 2_000_000,
            "time_frame": time_frame,
        }, separators=(",", ":"), sort_keys=True))
    metadata_lines.append(json.dumps({
        "capture_end_steady_ns": steady_start + 5_000_000_000,
        "capture_end_system_ns": system_start + 5_000_000_000,
        "elapsed_s": 5.0,
        "frame_count": count,
        "record_type": "capture_end",
        "speaker_or_audiohub_called": False,
    }, separators=(",", ":"), sort_keys=True))
    frames_path.write_text("\n".join(frame_lines) + "\n")
    metadata_path.write_text("\n".join(metadata_lines) + "\n")
    return frames_path, metadata_path


def load_certificate(frames_path, metadata_path):
    frames, frame_binding = _load_frames(frames_path)
    header, rows, trailer, metadata_binding = _load_metadata(metadata_path)
    return certify_native_timing(
        frames=frames,
        frame_binding=frame_binding,
        header=header,
        metadata_rows=rows,
        trailer=trailer,
        metadata_binding=metadata_binding,
    )


def test_native_certificate_binds_rmw_and_frame_evidence(tmp_path):
    frames_path, metadata_path = write_native_bundle(tmp_path)
    certificate = load_certificate(frames_path, metadata_path)
    assert certificate["collector_implementation"] == "rclcpp_native"
    assert certificate["native_evidence_integrity_passed"] is True
    assert certificate["native_timing"]["publisher_gid_distinct_count"] == 1
    assert certificate["native_timing"][
        "rmw_received_to_callback_system_p95_ms"] == pytest.approx(1.0)
    assert certificate["native_timing"]["rmw_source"][
        "interval_p95_ms"] == pytest.approx(20.0)
    assert certificate["native_timing_inputs"]["frames"]["sha256"] == (
        hashlib.sha256(frames_path.read_bytes()).hexdigest()
    )
    assert certificate["one_way_latency_measured"] is False
    assert certificate["production_ready"] is False
    assert certificate["speaker_enable_authorized"] is False
    assert certificate["control_authorized"] is False


def test_native_certificate_rejects_frame_metadata_mismatch(tmp_path):
    frames_path, metadata_path = write_native_bundle(tmp_path)
    lines = metadata_path.read_text().splitlines()
    row = json.loads(lines[2])
    row["time_frame"] += 1
    lines[2] = json.dumps(row, separators=(",", ":"), sort_keys=True)
    metadata_path.write_text("\n".join(lines) + "\n")
    with pytest.raises(NativeTimingCertificateError, match="source timestamp mismatch"):
        load_certificate(frames_path, metadata_path)


def test_native_certificate_rejects_unsafe_or_invalid_schema_inputs(tmp_path):
    frames_path, metadata_path = write_native_bundle(tmp_path)
    link = tmp_path / "metadata-link.jsonl"
    link.symlink_to(metadata_path)
    with pytest.raises(NativeTimingCertificateError, match="non-symlink"):
        _load_metadata(link)
    document = json.loads(frames_path.read_text().splitlines()[0])
    document["unexpected"] = True
    lines = frames_path.read_text().splitlines()
    lines[0] = json.dumps(document)
    frames_path.write_text("\n".join(lines) + "\n")
    with pytest.raises(NativeTimingCertificateError, match="invalid schema"):
        load_certificate(frames_path, metadata_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("host_boot_id", "not-a-boot-id", "boot ID"),
        ("speaker_or_audiohub_expected", True, "safety boundary"),
        ("supervised_speaker_capture_token", "unexpected", "safety boundary"),
    ],
)
def test_native_certificate_rejects_non_readonly_capture_header(
    tmp_path, field, value, message,
):
    frames_path, metadata_path = write_native_bundle(tmp_path)
    lines = metadata_path.read_text().splitlines()
    header = json.loads(lines[0])
    header[field] = value
    lines[0] = json.dumps(header, separators=(",", ":"), sort_keys=True)
    metadata_path.write_text("\n".join(lines) + "\n")
    with pytest.raises(NativeTimingCertificateError, match=message):
        load_certificate(frames_path, metadata_path)


def test_native_certificate_cli_refuses_overwrite(tmp_path):
    frames_path, metadata_path = write_native_bundle(tmp_path)
    output = tmp_path / "certificate.json"
    main([
        "--frames", str(frames_path),
        "--metadata", str(metadata_path),
        "--output", str(output),
    ])
    assert json.loads(output.read_text())["native_evidence_integrity_passed"] is True
    with pytest.raises(NativeTimingCertificateError, match="already exists"):
        main([
            "--frames", str(frames_path),
            "--metadata", str(metadata_path),
            "--output", str(output),
        ])
