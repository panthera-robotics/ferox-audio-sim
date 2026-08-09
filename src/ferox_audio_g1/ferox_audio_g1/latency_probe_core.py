"""Deterministic, bounded acoustic stimulus for onsite latency probes."""
from __future__ import annotations

import math
import struct

from .unitree_voice_contract import PCM_SAMPLE_RATE


def synthesize_chirp(
    *,
    duration_ms: int = 400,
    amplitude: float = 0.08,
    start_hz: float = 700.0,
    end_hz: float = 2400.0,
) -> bytes:
    """Return mono 16 kHz S16LE with a 20 ms fade at each edge.

    The conservative amplitude is intentional: the live probe combines it with
    a separately bounded hardware-volume setting.  A swept tone is easier to
    distinguish from normal room noise than a single-frequency beep.
    """
    if not 100 <= duration_ms <= 1_000:
        raise ValueError("duration_ms must be in [100, 1000]")
    if not 0.0 < amplitude <= 0.10 or not math.isfinite(amplitude):
        raise ValueError("amplitude must be finite and in (0, 0.10]")
    if not 100.0 <= start_hz < end_hz <= 4_000.0:
        raise ValueError("chirp frequencies are outside the safe probe range")

    sample_count = PCM_SAMPLE_RATE * duration_ms // 1_000
    duration_s = sample_count / PCM_SAMPLE_RATE
    fade_samples = min(PCM_SAMPLE_RATE // 50, sample_count // 4)
    frequency_slope = (end_hz - start_hz) / duration_s
    peak = int(round(32_767 * amplitude))
    output = bytearray()
    for index in range(sample_count):
        time_s = index / PCM_SAMPLE_RATE
        phase = 2.0 * math.pi * (
            start_hz * time_s + 0.5 * frequency_slope * time_s * time_s)
        envelope = 1.0
        if index < fade_samples:
            envelope = index / fade_samples
        elif index >= sample_count - fade_samples:
            envelope = (sample_count - 1 - index) / fade_samples
        sample = int(round(peak * max(0.0, envelope) * math.sin(phase)))
        output.extend(struct.pack("<h", sample))
    return bytes(output)

