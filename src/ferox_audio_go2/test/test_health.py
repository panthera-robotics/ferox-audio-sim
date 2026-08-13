from types import SimpleNamespace

from ferox_audio_go2.health import (
    COUNTERS,
    audio_health_report,
    validate_audio_diagnostic,
)


def report(**overrides):
    options = dict(
        mic_enabled=True,
        speaker_enabled=True,
        profile_evidence_valid=True,
        speaker_evidence_valid=True,
        mic_stream_live=True,
        audiohub_busy=False,
        hardware_profile="go2_opus48_audiohub_v1",
        runtime_firmware="go2-fw-1.1.7",
        evidence_sha256="a" * 64,
        last_fault=None,
        last_source_age_ms=10.0,
        counters={key: 0 for key in COUNTERS},
    )
    options.update(overrides)
    return audio_health_report(**options)


def test_ready_requires_live_mic_and_qualified_speaker():
    assert report().level == 0
    assert report(mic_stream_live=False).level == 1
    assert report(speaker_evidence_valid=False).level == 1
    assert report(last_fault="timeout").level == 2


def test_diagnostic_schema_round_trip_and_tamper_rejection():
    health = report()
    status = SimpleNamespace(
        level=health.level,
        name="ferox/go2_02/audio",
        hardware_id="go2_02",
        message=health.message,
        values=[SimpleNamespace(key=key, value=value) for key, value in health.values],
    )
    message = SimpleNamespace(status=[status])
    assert validate_audio_diagnostic(message, robot_id="go2_02") is None
    status.values.pop()
    assert "schema" in validate_audio_diagnostic(message, robot_id="go2_02")
