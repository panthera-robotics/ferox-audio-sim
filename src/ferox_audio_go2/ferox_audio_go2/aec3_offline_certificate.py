"""Digest-bound engineering certificate for the native offline AEC3 runtime.

This evaluates ordinary PCM fixtures and is intentionally separate from the
authenticated HATS/TCLw qualification path.  Engineering ERLE and clean
near-end preservation metrics are useful regression signals, but they are not
ETSI TCLw, ITU-T P.340, or a production speaker authorization.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import sys
import wave
from collections.abc import Mapping
from pathlib import Path


POLICY_ID = "ferox-go2-aec3-offline-v1"
SAMPLE_RATE_HZ = 48_000
FRAME_SAMPLES = 480
MAXIMUM_DURATION_S = 600.0
ENGINEERING_ERLE_MIN_DB = 20.0
NEAR_END_SI_SDR_MIN_DB = 25.0
DOUBLE_TALK_SI_SDR_MIN_DB = 8.0
DOUBLE_TALK_SI_SDR_IMPROVEMENT_MIN_DB = 12.0
NEAR_END_GAIN_ABS_MAX_DB = 3.0
NEAR_END_ALIGNMENT_ABS_MAX_MS = 20.0
SCENARIOS = {"far_end_single_talk", "near_end_single_talk", "double_talk"}
_REPORT_KEYS = {
    "aec_algorithm", "aec_enabled", "aec_profile", "agc_enabled", "audio_duration_s",
    "channels", "control_authorized", "delay_median_ms", "delay_ms",
    "delay_standard_deviation_ms", "divergent_filter_fraction",
    "echo_return_loss_db", "echo_return_loss_enhancement_db", "frame_count",
    "frame_duration_ms", "high_pass_filter_enabled",
    "noise_suppression_enabled", "offline_only", "pcm_format",
    "nearend_detection_enr_threshold", "nearend_mask_hf_enr_suppress",
    "nearend_mask_hf_enr_transparent", "nearend_mask_lf_enr_suppress",
    "nearend_mask_lf_enr_transparent",
    "processing_elapsed_s", "production_ready", "realtime_factor",
    "residual_echo_likelihood", "residual_echo_likelihood_recent_max",
    "sample_rate_hz", "schema_version", "speaker_enable_authorized",
    "speaker_or_audiohub_called", "stream_delay_ms", "tclw_claimed",
    "statistics_remote_tracks_assumed", "tuning_source",
    "webrtc_audio_processing_release", "webrtc_upstream_basis",
}


class Aec3OfflineCertificateError(ValueError):
    """Offline AEC3 input or runtime evidence is malformed."""


def _regular_bytes(path: str | Path, *, maximum_bytes: int) -> tuple[Path, bytes]:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise Aec3OfflineCertificateError(
            f"evidence must be a regular non-symlink file: {source}")
    payload = source.read_bytes()
    if not payload or len(payload) > maximum_bytes:
        raise Aec3OfflineCertificateError(f"evidence size is invalid: {source}")
    return source, payload


def _load_wav(path: str | Path) -> tuple[array.array, dict[str, object]]:
    source, payload = _regular_bytes(path, maximum_bytes=60_000_000)
    try:
        with wave.open(str(source), "rb") as handle:
            if (
                handle.getnchannels() != 1
                or handle.getsampwidth() != 2
                or handle.getframerate() != SAMPLE_RATE_HZ
                or handle.getcomptype() != "NONE"
            ):
                raise Aec3OfflineCertificateError(
                    f"WAV must be 48 kHz mono PCM16: {source}")
            frame_count = handle.getnframes()
            pcm = handle.readframes(frame_count)
    except (wave.Error, EOFError) as exc:
        raise Aec3OfflineCertificateError(f"invalid WAV: {source}") from exc
    if (
        frame_count <= 0
        or frame_count % FRAME_SAMPLES != 0
        or frame_count / SAMPLE_RATE_HZ > MAXIMUM_DURATION_S
        or len(pcm) != frame_count * 2
    ):
        raise Aec3OfflineCertificateError(
            f"WAV duration or 10 ms framing is invalid: {source}")
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples, {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "sample_count": len(samples),
        "duration_s": len(samples) / SAMPLE_RATE_HZ,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "channels": 1,
        "pcm_format": "S16_LE",
    }


def _load_report(path: str | Path) -> tuple[dict[str, object], dict[str, object]]:
    source, payload = _regular_bytes(path, maximum_bytes=1_000_000)
    try:
        report = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Aec3OfflineCertificateError("invalid AEC3 runtime report JSON") from exc
    if not isinstance(report, dict) or set(report) != _REPORT_KEYS:
        raise Aec3OfflineCertificateError("AEC3 runtime report schema is invalid")
    return report, {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _number(value: object, *, allow_null: bool = False) -> float | None:
    if value is None and allow_null:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Aec3OfflineCertificateError("runtime report numeric field is invalid")
    result = float(value)
    if not math.isfinite(result):
        raise Aec3OfflineCertificateError("runtime report numeric field is not finite")
    return result


def _energy(samples: array.array, start: int) -> float:
    count = len(samples) - start
    if count <= 0:
        raise Aec3OfflineCertificateError("metric window is empty")
    return sum(
        float(samples[index]) * float(samples[index])
        for index in range(start, len(samples))
    ) / count


def _rms_dbfs(samples: array.array, start: int) -> float:
    return 10.0 * math.log10(max(_energy(samples, start), 1e-12) / (32768.0 ** 2))


def _aligned_window(
    length: int, start: int, lag_samples: int
) -> tuple[int, int, int]:
    if lag_samples >= 0:
        reference_start = start
        estimate_start = start + lag_samples
    else:
        reference_start = start - lag_samples
        estimate_start = start
    count = min(length - reference_start, length - estimate_start)
    if count <= 0:
        raise Aec3OfflineCertificateError("aligned near-end metric window is empty")
    return reference_start, estimate_start, count


def _alignment_score(
    reference: array.array,
    estimate: array.array,
    *,
    start: int,
    lag_samples: int,
    stride: int,
) -> float:
    reference_start, estimate_start, available = _aligned_window(
        len(reference), start, lag_samples)
    count = min(available, 2 * SAMPLE_RATE_HZ)
    sum_reference = 0.0
    sum_estimate = 0.0
    sum_reference_squared = 0.0
    sum_estimate_squared = 0.0
    cross = 0.0
    observations = 0
    for offset in range(0, count, stride):
        reference_value = float(reference[reference_start + offset])
        estimate_value = float(estimate[estimate_start + offset])
        sum_reference += reference_value
        sum_estimate += estimate_value
        sum_reference_squared += reference_value * reference_value
        sum_estimate_squared += estimate_value * estimate_value
        cross += reference_value * estimate_value
        observations += 1
    covariance = cross - sum_reference * sum_estimate / observations
    reference_variance = (
        sum_reference_squared - sum_reference * sum_reference / observations)
    estimate_variance = (
        sum_estimate_squared - sum_estimate * sum_estimate / observations)
    denominator = math.sqrt(max(reference_variance * estimate_variance, 1e-24))
    return covariance / denominator


def _best_alignment_lag_samples(
    reference: array.array,
    estimate: array.array,
    *,
    start: int,
    maximum_abs_lag_samples: int = round(0.05 * SAMPLE_RATE_HZ),
) -> int:
    if len(reference) != len(estimate):
        raise Aec3OfflineCertificateError("near-end reference length mismatch")
    coarse_step = 16
    coarse = range(
        -maximum_abs_lag_samples,
        maximum_abs_lag_samples + 1,
        coarse_step,
    )
    coarse_best = max(
        coarse,
        key=lambda lag: _alignment_score(
            reference, estimate, start=start, lag_samples=lag, stride=16),
    )
    refinement = range(
        max(-maximum_abs_lag_samples, coarse_best - coarse_step),
        min(maximum_abs_lag_samples, coarse_best + coarse_step) + 1,
    )
    return max(
        refinement,
        key=lambda lag: _alignment_score(
            reference, estimate, start=start, lag_samples=lag, stride=8),
    )


def _gain_db(
    reference: array.array,
    estimate: array.array,
    start: int,
    lag_samples: int = 0,
) -> float:
    reference_start, estimate_start, count = _aligned_window(
        len(reference), start, lag_samples)
    reference_energy = sum(
        float(reference[index]) ** 2
        for index in range(reference_start, reference_start + count)
    ) / count
    estimate_energy = sum(
        float(estimate[index]) ** 2
        for index in range(estimate_start, estimate_start + count)
    ) / count
    return 10.0 * math.log10(
        max(estimate_energy, 1e-12) / max(reference_energy, 1e-12)
    )


def _si_sdr_db(
    reference: array.array,
    estimate: array.array,
    start: int,
    lag_samples: int = 0,
) -> float:
    if len(reference) != len(estimate):
        raise Aec3OfflineCertificateError("near-end reference length mismatch")
    reference_start, estimate_start, count = _aligned_window(
        len(reference), start, lag_samples)
    reference_mean = sum(
        reference[index]
        for index in range(reference_start, reference_start + count)
    ) / count
    estimate_mean = sum(
        estimate[index]
        for index in range(estimate_start, estimate_start + count)
    ) / count
    reference_energy = 0.0
    cross = 0.0
    for offset in range(count):
        reference_value = reference[reference_start + offset]
        estimate_value = estimate[estimate_start + offset]
        centered_reference = float(reference_value) - reference_mean
        centered_estimate = float(estimate_value) - estimate_mean
        reference_energy += centered_reference * centered_reference
        cross += centered_reference * centered_estimate
    if reference_energy <= 0.0:
        raise Aec3OfflineCertificateError("clean near-end reference has zero energy")
    scale = cross / reference_energy
    target_energy = 0.0
    residual_energy = 0.0
    for offset in range(count):
        reference_value = reference[reference_start + offset]
        estimate_value = estimate[estimate_start + offset]
        target = scale * (float(reference_value) - reference_mean)
        residual = (float(estimate_value) - estimate_mean) - target
        target_energy += target * target
        residual_energy += residual * residual
    return 10.0 * math.log10(max(target_energy, 1e-12) / max(residual_energy, 1e-12))


def certify_aec3_offline(
    *,
    scenario: str,
    render: array.array,
    capture: array.array,
    output: array.array,
    clean_near_end: array.array | None,
    runtime_report: Mapping[str, object],
    bindings: Mapping[str, object],
    convergence_skip_s: float,
) -> dict[str, object]:
    if scenario not in SCENARIOS:
        raise Aec3OfflineCertificateError("AEC3 scenario is invalid")
    if len(render) != len(capture) or len(output) != len(capture):
        raise Aec3OfflineCertificateError("AEC3 WAV sample counts differ")
    if clean_near_end is not None and len(clean_near_end) != len(capture):
        raise Aec3OfflineCertificateError("clean near-end sample count differs")
    if scenario == "double_talk" and clean_near_end is None:
        raise Aec3OfflineCertificateError(
            "double_talk requires an independent clean near-end reference")
    duration_s = len(capture) / SAMPLE_RATE_HZ
    if (
        not math.isfinite(convergence_skip_s)
        or convergence_skip_s < 0.0
        or convergence_skip_s >= duration_s
    ):
        raise Aec3OfflineCertificateError("convergence skip is outside the recording")
    start = round(convergence_skip_s * SAMPLE_RATE_HZ)
    frame_count = len(capture) // FRAME_SAMPLES
    if (
        runtime_report.get("schema_version") != 1
        or runtime_report.get("aec_algorithm") != "WebRTC AEC3"
        or runtime_report.get("aec_enabled") is not True
        or runtime_report.get("aec_profile") != "default"
        or runtime_report.get("webrtc_audio_processing_release") != "2.1"
        or runtime_report.get("webrtc_upstream_basis") != "M131"
        or runtime_report.get("offline_only") is not True
        or runtime_report.get("production_ready") is not False
        or runtime_report.get("tclw_claimed") is not False
        or runtime_report.get("speaker_enable_authorized") is not False
        or runtime_report.get("speaker_or_audiohub_called") is not False
        or runtime_report.get("statistics_remote_tracks_assumed") is not True
        or runtime_report.get("control_authorized") is not False
        or runtime_report.get("agc_enabled") is not False
        or runtime_report.get("noise_suppression_enabled") is not False
        or runtime_report.get("high_pass_filter_enabled") is not False
        or runtime_report.get("sample_rate_hz") != SAMPLE_RATE_HZ
        or runtime_report.get("channels") != 1
        or runtime_report.get("pcm_format") != "S16_LE"
        or runtime_report.get("frame_duration_ms") != 10
        or runtime_report.get("frame_count") != frame_count
        or abs((_number(runtime_report.get("audio_duration_s")) or 0.0) - duration_s) > 1e-6
        or (_number(runtime_report.get("processing_elapsed_s")) or 0.0) <= 0.0
        or (_number(runtime_report.get("realtime_factor")) or 0.0) <= 0.0
    ):
        raise Aec3OfflineCertificateError("AEC3 runtime identity or safety boundary is invalid")
    for field in (
        "delay_median_ms", "delay_ms", "delay_standard_deviation_ms",
        "divergent_filter_fraction", "echo_return_loss_db",
        "echo_return_loss_enhancement_db", "residual_echo_likelihood",
        "residual_echo_likelihood_recent_max",
        "nearend_detection_enr_threshold", "nearend_mask_hf_enr_suppress",
        "nearend_mask_hf_enr_transparent", "nearend_mask_lf_enr_suppress",
        "nearend_mask_lf_enr_transparent",
    ):
        _number(runtime_report.get(field), allow_null=True)
    expected_tuning_source = "WebRTC M131 default EchoCanceller3Config"
    if runtime_report.get("tuning_source") != expected_tuning_source:
        raise Aec3OfflineCertificateError("AEC3 tuning source is invalid")

    metrics: dict[str, object] = {
        "convergence_skip_s": convergence_skip_s,
        "capture_rms_dbfs": _rms_dbfs(capture, start),
        "output_rms_dbfs": _rms_dbfs(output, start),
        "render_rms_dbfs": _rms_dbfs(render, start),
        "engineering_erle_db": None,
        "input_near_end_si_sdr_db": None,
        "near_end_si_sdr_db": None,
        "near_end_si_sdr_improvement_db": None,
        "near_end_gain_db": None,
        "near_end_alignment_lag_ms": None,
    }
    checks: dict[str, bool] = {
        "runtime_identity_and_safety_boundary_valid": True,
        "wav_lengths_and_formats_match": True,
    }
    if scenario == "far_end_single_talk":
        erle_db = 10.0 * math.log10(
            max(_energy(capture, start), 1e-12) /
            max(_energy(output, start), 1e-12)
        )
        metrics["engineering_erle_db"] = erle_db
        checks["engineering_erle_at_least_20db"] = erle_db >= ENGINEERING_ERLE_MIN_DB
    else:
        reference = capture if scenario == "near_end_single_talk" else clean_near_end
        assert reference is not None
        lag_samples = _best_alignment_lag_samples(
            reference, output, start=start)
        lag_ms = 1000.0 * lag_samples / SAMPLE_RATE_HZ
        si_sdr_db = _si_sdr_db(reference, output, start, lag_samples)
        gain_db = _gain_db(reference, output, start, lag_samples)
        metrics["near_end_si_sdr_db"] = si_sdr_db
        metrics["near_end_gain_db"] = gain_db
        metrics["near_end_alignment_lag_ms"] = lag_ms
        if scenario == "near_end_single_talk":
            checks["near_end_si_sdr_at_least_25db"] = (
                si_sdr_db >= NEAR_END_SI_SDR_MIN_DB)
        else:
            input_si_sdr_db = _si_sdr_db(reference, capture, start)
            improvement_db = si_sdr_db - input_si_sdr_db
            metrics["input_near_end_si_sdr_db"] = input_si_sdr_db
            metrics["near_end_si_sdr_improvement_db"] = improvement_db
            checks["double_talk_output_si_sdr_at_least_8db"] = (
                si_sdr_db >= DOUBLE_TALK_SI_SDR_MIN_DB)
            checks["double_talk_si_sdr_improvement_at_least_12db"] = (
                improvement_db >= DOUBLE_TALK_SI_SDR_IMPROVEMENT_MIN_DB)
        checks["near_end_gain_within_3db"] = abs(gain_db) <= NEAR_END_GAIN_ABS_MAX_DB
        checks["near_end_alignment_lag_at_most_20ms"] = (
            abs(lag_ms) <= NEAR_END_ALIGNMENT_ABS_MAX_MS)

    offline_functional_gate_passed = all(checks.values())
    return {
        "schema_version": 1,
        "policy_id": POLICY_ID,
        "policy_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "evidence_class": "webrtc_aec3_offline_engineering",
        "scenario": scenario,
        "inputs": dict(bindings),
        "runtime": dict(runtime_report),
        "thresholds": {
            "engineering_erle_min_db": ENGINEERING_ERLE_MIN_DB,
            "near_end_si_sdr_min_db": NEAR_END_SI_SDR_MIN_DB,
            "double_talk_si_sdr_min_db": DOUBLE_TALK_SI_SDR_MIN_DB,
            "double_talk_si_sdr_improvement_min_db": (
                DOUBLE_TALK_SI_SDR_IMPROVEMENT_MIN_DB),
            "near_end_gain_abs_max_db": NEAR_END_GAIN_ABS_MAX_DB,
            "near_end_alignment_abs_max_ms": NEAR_END_ALIGNMENT_ABS_MAX_MS,
        },
        "metrics": metrics,
        "checks": checks,
        "failures": [name for name, passed in checks.items() if not passed],
        "runtime_integrity_passed": True,
        "offline_functional_gate_passed": offline_functional_gate_passed,
        "passed": offline_functional_gate_passed,
        "engineering_erle_is_not_tclw": True,
        "hats_qualified": False,
        "tclw_qualified": False,
        "production_ready": False,
        "mic_enable_authorized": False,
        "speaker_enable_authorized": False,
        "control_authorized": False,
        "boundary": (
            "Offline 48 kHz PCM engineering evidence only. ERLE and SI-SDR are "
            "regression metrics, not ETSI TCLw, ITU-T P.340 double-talk, HATS, "
            "real acoustic-loop, or live Go2 speaker qualification."
        ),
    }


def _write_new_private(path: str | Path, document: Mapping[str, object]) -> None:
    output = Path(path)
    if output.exists() or output.is_symlink():
        raise Aec3OfflineCertificateError(
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
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    parser.add_argument("--render-wav", required=True)
    parser.add_argument("--capture-wav", required=True)
    parser.add_argument("--output-wav", required=True)
    parser.add_argument("--runtime-report", required=True)
    parser.add_argument("--clean-near-end-wav")
    parser.add_argument("--convergence-skip-s", type=float, default=2.0)
    parser.add_argument("--output", required=True)
    options = parser.parse_args(args)
    render, render_binding = _load_wav(options.render_wav)
    capture, capture_binding = _load_wav(options.capture_wav)
    output, output_binding = _load_wav(options.output_wav)
    report, report_binding = _load_report(options.runtime_report)
    clean = None
    bindings: dict[str, object] = {
        "render_wav": render_binding,
        "capture_wav": capture_binding,
        "output_wav": output_binding,
        "runtime_report": report_binding,
    }
    if options.clean_near_end_wav:
        clean, clean_binding = _load_wav(options.clean_near_end_wav)
        bindings["clean_near_end_wav"] = clean_binding
    certificate = certify_aec3_offline(
        scenario=options.scenario,
        render=render,
        capture=capture,
        output=output,
        clean_near_end=clean,
        runtime_report=report,
        bindings=bindings,
        convergence_skip_s=options.convergence_skip_s,
    )
    _write_new_private(options.output, certificate)
    print(json.dumps(certificate, indent=2, sort_keys=True))
    if not certificate["offline_functional_gate_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
