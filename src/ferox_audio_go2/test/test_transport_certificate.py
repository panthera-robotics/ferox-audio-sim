import json
import stat

import pytest

from ferox_audio_go2.transport_certificate import (
    TransportCertificateError,
    _write_new_private,
    evaluate_transport_evidence,
    main,
)


def observation(reliability, *, p95_ms=39.0, capture="a" * 64, payload="b" * 64):
    return {
        "schema_version": 2,
        "source_topic": "/audiosender",
        "source_type": "unitree_go/msg/AudioData",
        "subscriber_reliability": reliability,
        "capture_sha256": capture,
        "framed_payload_sha256": payload,
        "duration_s": 120.01,
        "effective_rate_hz": 50.0,
        "frame_count": 6_001,
        "interval_p95_ms": p95_ms,
        "nonempty_ratio": 1.0,
        "payload_bytes_min": 160,
        "payload_bytes_max": 160,
        "payload_bytes_mode": 160,
        "time_frame_monotonic": True,
        "time_frame_step_mode": 200_000,
        "time_frame_step_outlier_count": 0,
    }


def codec(*, capture="a" * 64, recording="c" * 64):
    return {
        "capture_sha256": capture,
        "decoder": "opus",
        "decoded_frames": 6_001,
        "decoded_sample_rate": 48_000,
        "decoded_channels": 1,
        "decode_errors": 0,
        "recording_sha256": recording,
        "operator_audio_intelligible": False,
    }


def evaluate(*, reliable_p95=39.0, best_effort_p95=39.0):
    return evaluate_transport_evidence(
        reliable_observation=observation("reliable", p95_ms=reliable_p95),
        reliable_codec=codec(),
        best_effort_observation=observation("best_effort", p95_ms=best_effort_p95),
        best_effort_codec=codec(),
    )


def test_transport_integrity_and_strict_latency_are_separate():
    report = evaluate(reliable_p95=42.41574, best_effort_p95=42.384827)
    assert report["transport_integrity_passed"] is True
    assert len(report["policy_source_sha256"]) == 64
    assert report["strict_transport_gate_passed"] is False
    assert report["passed"] is False
    assert report["production_ready"] is False
    assert report["speaker_enable_authorized"] is False
    assert report["failures"] == [
        "reliable_receive_interval_p95_at_most_40ms",
        "best_effort_receive_interval_p95_at_most_40ms",
    ]


def test_under_threshold_transport_still_does_not_claim_production():
    report = evaluate()
    assert report["transport_integrity_passed"] is True
    assert report["strict_transport_gate_passed"] is True
    assert report["passed"] is True
    assert report["production_ready"] is False
    assert report["operator_audio_intelligible"] is False


def test_capture_and_cross_lane_tampering_fail_closed():
    report = evaluate_transport_evidence(
        reliable_observation=observation("reliable"),
        reliable_codec=codec(capture="d" * 64),
        best_effort_observation=observation("best_effort", payload="e" * 64),
        best_effort_codec=codec(recording="f" * 64),
    )
    assert report["transport_integrity_passed"] is False
    assert report["strict_transport_gate_passed"] is False
    assert {
        "reliable_capture_digest_matches",
        "dual_qos_framed_payload_digest_matches",
        "dual_qos_recording_digest_matches",
    }.issubset(report["failures"])


def test_certificate_cli_binds_inputs_and_writes_private_file(tmp_path):
    paths = {}
    documents = {
        "reliable-observation": observation("reliable"),
        "reliable-codec": codec(),
        "best-effort-observation": observation("best_effort"),
        "best-effort-codec": codec(),
    }
    for name, document in documents.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(document))
        paths[name] = path
    output = tmp_path / "certificate.json"
    main([
        "--reliable-observation", str(paths["reliable-observation"]),
        "--reliable-codec", str(paths["reliable-codec"]),
        "--best-effort-observation", str(paths["best-effort-observation"]),
        "--best-effort-codec", str(paths["best-effort-codec"]),
        "--output", str(output),
    ])
    certificate = json.loads(output.read_text())
    assert certificate["strict_transport_gate_passed"] is True
    assert set(certificate["inputs"]) == {
        "reliable_observation", "reliable_codec",
        "best_effort_observation", "best_effort_codec",
    }
    assert all(len(binding["sha256"]) == 64 for binding in certificate["inputs"].values())
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_private_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "certificate.json"
    _write_new_private(output, {"first": True})
    with pytest.raises(TransportCertificateError, match="refusing overwrite"):
        _write_new_private(output, {"second": True})
    assert json.loads(output.read_text()) == {"first": True}
