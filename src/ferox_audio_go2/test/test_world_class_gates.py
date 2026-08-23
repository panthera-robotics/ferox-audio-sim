import json

from ferox_audio_go2.aec_unavailable import (
    AecUnavailableError,
    aec_unavailable,
    refuse_engineering_erle_as_tclw,
)
from ferox_audio_go2.signal_metrics import (
    SignalMetricsError,
    analyze_pcm_s16le,
    assert_no_speech_claim,
)
from ferox_audio_go2.world_class_gates import evaluate_go2_hardware_production


def test_pcm_metrics_are_physical_and_never_claim_speech():
    pcm = bytes([0x00, 0x10, 0xFF, 0x7F, 0x00, 0x00, 0x00, 0x80])
    metrics = analyze_pcm_s16le(pcm, sample_rate=16_000)
    assert metrics["sample_count"] == 4
    assert metrics["peak_abs_s16"] == 32768
    assert metrics["clipped_samples"] == 2
    assert metrics["speech_claim_authorized"] is False
    assert metrics["operator_audio_intelligible"] is False
    assert_no_speech_claim(metrics)


def test_pcm_metrics_reject_odd_bytes_and_empty():
    try:
        analyze_pcm_s16le(b"\x00", sample_rate=16_000)
    except SignalMetricsError:
        pass
    else:
        raise AssertionError("odd PCM must fail")
    try:
        analyze_pcm_s16le(b"", sample_rate=16_000)
    except SignalMetricsError:
        return
    raise AssertionError("empty PCM must fail")


def test_go2_hardware_gate_fails_closed_on_template_evidence():
    evidence = json.loads("""
    {
      "control_authorized": false,
      "source_firmware": "REPLACE_WITH_APP_REPORTED_FIRMWARE",
      "codec_probe": {
        "operator_audio_intelligible": false,
        "operator_id": "REPLACE_AFTER_LISTENING",
        "capture_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "recording_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      },
      "observation": {
        "capture_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      },
      "speaker_probe": null
    }
    """)
    report = evaluate_go2_hardware_production(evidence)
    assert report["passed"] is False
    assert report["mic_enable_authorized"] is False
    assert report["speaker_enable_authorized"] is False
    assert report["control_authorized"] is False
    assert "firmware_fingerprint_present" in report["failures"]
    assert "operator_intelligible" in report["failures"]
    assert "live_speech_campaign_present" in report["failures"]
    assert "speaker_probe_supervised" in report["failures"]
    assert "aec_hats_campaign_present" in report["failures"]
    assert report["checks"]["aec_canceller_absent"] is True
    assert report["checks"]["aec_gates_missing_measurement"] is True
    assert report["checks"]["aec_tclw_not_claimed"] is True
    assert report["tclw_authorized"] is False
    aec = report["aec"]
    assert aec["canceller_present"] is False
    assert aec["speaker_enable_authorized"] is False
    assert aec["production_ready"] is False
    assert {gate["gate_id"] for gate in aec["gates"]} == {
        "aec_tclw_db", "aec_p340_telrdt_db", "aec_far_end_erle_db",
    }
    assert all(gate["reason"] == "missing_measurement" for gate in aec["gates"])
    assert all(gate["measured"] is None for gate in aec["gates"])


def test_aec_unavailable_ignores_injected_tclw_and_refuses_erle():
    stub = aec_unavailable({
        "tclw_db": 55.0,
        "telrdt_db": 40.0,
        "erle_db": 30.0,
        "hats_campaign": True,
        "control_authorized": True,
    })
    assert stub["interface"] == "aec_unavailable"
    assert stub["canceller_present"] is False
    assert stub["speaker_enable_authorized"] is False
    assert stub["production_ready"] is False
    assert stub["tclw_authorized"] is False
    assert stub["passed"] is False
    assert all(gate["reason"] == "missing_measurement" for gate in stub["gates"])
    try:
        refuse_engineering_erle_as_tclw(b"\xe8\x03" * 16, b"\x64\x00" * 16)
    except AecUnavailableError:
        return
    raise AssertionError("ERLE must not be accepted as TCLw")
