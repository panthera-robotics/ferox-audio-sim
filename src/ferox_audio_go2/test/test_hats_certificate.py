import hashlib
import hmac
import json
import os

import pytest

from ferox_audio_go2.hats_certificate import (
    HatsCertificateError,
    REQUIRED_SCENARIOS,
    REQUIRED_STANDARDS,
    evaluate_hats_report,
    load_authenticated_campaign,
    main,
)


def valid_report(digests):
    return {
        "schema_version": 1,
        "campaign_id": "go2-02-hats-001",
        "robot_id": "go2_02",
        "runtime_firmware": "unitree-go2-fw-1.2.3",
        "canceller": {
            "name": "webrtc-aec3",
            "version": "m145",
            "config_sha256": "a" * 64,
            "render_reference": "pre_audiohub_pcm",
            "playout_delay_measured": True,
        },
        "standards": dict(REQUIRED_STANDARDS),
        "lab": {
            "hats_manufacturer": "lab-vendor",
            "hats_model": "hats-1",
            "hats_serial": "serial-1",
            "measurement_system": "system-1",
            "room_id": "room-1",
            "horizontal_position_error_deg": 1.0,
        },
        "artifact_sha256s": dict(digests),
        "calibration": {
            "certificate_id": "cal-1",
            "valid_at_test": True,
            "before_passed": True,
            "after_passed": True,
        },
        "supervision": {
            "speaker_safe_volume_confirmed": True,
            "physical_stop_available": True,
            "operator_id": "operator-1",
            "lab_operator_id": "lab-operator-1",
        },
        "scenarios": {
            name: {
                "duration_s": 30.0,
                "event_count": 3,
                "artifact_sha256": digests[name],
            } for name in REQUIRED_SCENARIOS
        },
        "measurements": {
            "all_volume_settings_tested": True,
            "volume_settings": [1, 5, 10],
            "nominal_volume_setting": 5,
            "tclw_by_volume_setting_db": {"1": 46.0, "5": 46.0, "10": 46.0},
            "tclw_nominal_db": 46.0,
            "tclw_min_across_volume_settings_db": 46.0,
            "telrdt_db": 37.0,
            "engineering_erle_db": 20.0,
            "engineering_erle_is_not_tclw": True,
            "near_end_wer_baseline": 0.10,
            "near_end_wer_aec": 0.12,
            "double_talk_wer_baseline": 0.10,
            "double_talk_wer_aec": 0.15,
            "barge_in_p95_ms": 200.0,
        },
    }


def campaign(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    artifact_names = (*REQUIRED_SCENARIOS, "positioning", "calibration")
    artifacts = {}
    digests = {}
    for index, name in enumerate(artifact_names):
        path = tmp_path / f"{name}.bin"
        path.write_bytes((name.encode() + bytes([index])) * 4)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        artifacts[name] = {"path": path.name, "sha256": digest}
        digests[name] = digest
    report = valid_report(digests)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report, sort_keys=True))
    key_path = tmp_path / "verification.key"
    key_path.write_bytes(b"k" * 32)
    os.chmod(key_path, 0o600)
    report_payload = report_path.read_bytes()
    manifest = {
        "schema_version": 1,
        "campaign_id": report["campaign_id"],
        "verification_key_id": "panthera-audio-lab-v1",
        "report_path": report_path.name,
        "report_sha256": hashlib.sha256(report_payload).hexdigest(),
        "report_hmac_sha256": hmac.new(
            key_path.read_bytes(), report_payload, hashlib.sha256).hexdigest(),
        "artifacts": artifacts,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    return report, manifest, manifest_path, key_path


def test_authenticated_threshold_edge_campaign_passes_only_acoustic_lane(tmp_path):
    _, _, manifest_path, key_path = campaign(tmp_path)
    report, manifest, bindings = load_authenticated_campaign(
        manifest_path,
        verification_key_path=key_path,
        expected_key_id="panthera-audio-lab-v1",
    )
    certificate = evaluate_hats_report(
        report, manifest=manifest, bindings=bindings)
    assert certificate["aec_acoustic_gate_passed"] is True
    assert certificate["tclw_authorized"] is True
    assert certificate["p340_authorized"] is True
    assert certificate["report_hmac_verified"] is True
    assert certificate["production_ready"] is False
    assert certificate["speaker_enable_authorized"] is False
    assert certificate["control_authorized"] is False
    assert certificate["failures"] == []


@pytest.mark.parametrize(
    ("field", "value", "failure"),
    [
        ("tclw_nominal_db", 45.999, "tclw_nominal_at_least_46db"),
        ("tclw_min_across_volume_settings_db", 45.999,
         "tclw_all_volume_settings_at_least_46db"),
        ("telrdt_db", 36.999, "p340_type1_telrdt_at_least_37db"),
        ("engineering_erle_db", 19.999, "engineering_erle_at_least_20db"),
        ("near_end_wer_aec", 0.121, "near_end_wer_delta_at_most_2pp"),
        ("double_talk_wer_aec", 0.151, "double_talk_wer_delta_at_most_5pp"),
        ("barge_in_p95_ms", 200.001, "barge_in_p95_at_most_200ms"),
    ],
)
def test_each_metric_fails_closed_below_internal_or_published_bar(
        tmp_path, field, value, failure):
    _, _, manifest_path, key_path = campaign(tmp_path)
    report, manifest, bindings = load_authenticated_campaign(
        manifest_path,
        verification_key_path=key_path,
        expected_key_id="panthera-audio-lab-v1",
    )
    report["measurements"][field] = value
    certificate = evaluate_hats_report(
        report, manifest=manifest, bindings=bindings)
    assert certificate["aec_acoustic_gate_passed"] is False
    assert failure in certificate["failures"]
    assert certificate["tclw_authorized"] is False


def test_hmac_report_and_artifact_tampering_are_rejected_before_scoring(tmp_path):
    _, _, manifest_path, key_path = campaign(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    report_path = tmp_path / manifest["report_path"]
    report_path.write_bytes(report_path.read_bytes() + b"\n")
    with pytest.raises(HatsCertificateError, match="SHA-256"):
        load_authenticated_campaign(
            manifest_path, verification_key_path=key_path,
            expected_key_id="panthera-audio-lab-v1")
    _, _, manifest_path, key_path = campaign(tmp_path / "second")
    manifest = json.loads(manifest_path.read_text())
    artifact = tmp_path / "second" / manifest["artifacts"]["double_talk"]["path"]
    artifact.write_bytes(b"tampered")
    with pytest.raises(HatsCertificateError, match="artifact SHA-256"):
        load_authenticated_campaign(
            manifest_path, verification_key_path=key_path,
            expected_key_id="panthera-audio-lab-v1")


def test_manifest_cannot_substitute_artifact_not_bound_by_authenticated_report(
        tmp_path):
    _, _, manifest_path, key_path = campaign(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    replacement = tmp_path / "replacement-positioning.bin"
    replacement.write_bytes(b"replacement")
    manifest["artifacts"]["positioning"] = {
        "path": replacement.name,
        "sha256": hashlib.sha256(replacement.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(HatsCertificateError, match="authenticated report"):
        load_authenticated_campaign(
            manifest_path, verification_key_path=key_path,
            expected_key_id="panthera-audio-lab-v1")


def test_artifact_symlinks_are_rejected(tmp_path):
    _, _, manifest_path, key_path = campaign(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    artifact_path = tmp_path / manifest["artifacts"]["positioning"]["path"]
    target_path = tmp_path / "positioning-target.bin"
    artifact_path.rename(target_path)
    artifact_path.symlink_to(target_path.name)
    with pytest.raises(HatsCertificateError, match="symlink"):
        load_authenticated_campaign(
            manifest_path, verification_key_path=key_path,
            expected_key_id="panthera-audio-lab-v1")


def test_per_volume_results_must_be_complete_and_consistent(tmp_path):
    _, _, manifest_path, key_path = campaign(tmp_path)
    report, manifest, bindings = load_authenticated_campaign(
        manifest_path, verification_key_path=key_path,
        expected_key_id="panthera-audio-lab-v1")
    report["measurements"]["tclw_by_volume_setting_db"]["10"] = 45.9
    certificate = evaluate_hats_report(
        report, manifest=manifest, bindings=bindings)
    assert certificate["aec_acoustic_gate_passed"] is False
    assert "each_volume_setting_tclw_at_least_46db" in certificate["failures"]
    assert "reported_tclw_min_matches_per_setting_results" in certificate["failures"]


def test_key_permissions_identity_and_relative_paths_fail_closed(tmp_path):
    _, _, manifest_path, key_path = campaign(tmp_path)
    os.chmod(key_path, 0o644)
    with pytest.raises(HatsCertificateError, match="group/world"):
        load_authenticated_campaign(
            manifest_path, verification_key_path=key_path,
            expected_key_id="panthera-audio-lab-v1")
    os.chmod(key_path, 0o600)
    with pytest.raises(HatsCertificateError, match="identity"):
        load_authenticated_campaign(
            manifest_path, verification_key_path=key_path,
            expected_key_id="wrong-key")
    manifest = json.loads(manifest_path.read_text())
    manifest["report_path"] = "../report.json"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(HatsCertificateError, match="relative"):
        load_authenticated_campaign(
            manifest_path, verification_key_path=key_path,
            expected_key_id="panthera-audio-lab-v1")


def test_scenario_reuse_and_missing_supervision_do_not_pass(tmp_path):
    _, _, manifest_path, key_path = campaign(tmp_path)
    report, manifest, bindings = load_authenticated_campaign(
        manifest_path, verification_key_path=key_path,
        expected_key_id="panthera-audio-lab-v1")
    report["scenarios"]["double_talk"]["artifact_sha256"] = (
        report["scenarios"]["near_end_single_talk"]["artifact_sha256"])
    report["supervision"]["physical_stop_available"] = False
    certificate = evaluate_hats_report(
        report, manifest=manifest, bindings=bindings)
    assert "scenario_artifacts_are_distinct" in certificate["failures"]
    assert "double_talk_artifact_digest_bound" in certificate["failures"]
    assert "physical_stop_available" in certificate["failures"]


def test_cli_writes_private_certificate_and_refuses_overwrite(tmp_path):
    _, _, manifest_path, key_path = campaign(tmp_path)
    output = tmp_path / "certificate.json"
    main([
        "--campaign-manifest", str(manifest_path),
        "--verification-key-file", str(key_path),
        "--expected-key-id", "panthera-audio-lab-v1",
        "--output", str(output),
    ])
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(HatsCertificateError, match="refusing overwrite"):
        main([
            "--campaign-manifest", str(manifest_path),
            "--verification-key-file", str(key_path),
            "--expected-key-id", "panthera-audio-lab-v1",
            "--output", str(output),
        ])
