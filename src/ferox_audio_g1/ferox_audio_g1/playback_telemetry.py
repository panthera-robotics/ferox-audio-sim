"""Bounded, ROS-free latency telemetry for the G1 speaker request path."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math

from .pcm_gate import PcmRequestEvidence


def _p95(values: deque[float]) -> float:
    if not values:
        return -1.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


@dataclass(frozen=True)
class PlaybackSnapshot:
    dispatch_last_ms: float
    dispatch_p95_ms: float
    dispatch_max_ms: float
    response_last_ms: float
    response_p95_ms: float
    response_max_ms: float
    request_audio_last_ms: float
    target_flush_total: int
    end_flush_total: int
    idle_flush_total: int


class PlaybackTelemetry:
    def __init__(self, *, window_size: int = 256) -> None:
        if not isinstance(window_size, int) or isinstance(window_size, bool):
            raise ValueError("window_size must be an integer")
        if not 2 <= window_size <= 4096:
            raise ValueError("window_size must be in [2,4096]")
        self._dispatch: deque[float] = deque(maxlen=window_size)
        self._response: deque[float] = deque(maxlen=window_size)
        self._request_audio_last_ms = -1.0
        self._flush_totals = {"target": 0, "end": 0, "idle": 0}

    @staticmethod
    def _latency(value: float, name: str) -> float:
        value = float(value)
        if not math.isfinite(value) or not 0.0 <= value <= 600_000.0:
            raise ValueError(f"{name} must be finite and in [0,600000] ms")
        return value

    def record_dispatch(self, evidence: PcmRequestEvidence) -> None:
        if evidence.flush_reason not in self._flush_totals:
            raise ValueError("unknown PCM flush reason")
        delay = self._latency(
            evidence.first_chunk_to_request_ms, "first_chunk_to_request_ms")
        audio = self._latency(evidence.payload_audio_ms, "payload_audio_ms")
        if evidence.payload_bytes <= 0:
            raise ValueError("payload_bytes must be positive")
        self._dispatch.append(delay)
        self._request_audio_last_ms = audio
        self._flush_totals[evidence.flush_reason] += 1

    def record_play_response(self, latency_ms: float) -> None:
        self._response.append(self._latency(latency_ms, "play_response_latency_ms"))

    def snapshot(self) -> PlaybackSnapshot:
        return PlaybackSnapshot(
            dispatch_last_ms=self._dispatch[-1] if self._dispatch else -1.0,
            dispatch_p95_ms=_p95(self._dispatch),
            dispatch_max_ms=max(self._dispatch) if self._dispatch else -1.0,
            response_last_ms=self._response[-1] if self._response else -1.0,
            response_p95_ms=_p95(self._response),
            response_max_ms=max(self._response) if self._response else -1.0,
            request_audio_last_ms=self._request_audio_last_ms,
            target_flush_total=self._flush_totals["target"],
            end_flush_total=self._flush_totals["end"],
            idle_flush_total=self._flush_totals["idle"],
        )
