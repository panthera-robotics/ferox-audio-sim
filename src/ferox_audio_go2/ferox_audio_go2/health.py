"""Strict runtime health contract for the Go2 audio adapter."""
from __future__ import annotations

from dataclasses import dataclass
import math


OK = 0
WARN = 1
ERROR = 2
COUNTERS = (
    "source_frames_total", "source_frames_rejected_total", "mic_chunks_total",
    "mic_discontinuities_total", "speaker_chunks_rejected_total",
    "speaker_uploads_completed_total", "audiohub_responses_ok_total",
)
BOOLEANS = (
    "ready", "mic_enabled", "speaker_enabled", "profile_evidence_valid",
    "speaker_evidence_valid", "mic_stream_live", "audiohub_busy",
)
TEXT = (
    "schema_version", "hardware_profile", "runtime_firmware",
    "evidence_sha256", "last_fault",
)
TIMINGS = (
    "last_source_age_ms", "decode_p50_ms", "decode_p95_ms",
    "decode_p99_ms", "decode_max_ms", "source_to_chunk_p50_ms",
    "source_to_chunk_p95_ms", "source_to_chunk_p99_ms",
    "source_to_chunk_max_ms",
)
HEALTH_KEYS = frozenset((*COUNTERS, *BOOLEANS, *TEXT, *TIMINGS))


@dataclass(frozen=True)
class AudioHealthReport:
    level: int
    message: str
    values: tuple[tuple[str, str], ...]


def audio_health_report(
    *,
    mic_enabled: bool,
    speaker_enabled: bool,
    profile_evidence_valid: bool,
    speaker_evidence_valid: bool,
    mic_stream_live: bool,
    audiohub_busy: bool,
    hardware_profile: str,
    runtime_firmware: str,
    evidence_sha256: str,
    last_fault: str | None,
    last_source_age_ms: float,
    decode_p50_ms: float = -1.0,
    decode_p95_ms: float = -1.0,
    decode_p99_ms: float = -1.0,
    decode_max_ms: float = -1.0,
    source_to_chunk_p50_ms: float = -1.0,
    source_to_chunk_p95_ms: float = -1.0,
    source_to_chunk_p99_ms: float = -1.0,
    source_to_chunk_max_ms: float = -1.0,
    counters: dict[str, int] | None = None,
) -> AudioHealthReport:
    counters = counters or {}
    if set(counters) != set(COUNTERS):
        raise ValueError("Go2 audio health counters do not match the schema")
    if any(isinstance(value, bool) or not isinstance(value, int)
           or not 0 <= value <= 10**15 for value in counters.values()):
        raise ValueError("Go2 audio health counters are invalid")
    age = float(last_source_age_ms)
    timing_values = {
        "last_source_age_ms": age,
        "decode_p50_ms": float(decode_p50_ms),
        "decode_p95_ms": float(decode_p95_ms),
        "decode_p99_ms": float(decode_p99_ms),
        "decode_max_ms": float(decode_max_ms),
        "source_to_chunk_p50_ms": float(source_to_chunk_p50_ms),
        "source_to_chunk_p95_ms": float(source_to_chunk_p95_ms),
        "source_to_chunk_p99_ms": float(source_to_chunk_p99_ms),
        "source_to_chunk_max_ms": float(source_to_chunk_max_ms),
    }
    for name, value in timing_values.items():
        if not math.isfinite(value) or not (
                -1.0 <= value <= 600_000.0):
            raise ValueError(f"{name} is invalid")
    fault = " ".join(str(last_fault or "").split())[:160]
    requested = bool(mic_enabled or speaker_enabled)
    capabilities_ready = (
        bool(profile_evidence_valid)
        and (not mic_enabled or mic_stream_live)
        and (not speaker_enabled or speaker_evidence_valid)
    )
    if fault:
        level, message = ERROR, "Go2 audio adapter latched fail-closed"
    elif not requested:
        level, message = WARN, "Go2 microphone and speaker disabled by policy"
    elif not capabilities_ready:
        level, message = WARN, "waiting for qualified Go2 audio runtime evidence"
    else:
        level, message = OK, "Go2 audio adapter healthy"
    values = (
        ("schema_version", "2"),
        ("hardware_profile", str(hardware_profile)[:128]),
        ("runtime_firmware", str(runtime_firmware)[:128]),
        ("evidence_sha256", str(evidence_sha256)[:64]),
        ("last_fault", fault),
        ("last_source_age_ms", f"{age:.3f}"),
        *((name, f"{timing_values[name]:.3f}")
          for name in TIMINGS if name != "last_source_age_ms"),
        ("ready", str(level == OK).lower()),
        ("mic_enabled", str(bool(mic_enabled)).lower()),
        ("speaker_enabled", str(bool(speaker_enabled)).lower()),
        ("profile_evidence_valid", str(bool(profile_evidence_valid)).lower()),
        ("speaker_evidence_valid", str(bool(speaker_evidence_valid)).lower()),
        ("mic_stream_live", str(bool(mic_stream_live)).lower()),
        ("audiohub_busy", str(bool(audiohub_busy)).lower()),
        *((name, str(counters[name])) for name in COUNTERS),
    )
    return AudioHealthReport(level=level, message=message, values=values)


def validate_audio_diagnostic(message, *, robot_id: str) -> str | None:
    statuses = list(getattr(message, "status", ()))
    if len(statuses) != 1:
        return "Go2 audio diagnostic must contain exactly one status"
    status = statuses[0]
    try:
        level = int(status.level[0] if isinstance(status.level, bytes) else status.level)
    except (TypeError, ValueError, IndexError):
        return "Go2 audio diagnostic level is invalid"
    if level not in (0, 1, 2, 3):
        return "Go2 audio diagnostic level is invalid"
    if str(getattr(status, "name", "")) != f"ferox/{robot_id}/audio":
        return "Go2 audio diagnostic name mismatch"
    if str(getattr(status, "hardware_id", "")) != robot_id:
        return "Go2 audio diagnostic hardware ID mismatch"
    summary = str(getattr(status, "message", ""))
    if not summary or len(summary) > 256 or any(char in summary for char in "\r\n\x00"):
        return "Go2 audio diagnostic summary is invalid"
    values: dict[str, str] = {}
    for pair in list(getattr(status, "values", ())):
        key, value = str(pair.key), str(pair.value)
        if key in values or key not in HEALTH_KEYS or len(value) > 160 or "\x00" in value:
            return "Go2 audio diagnostic key or value is invalid"
        values[key] = value
    if set(values) != HEALTH_KEYS or values.get("schema_version") != "2":
        return "Go2 audio diagnostic schema mismatch"
    for key in BOOLEANS:
        if values[key] not in {"true", "false"}:
            return f"Go2 audio diagnostic {key} is not boolean"
    if (level == OK) != (values["ready"] == "true"):
        return "Go2 audio readiness contradicts diagnostic level"
    for key in COUNTERS:
        try:
            value = int(values[key])
        except ValueError:
            return f"Go2 audio diagnostic {key} is not an integer"
        if not 0 <= value <= 10**15:
            return f"Go2 audio diagnostic {key} is outside bounds"
    for key in TIMINGS:
        try:
            value = float(values[key])
        except ValueError:
            return f"Go2 audio {key} is not numeric"
        if not math.isfinite(value) or not -1.0 <= value <= 600_000.0:
            return f"Go2 audio {key} is outside bounds"
    return None
