import json
import math
import random
import wave

import pytest

from ferox_audio_go2.aec3_offline_certificate import (
    Aec3OfflineCertificateError,
    _load_report,
    _load_wav,
    certify_aec3_offline,
    main,
)


RATE = 48_000


def write_wav(path, samples):
    payload = bytearray()
    for sample in samples:
        payload.extend(int(sample).to_bytes(2, "little", signed=True))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(payload)


def runtime_report(frame_count):
    duration = frame_count * 0.01
    return {
        "aec_algorithm": "WebRTC AEC3",
        "aec_enabled": True,
        "aec_profile": "default",
        "agc_enabled": False,
        "audio_duration_s": duration,
        "channels": 1,
        "control_authorized": False,
        "delay_median_ms": None,
        "delay_ms": 0,
        "delay_standard_deviation_ms": None,
        "divergent_filter_fraction": 0.0,
        "echo_return_loss_db": 6.0,
        "echo_return_loss_enhancement_db": 30.0,
        "frame_count": frame_count,
        "frame_duration_ms": 10,
        "high_pass_filter_enabled": False,
        "noise_suppression_enabled": False,
        "nearend_detection_enr_threshold": 0.25,
        "nearend_mask_hf_enr_suppress": 0.3,
        "nearend_mask_hf_enr_transparent": 0.1,
        "nearend_mask_lf_enr_suppress": 1.1,
        "nearend_mask_lf_enr_transparent": 1.09,
        "offline_only": True,
        "pcm_format": "S16_LE",
        "processing_elapsed_s": duration / 20.0,
        "production_ready": False,
        "realtime_factor": 0.05,
        "residual_echo_likelihood": 0.01,
        "residual_echo_likelihood_recent_max": 0.02,
        "sample_rate_hz": RATE,
        "schema_version": 1,
        "speaker_enable_authorized": False,
        "speaker_or_audiohub_called": False,
        "statistics_remote_tracks_assumed": True,
        "stream_delay_ms": 0,
        "tclw_claimed": False,
        "tuning_source": "WebRTC M131 default EchoCanceller3Config",
        "webrtc_audio_processing_release": "2.1",
        "webrtc_upstream_basis": "M131",
    }


def fixture_samples(seconds=4):
    count = RATE * seconds
    render = [round(10_000 * math.sin(2 * math.pi * 431 * i / RATE)) for i in range(count)]
    near = [round(6_000 * math.sin(2 * math.pi * 997 * i / RATE)) for i in range(count)]
    return render, near


def load_bundle(tmp_path, *, scenario):
    render, near = fixture_samples()
    if scenario == "far_end_single_talk":
        capture = [value // 2 for value in render]
        output = [value // 200 for value in render]
        clean = None
    elif scenario == "near_end_single_talk":
        capture = near
        output = [value + (index % 3 - 1) for index, value in enumerate(near)]
        clean = None
    else:
        capture = [near_value + render_value // 2
                   for near_value, render_value in zip(near, render)]
        output = [near_value + render_value // 200
                  for near_value, render_value in zip(near, render)]
        clean = near
    paths = {}
    for name, samples in (
        ("render", render), ("capture", capture), ("output", output)
    ):
        paths[name] = tmp_path / f"{name}.wav"
        write_wav(paths[name], samples)
    paths["report"] = tmp_path / "runtime.json"
    paths["report"].write_text(json.dumps(runtime_report(len(render) // 480)))
    loaded = {}
    bindings = {}
    for name in ("render", "capture", "output"):
        loaded[name], bindings[f"{name}_wav"] = _load_wav(paths[name])
    report, bindings["runtime_report"] = _load_report(paths["report"])
    loaded_clean = None
    if clean is not None:
        paths["clean"] = tmp_path / "clean.wav"
        write_wav(paths["clean"], clean)
        loaded_clean, bindings["clean_near_end_wav"] = _load_wav(paths["clean"])
    certificate = certify_aec3_offline(
        scenario=scenario,
        render=loaded["render"],
        capture=loaded["capture"],
        output=loaded["output"],
        clean_near_end=loaded_clean,
        runtime_report=report,
        bindings=bindings,
        convergence_skip_s=1.0,
    )
    return certificate, paths


def test_far_end_engineering_erle_passes_without_tclw_claim(tmp_path):
    certificate, _ = load_bundle(tmp_path, scenario="far_end_single_talk")
    assert certificate["metrics"]["engineering_erle_db"] > 30.0
    assert certificate["offline_functional_gate_passed"] is True
    assert certificate["engineering_erle_is_not_tclw"] is True
    assert certificate["hats_qualified"] is False
    assert certificate["tclw_qualified"] is False
    assert certificate["production_ready"] is False
    assert certificate["speaker_enable_authorized"] is False


def test_double_talk_clean_reference_measures_near_end_preservation(tmp_path):
    certificate, _ = load_bundle(tmp_path, scenario="double_talk")
    assert certificate["metrics"]["near_end_si_sdr_db"] > 8.0
    assert certificate["metrics"]["near_end_si_sdr_improvement_db"] > 12.0
    assert abs(certificate["metrics"]["near_end_gain_db"]) < 1.0
    assert certificate["offline_functional_gate_passed"] is True


def test_near_end_metric_reports_bounded_algorithmic_delay(tmp_path):
    source = random.Random(42)
    near = [round(6_000 * source.uniform(-1.0, 1.0)) for _ in range(4 * RATE)]
    lag_samples = round(0.016 * RATE)
    delayed = [0] * lag_samples + near[:-lag_samples]
    silence = [0] * len(near)
    loaded = []
    bindings = {}
    for name, samples in (("render", silence), ("capture", near), ("output", delayed)):
        path = tmp_path / f"{name}.wav"
        write_wav(path, samples)
        values, binding = _load_wav(path)
        loaded.append(values)
        bindings[f"{name}_wav"] = binding
    report = runtime_report(len(near) // 480)
    certificate = certify_aec3_offline(
        scenario="near_end_single_talk",
        render=loaded[0], capture=loaded[1], output=loaded[2],
        clean_near_end=None, runtime_report=report, bindings=bindings,
        convergence_skip_s=1.0,
    )
    assert certificate["metrics"]["near_end_alignment_lag_ms"] == pytest.approx(16.0)
    assert certificate["metrics"]["near_end_si_sdr_db"] > 100.0
    assert certificate["checks"]["near_end_alignment_lag_at_most_20ms"] is True
    assert certificate["offline_functional_gate_passed"] is True


def test_double_talk_without_clean_reference_is_rejected(tmp_path):
    certificate, paths = load_bundle(tmp_path, scenario="double_talk")
    assert certificate["passed"] is True
    render, _ = _load_wav(paths["render"])
    capture, _ = _load_wav(paths["capture"])
    output, _ = _load_wav(paths["output"])
    report, _ = _load_report(paths["report"])
    with pytest.raises(Aec3OfflineCertificateError, match="clean near-end"):
        certify_aec3_offline(
            scenario="double_talk",
            render=render,
            capture=capture,
            output=output,
            clean_near_end=None,
            runtime_report=report,
            bindings={},
            convergence_skip_s=1.0,
        )


def test_runtime_report_cannot_claim_tclw_or_speaker_authority(tmp_path):
    report_path = tmp_path / "runtime.json"
    report = runtime_report(400)
    report["tclw_claimed"] = True
    report_path.write_text(json.dumps(report))
    loaded, _ = _load_report(report_path)
    render, near = fixture_samples()
    paths = []
    for index, samples in enumerate((render, near, near)):
        path = tmp_path / f"{index}.wav"
        write_wav(path, samples)
        paths.append(_load_wav(path)[0])
    with pytest.raises(Aec3OfflineCertificateError, match="safety boundary"):
        certify_aec3_offline(
            scenario="near_end_single_talk",
            render=paths[0], capture=paths[1], output=paths[2],
            clean_near_end=None, runtime_report=loaded, bindings={},
            convergence_skip_s=1.0,
        )


def test_cli_binds_inputs_and_refuses_overwrite(tmp_path):
    _, paths = load_bundle(tmp_path, scenario="far_end_single_talk")
    certificate_path = tmp_path / "certificate.json"
    arguments = [
        "--scenario", "far_end_single_talk",
        "--render-wav", str(paths["render"]),
        "--capture-wav", str(paths["capture"]),
        "--output-wav", str(paths["output"]),
        "--runtime-report", str(paths["report"]),
        "--convergence-skip-s", "1",
        "--output", str(certificate_path),
    ]
    main(arguments)
    document = json.loads(certificate_path.read_text())
    assert set(document["inputs"]) == {
        "render_wav", "capture_wav", "output_wav", "runtime_report"
    }
    with pytest.raises(Aec3OfflineCertificateError, match="already exists"):
        main(arguments)


def test_wav_symlink_is_rejected(tmp_path):
    render, _ = fixture_samples(seconds=1)
    target = tmp_path / "target.wav"
    write_wav(target, render)
    link = tmp_path / "link.wav"
    link.symlink_to(target)
    with pytest.raises(Aec3OfflineCertificateError, match="non-symlink"):
        _load_wav(link)
