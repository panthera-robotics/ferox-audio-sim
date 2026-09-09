import pytest

from ferox_audio_go2.live_core_qualification import evaluate_live_core, percentile


def passing_report(**overrides):
    values = {
        "duration_s": 120.0,
        "source_frames": 6_000,
        "accepted_frames": 6_000,
        "rejected_frames": 0,
        "output_chunks": 1_200,
        "discontinuities": 0,
        "payload_lengths": {160},
        "chunk_lengths": {3_200},
        "time_frame_step": 200_000,
        "time_frame_step_outliers": 0,
        "decode_latencies_ms": [0.03, 0.04, 0.05],
        "source_to_chunk_latencies_ms": [79.0, 82.0, 90.0],
    }
    values.update(overrides)
    return evaluate_live_core(**values)


def test_live_core_gate_accepts_exact_candidate_stream():
    report = passing_report()
    assert report["failures"] == []
    assert report["source_rate_hz"] == pytest.approx(50.0)
    assert report["expected_output_chunks"] == 1_200
    assert report["time_frame_step_mode"] == 200_000


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"rejected_frames": 1}, "zero_rejected_frames"),
        ({"accepted_frames": 5_999}, "all_source_frames_accepted"),
        ({"discontinuities": 1}, "zero_discontinuities"),
        ({"payload_lengths": {159, 160}}, "exact_160_byte_source_frames"),
        ({"chunk_lengths": {3_200, 6_400}}, "exact_3200_byte_output_chunks"),
        ({"output_chunks": 1_199}, "exact_five_to_one_chunking"),
        ({"time_frame_step_outliers": 1}, "zero_source_step_outliers"),
        ({"decode_latencies_ms": [1.1]}, "decode_p95_under_1ms"),
        ({"source_to_chunk_latencies_ms": [121.0]},
         "source_to_chunk_p99_under_120ms"),
        ({"source_to_chunk_latencies_ms": [151.0]},
         "source_to_chunk_max_under_150ms"),
    ],
)
def test_live_core_gate_fails_closed(override, expected):
    assert expected in passing_report(**override)["failures"]


def test_percentile_uses_nearest_rank_and_handles_empty():
    assert percentile([], 0.95) is None
    assert percentile([3.0, 1.0, 2.0], 0.50) == 2.0
    assert percentile([3.0, 1.0, 2.0], 0.95) == 3.0
