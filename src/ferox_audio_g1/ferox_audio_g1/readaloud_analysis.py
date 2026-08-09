"""ROS-free signal evidence for a supervised onsite read-aloud."""
from __future__ import annotations

import array
import math
import statistics


class ReadAloudEvidenceError(ValueError):
    pass


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def _dbfs(samples: array.array) -> float:
    if not samples:
        return -120.0
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    return -120.0 if rms <= 0.0 else 20.0 * math.log10(rms / 32_768.0)


def analyze_readaloud_pcm(
    pcm: bytes,
    *,
    expected_duration_s: int,
    sample_rate: int = 16_000,
    frame_ms: int = 100,
) -> dict[str, object]:
    if not 5 <= expected_duration_s <= 120:
        raise ReadAloudEvidenceError("expected_duration_s must be in [5, 120]")
    if sample_rate != 16_000 or frame_ms != 100:
        raise ReadAloudEvidenceError("only the 16 kHz/100 ms evidence contract is supported")
    if len(pcm) % 2:
        raise ReadAloudEvidenceError("S16LE payload has an odd byte count")
    samples = array.array("h")
    samples.frombytes(pcm)
    expected_samples = expected_duration_s * sample_rate
    if len(samples) != expected_samples:
        raise ReadAloudEvidenceError(
            f"capture is incomplete: expected {expected_samples}, got {len(samples)}")

    frame_samples = sample_rate * frame_ms // 1_000
    frames = [
        _dbfs(samples[index:index + frame_samples])
        for index in range(0, len(samples), frame_samples)
    ]
    noise_floor = _percentile(frames, 0.20)
    voice_threshold = max(-45.0, noise_floor + 12.0)
    voiced = [value >= voice_threshold for value in frames]
    segments: list[tuple[int, int]] = []
    start: int | None = None
    last_voice: int | None = None
    for index, active in enumerate(voiced):
        if active:
            if start is None:
                start = index
            last_voice = index
        elif start is not None and last_voice is not None and index - last_voice > 2:
            if last_voice - start + 1 >= 3:
                segments.append((start, last_voice + 1))
            start = None
            last_voice = None
    if start is not None and last_voice is not None and last_voice - start + 1 >= 3:
        segments.append((start, last_voice + 1))

    clipped = sum(abs(value) >= 32_760 for value in samples)
    voiced_duration_s = sum(voiced) * frame_ms / 1_000.0
    return {
        "capture_samples": len(samples),
        "capture_duration_s": len(samples) / sample_rate,
        "complete": True,
        "frame_ms": frame_ms,
        "noise_floor_p20_dbfs": round(noise_floor, 3),
        "frame_p50_dbfs": round(statistics.median(frames), 3),
        "frame_p95_dbfs": round(_percentile(frames, 0.95), 3),
        "peak_frame_dbfs": round(max(frames), 3),
        "voice_threshold_dbfs": round(voice_threshold, 3),
        "voiced_frame_count": sum(voiced),
        "voiced_duration_s": round(voiced_duration_s, 3),
        "speech_segments_s": [
            [round(start_index * frame_ms / 1_000.0, 3),
             round(end_index * frame_ms / 1_000.0, 3)]
            for start_index, end_index in segments
        ],
        "speech_detected": voiced_duration_s >= 1.0 and bool(segments),
        "clipped_sample_count": clipped,
        "clipped_sample_fraction": round(clipped / max(1, len(samples)), 9),
        "clipping_gate_passed": clipped / max(1, len(samples)) <= 0.0001,
    }

