import pytest

from ferox_audio_g1.health import ERROR, OK, WARN, voice_health_report


def _report(**changes):
    values = dict(
        speaker_enabled=True,
        volume_confirmed=True,
        latched_fault=None,
        accepted_chunks=1,
        rejected_chunks=0,
        requests_ok=1,
        play_requests_ok=0,
        request_timeout_total=0,
        unitree_error_total=0,
        buffered_bytes=0,
        inflight_age_ms=-1.0,
    )
    values.update(changes)
    return voice_health_report(**values)


def test_ready_requires_api_evidence_and_enabled_speaker():
    assert _report().level == OK
    assert _report(volume_confirmed=False).level == WARN
    assert _report(speaker_enabled=False).level == WARN


def test_latched_fault_is_bounded_and_error():
    report = _report(latched_fault="timeout\n" + "x" * 300)
    assert report.level == ERROR
    assert len(dict(report.values)["last_fault"]) <= 160


@pytest.mark.parametrize("field,value", [
    ("rejected_chunks", -1),
    ("buffered_bytes", 10**16),
    ("inflight_age_ms", float("nan")),
])
def test_invalid_evidence_is_rejected(field, value):
    with pytest.raises(ValueError):
        _report(**{field: value})
