"""ROS-free health contract for the G1 Unitree voice adapter."""
from __future__ import annotations

from dataclasses import dataclass
import math

from .playback_telemetry import PlaybackSnapshot


OK = 0
WARN = 1
ERROR = 2


@dataclass(frozen=True)
class VoiceHealthReport:
    level: int
    message: str
    values: tuple[tuple[str, str], ...]


def voice_health_report(
    *,
    speaker_enabled: bool,
    volume_confirmed: bool,
    latched_fault: str | None,
    accepted_chunks: int,
    rejected_chunks: int,
    requests_ok: int,
    play_requests_ok: int,
    request_timeout_total: int,
    unitree_error_total: int,
    buffered_bytes: int,
    buffered_audio_ms: float,
    inflight_age_ms: float,
    playback: PlaybackSnapshot,
) -> VoiceHealthReport:
    counters = {
        "accepted_chunks_total": accepted_chunks,
        "rejected_chunks_total": rejected_chunks,
        "requests_ok_total": requests_ok,
        "play_requests_ok_total": play_requests_ok,
        "request_timeout_total": request_timeout_total,
        "unitree_error_total": unitree_error_total,
        "buffered_bytes": buffered_bytes,
        "target_flush_total": playback.target_flush_total,
        "end_flush_total": playback.end_flush_total,
        "idle_flush_total": playback.idle_flush_total,
    }
    if any(not isinstance(value, int) or isinstance(value, bool)
           or not 0 <= value <= 10**15 for value in counters.values()):
        raise ValueError("voice health counters are invalid")
    timings = {
        "buffered_audio_ms": buffered_audio_ms,
        "inflight_age_ms": inflight_age_ms,
        "first_chunk_to_request_last_ms": playback.dispatch_last_ms,
        "first_chunk_to_request_p95_ms": playback.dispatch_p95_ms,
        "first_chunk_to_request_max_ms": playback.dispatch_max_ms,
        "play_response_latency_last_ms": playback.response_last_ms,
        "play_response_latency_p95_ms": playback.response_p95_ms,
        "play_response_latency_max_ms": playback.response_max_ms,
        "request_audio_last_ms": playback.request_audio_last_ms,
    }
    timings = {name: float(value) for name, value in timings.items()}
    if any(not math.isfinite(value) or not -1.0 <= value <= 600_000.0
           for value in timings.values()):
        raise ValueError("voice health timing is invalid")
    if timings["buffered_audio_ms"] < 0.0:
        raise ValueError("buffered_audio_ms cannot be unavailable")
    fault = " ".join(str(latched_fault or "").split())[:160]
    if fault:
        level, message = ERROR, "G1 voice adapter latched fail-closed"
    elif not volume_confirmed:
        level, message = WARN, "waiting for Unitree voice API readiness evidence"
    elif not speaker_enabled:
        level, message = WARN, "speaker disabled by deployment policy"
    else:
        level, message = OK, "G1 voice adapter healthy"
    values = (
        ("schema_version", "2"),
        ("ready", str(level == OK).lower()),
        ("speaker_enabled", str(bool(speaker_enabled)).lower()),
        ("microphone_available", "false"),
        ("volume_confirmed", str(bool(volume_confirmed)).lower()),
        *((key, str(value)) for key, value in counters.items()),
        *((key, f"{value:.3f}") for key, value in timings.items()),
        ("last_fault", fault),
    )
    return VoiceHealthReport(level=level, message=message, values=tuple(values))
