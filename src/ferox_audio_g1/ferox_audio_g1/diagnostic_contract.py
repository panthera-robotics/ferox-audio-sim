"""Strict schema validation for G1 audio diagnostics crossing DDS domains."""
from __future__ import annotations

import math


COUNTERS = frozenset({
    "accepted_chunks_total", "rejected_chunks_total", "requests_ok_total",
    "play_requests_ok_total", "request_timeout_total", "unitree_error_total",
    "buffered_bytes", "target_flush_total", "end_flush_total", "idle_flush_total",
})
BOOLEANS = frozenset({
    "ready", "speaker_enabled", "microphone_available", "volume_confirmed",
})
KEYS = frozenset({
    "schema_version", "last_fault", *COUNTERS, *BOOLEANS,
    "buffered_audio_ms", "inflight_age_ms",
    "first_chunk_to_request_last_ms", "first_chunk_to_request_p95_ms",
    "first_chunk_to_request_max_ms", "play_response_latency_last_ms",
    "play_response_latency_p95_ms", "play_response_latency_max_ms",
    "request_audio_last_ms",
})
TIMINGS = KEYS - COUNTERS - BOOLEANS - {"schema_version", "last_fault"}


def _level_value(value) -> int:
    if isinstance(value, (bytes, bytearray)):
        if len(value) != 1:
            return -1
        return value[0]
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def validate_audio_diagnostic(message, *, robot_id: str) -> str | None:
    statuses = list(getattr(message, "status", ()))
    if len(statuses) != 1:
        return "audio diagnostic must contain exactly one status"
    status = statuses[0]
    level = _level_value(getattr(status, "level", -1))
    if level not in (0, 1, 2, 3):
        return "audio diagnostic level is invalid"
    if str(getattr(status, "name", "")) != f"ferox/{robot_id}/audio":
        return "audio diagnostic component name mismatch"
    if str(getattr(status, "hardware_id", "")) != robot_id:
        return "audio diagnostic hardware id mismatch"
    summary = str(getattr(status, "message", ""))
    if not summary or len(summary) > 256 or any(c in summary for c in "\r\n\x00"):
        return "audio diagnostic summary is invalid"
    values: dict[str, str] = {}
    for pair in list(getattr(status, "values", ())):
        key = str(getattr(pair, "key", ""))
        value = str(getattr(pair, "value", ""))
        if key in values:
            return "audio diagnostic contains a duplicate key"
        if key not in KEYS or len(value) > 160 or "\x00" in value:
            return "audio diagnostic key or value is invalid"
        values[key] = value
    if set(values) != set(KEYS) or values["schema_version"] != "2":
        return "audio diagnostic schema mismatch"
    for key in BOOLEANS:
        if values[key] not in {"true", "false"}:
            return f"audio diagnostic {key} is not boolean"
    if (level == 0) != (values["ready"] == "true"):
        return "audio diagnostic readiness contradicts level"
    for key in COUNTERS:
        try:
            value = int(values[key])
        except ValueError:
            return f"audio diagnostic {key} is not an integer"
        if not 0 <= value <= 10**15:
            return f"audio diagnostic {key} is outside bounds"
    for key in TIMINGS:
        try:
            timing = float(values[key])
        except ValueError:
            return f"audio diagnostic {key} is not numeric"
        if not math.isfinite(timing) or not -1.0 <= timing <= 600_000.0:
            return f"audio diagnostic {key} is outside bounds"
    if float(values["buffered_audio_ms"]) < 0.0:
        return "audio diagnostic buffered_audio_ms cannot be unavailable"
    return None
