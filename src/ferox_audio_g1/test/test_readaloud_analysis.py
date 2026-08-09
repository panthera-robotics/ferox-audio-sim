import math
import struct

import pytest

from ferox_audio_g1.readaloud_analysis import (
    ReadAloudEvidenceError,
    analyze_readaloud_pcm,
)


def _pcm_with_speech(duration_s=5):
    samples = []
    for index in range(duration_s * 16_000):
        time_s = index / 16_000
        if 1.0 <= time_s < 3.0:
            value = round(8_000 * math.sin(2.0 * math.pi * 440.0 * time_s))
        else:
            value = round(20 * math.sin(2.0 * math.pi * 100.0 * time_s))
        samples.append(value)
    return struct.pack(f"<{len(samples)}h", *samples)


def test_readaloud_detects_bounded_speech_without_clipping():
    evidence = analyze_readaloud_pcm(_pcm_with_speech(), expected_duration_s=5)
    assert evidence["complete"] is True
    assert evidence["speech_detected"] is True
    assert evidence["voiced_duration_s"] >= 1.9
    assert evidence["clipping_gate_passed"] is True
    assert evidence["speech_segments_s"] == [[1.0, 3.0]]


def test_readaloud_rejects_truncated_and_misaligned_pcm():
    with pytest.raises(ReadAloudEvidenceError, match="incomplete"):
        analyze_readaloud_pcm(b"\x00\x00" * 10, expected_duration_s=5)
    with pytest.raises(ReadAloudEvidenceError, match="odd byte"):
        analyze_readaloud_pcm(b"\x00", expected_duration_s=5)
