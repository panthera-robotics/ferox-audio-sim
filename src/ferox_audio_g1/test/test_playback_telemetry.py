import pytest

from ferox_audio_g1.pcm_gate import PcmRequestEvidence
from ferox_audio_g1.playback_telemetry import PlaybackTelemetry


def request(delay, reason="target", audio_ms=1000.0):
    return PcmRequestEvidence(
        flush_reason=reason,
        first_chunk_to_request_ms=delay,
        payload_bytes=int(audio_ms * 32),
        payload_audio_ms=audio_ms,
    )


def test_reports_bounded_dispatch_response_and_flush_metrics():
    telemetry = PlaybackTelemetry(window_size=4)
    for delay in (100.0, 200.0, 300.0, 400.0, 500.0):
        telemetry.record_dispatch(request(delay))
    telemetry.record_dispatch(request(25.0, reason="end", audio_ms=100.0))
    telemetry.record_dispatch(request(150.0, reason="idle", audio_ms=100.0))
    telemetry.record_play_response(12.5)
    snapshot = telemetry.snapshot()
    assert snapshot.dispatch_last_ms == 150.0
    assert snapshot.dispatch_p95_ms == 500.0
    assert snapshot.dispatch_max_ms == 500.0
    assert snapshot.response_p95_ms == 12.5
    assert snapshot.request_audio_last_ms == 100.0
    assert snapshot.target_flush_total == 5
    assert snapshot.end_flush_total == 1
    assert snapshot.idle_flush_total == 1


def test_rejects_unbounded_or_unknown_evidence():
    telemetry = PlaybackTelemetry()
    with pytest.raises(ValueError):
        telemetry.record_dispatch(request(float("nan")))
    with pytest.raises(ValueError, match="reason"):
        telemetry.record_dispatch(request(1.0, reason="unknown"))
    with pytest.raises(ValueError):
        PlaybackTelemetry(window_size=1)
