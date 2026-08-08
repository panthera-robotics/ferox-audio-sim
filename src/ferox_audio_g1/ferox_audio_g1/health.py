"""ROS-free health contract for the G1 Unitree voice adapter."""
from __future__ import annotations

from dataclasses import dataclass
import math


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
    inflight_age_ms: float,
) -> VoiceHealthReport:
    counters = {
        "accepted_chunks_total": accepted_chunks,
        "rejected_chunks_total": rejected_chunks,
        "requests_ok_total": requests_ok,
        "play_requests_ok_total": play_requests_ok,
        "request_timeout_total": request_timeout_total,
        "unitree_error_total": unitree_error_total,
        "buffered_bytes": buffered_bytes,
    }
    if any(not isinstance(value, int) or isinstance(value, bool)
           or not 0 <= value <= 10**15 for value in counters.values()):
        raise ValueError("voice health counters are invalid")
    inflight_age_ms = float(inflight_age_ms)
    if (not math.isfinite(inflight_age_ms)
            or not -1.0 <= inflight_age_ms <= 600_000.0):
        raise ValueError("inflight_age_ms is invalid")
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
        ("schema_version", "1"),
        ("ready", str(level == OK).lower()),
        ("speaker_enabled", str(bool(speaker_enabled)).lower()),
        ("microphone_available", "false"),
        ("volume_confirmed", str(bool(volume_confirmed)).lower()),
        *((key, str(value)) for key, value in counters.items()),
        ("inflight_age_ms", f"{inflight_age_ms:.3f}"),
        ("last_fault", fault),
    )
    return VoiceHealthReport(level=level, message=message, values=tuple(values))
