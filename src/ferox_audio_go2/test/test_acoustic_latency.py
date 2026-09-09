import numpy as np
import pytest

from ferox_audio_go2.acoustic_latency import (
    AcousticLatencyError,
    evaluate_acoustic_latency,
    matched_filter,
)


def chirp(samples=960):
    phase = np.linspace(0.0, 80.0 * np.pi, samples)
    return 10_000.0 * np.sin(phase * np.linspace(0.2, 1.0, samples))


def test_matched_filter_localizes_unique_signal():
    reference = chirp()
    capture = np.zeros(8_000)
    capture[2_345:2_345 + reference.size] = reference
    report = matched_filter(capture, reference)
    assert report["onset_sample"] == 2_345
    assert report["localization_passed"] is True
    assert report["normalized_peak"] > 0.99


def test_command_to_mic_callback_latency_uses_same_frame_observation():
    reference = chirp(480)
    capture = np.zeros(4_800, dtype="<i2")
    capture[1_200:1_680] = reference.astype("<i2")
    callbacks = [1_000_000_000 + index * 10_000_000 for index in range(10)]
    report = evaluate_acoustic_latency(
        capture_pcm=capture.tobytes(),
        capture_sample_rate=48_000,
        frame_samples=480,
        frame_callbacks_steady_ns=callbacks,
        reference=reference,
        reference_sample_rate=48_000,
        publish_steady_ns=900_000_000,
        maximum_latency_ms=200.0,
    )
    assert report["metrics"]["matched_onset_frame_index"] == 2
    assert report["metrics"]["command_to_mic_callback_latency_ms"] == 120.0
    assert report["measured"] is True


def test_ambiguous_repeated_signal_fails_closed():
    reference = chirp()
    capture = np.zeros(6_000)
    capture[500:500 + reference.size] = reference
    capture[3_000:3_000 + reference.size] = reference
    report = matched_filter(capture, reference)
    assert report["localization_passed"] is False
    assert report["peak_ratio"] == pytest.approx(1.0)


def test_short_capture_is_rejected():
    with pytest.raises(AcousticLatencyError, match="twice"):
        matched_filter(np.zeros(100), chirp(80))
