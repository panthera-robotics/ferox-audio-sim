"""Authenticated, fail-closed import of Go2 AEC/HATS lab evidence.

The tool does not calculate ETSI TCLw or ITU-T TELRDT from ordinary WAVs.
Those metrics must come from the lab report and the report must be authenticated
with an out-of-repository HMAC key.  Scenario artifacts are re-read and bound by
SHA-256 before any AEC acoustic gate can pass.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path


POLICY_ID = "ferox-go2-aec-hats-v1"
TCLW_NOMINAL_MIN_DB = 46.0
TCLW_ANY_VOLUME_MIN_DB = 46.0
TCLW_RECOMMENDED_DB = 50.0
P340_TYPE1_TELRDT_MIN_DB = 37.0
ENGINEERING_ERLE_MIN_DB = 20.0
NEAR_END_WER_DELTA_MAX = 0.02
DOUBLE_TALK_WER_DELTA_MAX = 0.05
BARGE_IN_P95_MAX_MS = 200.0
MIN_SCENARIO_DURATION_S = 30.0
MIN_SCENARIO_EVENTS = 3
REQUIRED_SCENARIOS = (
    "far_end_single_talk",
    "near_end_single_talk",
    "double_talk",
)
REQUIRED_STANDARDS = {
    "tclw": "ETSI ES 202 738 V1.8.2 (2022-05)",
    "hats": "ITU-T P.581 (07/2022)",
    "double_talk_basis": "ITU-T P.340 (05/2000), Type 1",
    "current_p340_review": "ITU-T P.340 (07/2026)",
    "test_signal": "ITU-T P.501 (04/2025)",
    "analysis": "ITU-T P.502 (05/2000) + Amendment 2 (09/2014)",
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PORTABLE = re.compile(r"[A-Za-z0-9_.:+-]{1,128}\Z")
_ROBOT_ID = re.compile(r"go2_[0-9]{2}\Z")


class HatsCertificateError(ValueError):
    """HATS evidence is malformed, unauthenticated, or incomplete."""


def _regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise HatsCertificateError(f"evidence must be a regular non-symlink file: {path}")
    payload = path.read_bytes()
    if not payload or len(payload) > maximum_bytes:
        raise HatsCertificateError(f"evidence size is invalid: {path}")
    return payload


def _hash_regular_file(path: Path, *, maximum_bytes: int) -> tuple[str, int]:
    if not path.is_file() or path.is_symlink():
        raise HatsCertificateError(f"evidence must be a regular non-symlink file: {path}")
    size_bytes = path.stat().st_size
    if size_bytes <= 0 or size_bytes > maximum_bytes:
        raise HatsCertificateError(f"evidence size is invalid: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), size_bytes


def _json(payload: bytes, *, label: str) -> dict[str, object]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HatsCertificateError(f"invalid {label} JSON") from exc
    if not isinstance(document, dict):
        raise HatsCertificateError(f"{label} must be a JSON object")
    return document


def _portable(value: object) -> bool:
    return bool(_PORTABLE.fullmatch(str(value or "").strip()))


def _digest(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if _DIGEST.fullmatch(text) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _relative_artifact(base: Path, value: object) -> Path:
    text = str(value or "")
    candidate = Path(text)
    if not text or candidate.is_absolute() or ".." in candidate.parts:
        raise HatsCertificateError("artifact paths must be safe relative paths")
    source = base / candidate
    cursor = base
    for part in candidate.parts:
        cursor /= part
        if cursor.is_symlink():
            raise HatsCertificateError("artifact paths must not contain symlinks")
    resolved = source.resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise HatsCertificateError("artifact path escapes the campaign directory") from exc
    return source


def _load_key(path: str | Path) -> bytes:
    source = Path(path)
    payload = _regular_file(source, maximum_bytes=4096)
    if stat.S_IMODE(source.stat().st_mode) & 0o077:
        raise HatsCertificateError("verification key must not be group/world accessible")
    if len(payload) < 32:
        raise HatsCertificateError("verification key must contain at least 256 bits")
    return payload


def load_authenticated_campaign(
    manifest_path: str | Path,
    *,
    verification_key_path: str | Path,
    expected_key_id: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    manifest_source = Path(manifest_path)
    manifest_payload = _regular_file(manifest_source, maximum_bytes=1_000_000)
    manifest = _json(manifest_payload, label="campaign manifest")
    if manifest.get("schema_version") != 1:
        raise HatsCertificateError("campaign manifest schema_version must be 1")
    if not _portable(expected_key_id) or manifest.get("verification_key_id") != expected_key_id:
        raise HatsCertificateError("campaign verification key identity mismatch")
    report_source = _relative_artifact(
        manifest_source.parent, manifest.get("report_path"))
    report_payload = _regular_file(report_source, maximum_bytes=10_000_000)
    report_sha256 = hashlib.sha256(report_payload).hexdigest()
    if _digest(manifest.get("report_sha256")) != report_sha256:
        raise HatsCertificateError("campaign report SHA-256 mismatch")
    key = _load_key(verification_key_path)
    expected_hmac = hmac.new(key, report_payload, hashlib.sha256).hexdigest()
    supplied_hmac = _digest(manifest.get("report_hmac_sha256"))
    if supplied_hmac is None or not hmac.compare_digest(supplied_hmac, expected_hmac):
        raise HatsCertificateError("campaign report HMAC verification failed")
    report = _json(report_payload, label="HATS report")
    report_artifact_digests = _mapping(report.get("artifact_sha256s"))
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise HatsCertificateError("campaign artifacts must be an object")
    bindings: dict[str, object] = {
        "manifest": {
            "path": str(manifest_source.resolve()),
            "sha256": hashlib.sha256(manifest_payload).hexdigest(),
            "size_bytes": len(manifest_payload),
        },
        "report": {
            "path": str(report_source),
            "sha256": report_sha256,
            "size_bytes": len(report_payload),
            "hmac_sha256_verified": True,
            "verification_key_id": expected_key_id,
        },
        "artifacts": {},
    }
    artifact_bindings = bindings["artifacts"]
    assert isinstance(artifact_bindings, dict)
    for name in (*REQUIRED_SCENARIOS, "positioning", "calibration"):
        spec = artifacts.get(name)
        if not isinstance(spec, Mapping):
            raise HatsCertificateError(f"missing campaign artifact: {name}")
        artifact_source = _relative_artifact(manifest_source.parent, spec.get("path"))
        actual, size_bytes = _hash_regular_file(
            artifact_source, maximum_bytes=1_000_000_000)
        if _digest(spec.get("sha256")) != actual:
            raise HatsCertificateError(f"campaign artifact SHA-256 mismatch: {name}")
        if _digest(report_artifact_digests.get(name)) != actual:
            raise HatsCertificateError(
                f"authenticated report does not bind campaign artifact: {name}")
        artifact_bindings[name] = {
            "path": str(artifact_source),
            "sha256": actual,
            "size_bytes": size_bytes,
        }
    return report, manifest, bindings


def evaluate_hats_report(
    report: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    bindings: Mapping[str, object],
) -> dict[str, object]:
    standards = _mapping(report.get("standards"))
    canceller = _mapping(report.get("canceller"))
    lab = _mapping(report.get("lab"))
    calibration = _mapping(report.get("calibration"))
    supervision = _mapping(report.get("supervision"))
    scenarios = _mapping(report.get("scenarios"))
    measurements = _mapping(report.get("measurements"))
    tclw_nominal = _number(measurements.get("tclw_nominal_db"))
    tclw_any_volume = _number(measurements.get("tclw_min_across_volume_settings_db"))
    telrdt = _number(measurements.get("telrdt_db"))
    erle = _number(measurements.get("engineering_erle_db"))
    near_baseline = _number(measurements.get("near_end_wer_baseline"))
    near_aec = _number(measurements.get("near_end_wer_aec"))
    double_baseline = _number(measurements.get("double_talk_wer_baseline"))
    double_aec = _number(measurements.get("double_talk_wer_aec"))
    barge_in = _number(measurements.get("barge_in_p95_ms"))
    volume_settings = measurements.get("volume_settings")
    volume_results = _mapping(measurements.get("tclw_by_volume_setting_db"))
    normalized_volume_settings = (
        [str(value) for value in volume_settings]
        if isinstance(volume_settings, list) else []
    )
    volume_values = {
        str(name): _number(value) for name, value in volume_results.items()
    }
    nominal_volume_setting = str(
        measurements.get("nominal_volume_setting") or "")
    computed_tclw_min = (
        min(value for value in volume_values.values() if value is not None)
        if volume_values and all(value is not None for value in volume_values.values())
        else None
    )
    position_error = _number(lab.get("horizontal_position_error_deg"))
    artifact_bindings = _mapping(bindings.get("artifacts"))
    scenario_checks: dict[str, bool] = {}
    scenario_metrics: dict[str, object] = {}
    scenario_digests: list[str] = []
    for name in REQUIRED_SCENARIOS:
        scenario = _mapping(scenarios.get(name))
        duration = _number(scenario.get("duration_s"))
        events = _integer(scenario.get("event_count"))
        reported_digest = _digest(scenario.get("artifact_sha256"))
        bound_digest = _digest(_mapping(artifact_bindings.get(name)).get("sha256"))
        scenario_checks[f"{name}_duration_at_least_30s"] = (
            duration is not None and duration >= MIN_SCENARIO_DURATION_S
        )
        scenario_checks[f"{name}_event_count_at_least_3"] = (
            events is not None and events >= MIN_SCENARIO_EVENTS
        )
        scenario_checks[f"{name}_artifact_digest_bound"] = (
            reported_digest is not None and reported_digest == bound_digest
        )
        scenario_metrics[name] = {
            "duration_s": duration,
            "event_count": events,
            "artifact_sha256": reported_digest,
        }
        if reported_digest:
            scenario_digests.append(reported_digest)
    checks = {
        "report_schema_v1": report.get("schema_version") == 1,
        "campaign_id_matches_manifest": (
            _portable(report.get("campaign_id"))
            and report.get("campaign_id") == manifest.get("campaign_id")
        ),
        "robot_id_go2": bool(_ROBOT_ID.fullmatch(str(report.get("robot_id") or ""))),
        "firmware_fingerprint_present": _portable(report.get("runtime_firmware")),
        "canceller_name_present": _portable(canceller.get("name")),
        "canceller_version_present": _portable(canceller.get("version")),
        "canceller_config_digest_present": _digest(canceller.get("config_sha256")) is not None,
        "render_reference_pcm_declared": canceller.get("render_reference") == "pre_audiohub_pcm",
        "playout_delay_measured": canceller.get("playout_delay_measured") is True,
        "standards_exactly_pinned": all(
            standards.get(name) == expected for name, expected in REQUIRED_STANDARDS.items()
        ),
        "hats_identity_complete": all(
            _portable(lab.get(name)) for name in (
                "hats_manufacturer", "hats_model", "hats_serial",
                "measurement_system", "room_id",
            )
        ),
        "hats_horizontal_position_within_2deg": (
            position_error is not None and abs(position_error) <= 2.0
        ),
        "positioning_artifact_bound": _digest(
            _mapping(artifact_bindings.get("positioning")).get("sha256")) is not None,
        "calibration_artifact_bound": _digest(
            _mapping(artifact_bindings.get("calibration")).get("sha256")) is not None,
        "calibration_certificate_present": _portable(
            calibration.get("certificate_id")),
        "calibration_valid_at_test": calibration.get("valid_at_test") is True,
        "calibration_before_and_after_passed": (
            calibration.get("before_passed") is True
            and calibration.get("after_passed") is True
        ),
        "speaker_safe_volume_supervised": (
            supervision.get("speaker_safe_volume_confirmed") is True
            and _portable(supervision.get("operator_id"))
            and _portable(supervision.get("lab_operator_id"))
        ),
        "physical_stop_available": supervision.get("physical_stop_available") is True,
        "volume_settings_complete": (
            measurements.get("all_volume_settings_tested") is True
            and len(normalized_volume_settings) >= 1
            and len(set(normalized_volume_settings)) == len(normalized_volume_settings)
            and all(_portable(value) for value in normalized_volume_settings)
            and set(normalized_volume_settings) == set(volume_values)
            and nominal_volume_setting in normalized_volume_settings
        ),
        "each_volume_setting_tclw_at_least_46db": (
            bool(volume_values)
            and all(
                value is not None and value >= TCLW_ANY_VOLUME_MIN_DB
                for value in volume_values.values()
            )
        ),
        "reported_tclw_min_matches_per_setting_results": (
            tclw_any_volume is not None
            and computed_tclw_min is not None
            and math.isclose(tclw_any_volume, computed_tclw_min, abs_tol=1e-6)
        ),
        "reported_nominal_tclw_matches_nominal_setting": (
            tclw_nominal is not None
            and nominal_volume_setting in volume_values
            and volume_values[nominal_volume_setting] is not None
            and math.isclose(
                tclw_nominal,
                volume_values[nominal_volume_setting],
                abs_tol=1e-6,
            )
        ),
        "scenario_artifacts_are_distinct": (
            len(scenario_digests) == len(REQUIRED_SCENARIOS)
            and len(set(scenario_digests)) == len(REQUIRED_SCENARIOS)
        ),
        **scenario_checks,
        "tclw_nominal_at_least_46db": (
            tclw_nominal is not None and tclw_nominal >= TCLW_NOMINAL_MIN_DB
        ),
        "tclw_all_volume_settings_at_least_46db": (
            tclw_any_volume is not None and tclw_any_volume >= TCLW_ANY_VOLUME_MIN_DB
        ),
        "p340_type1_telrdt_at_least_37db": (
            telrdt is not None and telrdt >= P340_TYPE1_TELRDT_MIN_DB
        ),
        "engineering_erle_at_least_20db": (
            erle is not None and erle >= ENGINEERING_ERLE_MIN_DB
        ),
        "near_end_wer_delta_at_most_2pp": (
            near_baseline is not None and near_aec is not None
            and 0.0 <= near_baseline <= 1.0 and 0.0 <= near_aec <= 1.0
            and near_aec - near_baseline <= NEAR_END_WER_DELTA_MAX
        ),
        "double_talk_wer_delta_at_most_5pp": (
            double_baseline is not None and double_aec is not None
            and 0.0 <= double_baseline <= 1.0 and 0.0 <= double_aec <= 1.0
            and double_aec - double_baseline <= DOUBLE_TALK_WER_DELTA_MAX
        ),
        "barge_in_p95_at_most_200ms": (
            barge_in is not None and 0.0 <= barge_in <= BARGE_IN_P95_MAX_MS
        ),
        "engineering_erle_not_labeled_tclw": (
            measurements.get("engineering_erle_is_not_tclw") is True
        ),
        "report_hmac_verified": _mapping(bindings.get("report")).get(
            "hmac_sha256_verified") is True,
    }
    acoustic_gate_passed = all(checks.values())
    return {
        "schema_version": 1,
        "policy_id": POLICY_ID,
        "policy_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "evidence_class": "go2_aec_hats_authenticated_lab_certificate",
        "inputs": dict(bindings),
        "thresholds": {
            "tclw_nominal_min_db": TCLW_NOMINAL_MIN_DB,
            "tclw_all_volume_settings_min_db": TCLW_ANY_VOLUME_MIN_DB,
            "tclw_recommended_objective_db": TCLW_RECOMMENDED_DB,
            "p340_type1_telrdt_min_db": P340_TYPE1_TELRDT_MIN_DB,
            "engineering_erle_min_db": ENGINEERING_ERLE_MIN_DB,
            "near_end_wer_delta_max": NEAR_END_WER_DELTA_MAX,
            "double_talk_wer_delta_max": DOUBLE_TALK_WER_DELTA_MAX,
            "barge_in_p95_max_ms": BARGE_IN_P95_MAX_MS,
        },
        "metrics": {
            "tclw_nominal_db": tclw_nominal,
            "tclw_min_across_volume_settings_db": tclw_any_volume,
            "telrdt_db": telrdt,
            "engineering_erle_db": erle,
            "near_end_wer_delta": (
                near_aec - near_baseline
                if near_aec is not None and near_baseline is not None else None
            ),
            "double_talk_wer_delta": (
                double_aec - double_baseline
                if double_aec is not None and double_baseline is not None else None
            ),
            "barge_in_p95_ms": barge_in,
            "scenarios": scenario_metrics,
        },
        "checks": checks,
        "failures": [name for name, passed in checks.items() if not passed],
        "report_hmac_verified": checks["report_hmac_verified"],
        "canceller_present": bool(
            checks["canceller_name_present"] and checks["canceller_version_present"]),
        "tclw_authorized": acoustic_gate_passed,
        "p340_authorized": acoustic_gate_passed,
        "aec_acoustic_gate_passed": acoustic_gate_passed,
        "passed": acoustic_gate_passed,
        "production_ready": False,
        "mic_enable_authorized": False,
        "speaker_enable_authorized": False,
        "control_authorized": False,
        "boundary": (
            "This certificate imports authenticated lab results; it does not calculate "
            "TCLw/TELRDT from WAV files. Passing qualifies only the AEC acoustic evidence "
            "lane. Full audio production promotion still requires transport, firmware, "
            "live multilingual WER, soak, speaker, review, and release gates."
        ),
    }


def _write_new_private(path: str | Path, document: Mapping[str, object]) -> None:
    output = Path(path)
    if output.exists() or output.is_symlink():
        raise HatsCertificateError(f"output already exists; refusing overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write((json.dumps(document, indent=2, sort_keys=True) + "\n").encode())


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-manifest", required=True)
    parser.add_argument("--verification-key-file", required=True)
    parser.add_argument("--expected-key-id", required=True)
    parser.add_argument("--output", required=True)
    options = parser.parse_args(args)
    report, manifest, bindings = load_authenticated_campaign(
        options.campaign_manifest,
        verification_key_path=options.verification_key_file,
        expected_key_id=options.expected_key_id,
    )
    certificate = evaluate_hats_report(
        report, manifest=manifest, bindings=bindings)
    _write_new_private(options.output, certificate)
    print(json.dumps(certificate, indent=2, sort_keys=True))
    if not certificate["aec_acoustic_gate_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
