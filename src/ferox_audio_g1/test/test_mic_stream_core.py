import pytest

from ferox_audio_g1.mic_stream_core import (
    MicStreamContract,
    MicStreamError,
    analyze_mic_stream,
    percentile,
    required_jitter_buffer_ms,
)

NOMINAL_S = 0.16
PACKET_BYTES = 5_120


def _perfect_capture(packets=750, interval_s=NOMINAL_S):
    arrivals = [index * interval_s for index in range(packets)]
    return {
        "arrivals_s": arrivals,
        "payload_sizes": [PACKET_BYTES] * packets,
        "elapsed_s": packets * interval_s,
    }


def test_contract_derives_nominal_interval_from_pcm_rate():
    contract = MicStreamContract()
    assert contract.bytes_per_second == 32_000
    assert contract.nominal_interval_s == pytest.approx(0.16)


def test_a_real_time_stream_is_continuous():
    report = analyze_mic_stream(**_perfect_capture())
    assert report.contract_held
    assert report.continuous
    assert report.reasons == ()
    assert report.missing_packets == 0
    assert report.audio_s == pytest.approx(report.span_s)
    assert report.burst_packets == 0


def test_unaligned_probe_window_does_not_invent_a_dropped_packet():
    # Regression: the live 180 s G1 capture lost nothing (no interval above 2x
    # nominal, every 10 s window full) yet an elapsed/nominal expectation
    # reported one phantom missing packet purely because the probe window began
    # part-way through a packet interval.
    packets = 1_124
    lead_in_s = 0.09
    arrivals = [lead_in_s + index * NOMINAL_S for index in range(packets)]
    report = analyze_mic_stream(
        arrivals_s=arrivals,
        payload_sizes=[PACKET_BYTES] * packets,
        elapsed_s=lead_in_s + packets * NOMINAL_S + 0.05,
    )
    assert report.expected_packets == packets
    assert report.missing_packets == 0
    assert report.gap_counts["over_2x_nominal"] == 0
    assert report.continuous


def test_source_clock_running_slow_is_reported_not_failed():
    # The live G1 source delivered 160 ms packets every ~160.22 ms against host
    # monotonic time. Counting against the nominal rate would accrue a phantom
    # drop roughly every 700 packets; the cadence-based rule must not.
    packets = 1_124
    observed_interval_s = 0.16022
    arrivals = [index * observed_interval_s for index in range(packets)]
    report = analyze_mic_stream(
        arrivals_s=arrivals,
        payload_sizes=[PACKET_BYTES] * packets,
        elapsed_s=packets * observed_interval_s,
    )
    assert report.missing_packets == 0
    assert report.continuous
    assert report.cadence_ms == pytest.approx(160.22, abs=0.01)
    # Source slower than nominal means less audio than wall time.
    assert report.rate_deviation_ppm == pytest.approx(-1_373.0, abs=25.0)


def test_a_real_hole_is_still_caught_under_clock_drift():
    # Same drifting source, but one packet genuinely vanishes.
    observed_interval_s = 0.16022
    arrivals = [index * observed_interval_s for index in range(200)]
    del arrivals[100]
    report = analyze_mic_stream(
        arrivals_s=arrivals,
        payload_sizes=[PACKET_BYTES] * len(arrivals),
        elapsed_s=200 * observed_interval_s,
    )
    assert report.missing_packets == 1
    assert not report.continuous
    assert any("lost in stream holes" in reason for reason in report.reasons)


def test_short_packets_break_the_contract():
    capture = _perfect_capture(packets=10)
    capture["payload_sizes"][4] = 2_560
    report = analyze_mic_stream(**capture)
    assert not report.contract_held
    assert not report.continuous
    assert any("packet sizes deviate" in reason for reason in report.reasons)


def test_uniform_loss_is_caught_by_span_drift_not_by_holes():
    # Every second packet never arrives. Timing alone cannot tell this apart
    # from a source running at half rate -- every interval is identical, so
    # there is no hole to find. Only the audio-versus-span comparison exposes
    # it, which is why drift and not hole counting is the loss backstop.
    capture = _perfect_capture(packets=100, interval_s=NOMINAL_S * 2)
    report = analyze_mic_stream(**capture)
    assert report.contract_held
    assert report.missing_packets == 0
    assert not report.continuous
    assert report.drift_ratio == pytest.approx(0.5, abs=0.01)
    assert any("drift" in reason for reason in report.reasons)


def test_jitter_alone_does_not_fail_a_lossless_stream():
    # Mirrors the live capture: mostly regular delivery with a handful of early
    # arrivals, each paid back by a late one. Nothing is lost.
    arrivals = [index * NOMINAL_S for index in range(1_000)]
    for index in (137, 402, 668, 903):
        arrivals[index] -= 0.085
    report = analyze_mic_stream(
        arrivals_s=arrivals,
        payload_sizes=[PACKET_BYTES] * len(arrivals),
        elapsed_s=len(arrivals) * NOMINAL_S,
    )
    assert report.continuous
    assert report.missing_packets == 0
    assert report.burst_packets == 4
    assert report.interarrival_ms["min"] == pytest.approx(75.0, abs=0.5)
    assert report.interarrival_ms["max"] == pytest.approx(245.0, abs=0.5)
    assert report.gap_counts["over_2x_nominal"] == 0


def test_jitter_buffer_requirement_is_the_worst_overshoot():
    arrivals = [0.0, NOMINAL_S, NOMINAL_S + 0.257]
    report = analyze_mic_stream(
        arrivals_s=arrivals,
        payload_sizes=[PACKET_BYTES] * 3,
        elapsed_s=3 * NOMINAL_S,
    )
    assert required_jitter_buffer_ms(report) == pytest.approx(97.0, abs=1e-6)


def test_localized_dropout_shows_up_in_the_worst_window():
    # 30 s of packets, but the middle 10 s window is half empty.
    arrivals = []
    for index in range(int(10 / NOMINAL_S)):
        arrivals.append(index * NOMINAL_S)
    for index in range(int(5 / NOMINAL_S)):
        arrivals.append(10.0 + index * NOMINAL_S)
    for index in range(int(10 / NOMINAL_S)):
        arrivals.append(20.0 + index * NOMINAL_S)
    report = analyze_mic_stream(
        arrivals_s=arrivals,
        payload_sizes=[PACKET_BYTES] * len(arrivals),
        elapsed_s=30.0,
        window_s=10.0,
    )
    assert report.window_packets[0] == 62
    assert report.worst_window_packets == 31


def test_final_clipped_window_does_not_decide_the_verdict():
    # A capture that stops mid-window must not report a false dropout.
    report = analyze_mic_stream(**_perfect_capture(packets=94), window_s=10.0)
    assert report.window_packets[-1] < report.window_packets[0]
    assert report.worst_window_packets == report.window_packets[0]


def test_empty_capture_is_not_a_pass():
    report = analyze_mic_stream(
        arrivals_s=[], payload_sizes=[], elapsed_s=20.0)
    assert not report.contract_held
    assert not report.continuous
    assert "no packets received" in report.reasons


@pytest.mark.parametrize("changes", [
    {"arrivals_s": [0.0, 1.0], "payload_sizes": [PACKET_BYTES]},
    {"elapsed_s": 0.0},
    {"elapsed_s": float("inf")},
    {"window_s": float("nan")},
    {"max_drift_ratio": float("nan")},
    {"max_drift_ratio": -0.01},
    {"arrivals_s": [0.0, float("nan")],
     "payload_sizes": [PACKET_BYTES] * 2},
    {"payload_sizes": [PACKET_BYTES, -1]},
    {"payload_sizes": [PACKET_BYTES, 1.5]},
    {"arrivals_s": [1.0, 0.0], "payload_sizes": [PACKET_BYTES] * 2},
])
def test_malformed_captures_are_rejected(changes):
    capture = _perfect_capture(packets=2)
    capture.update(changes)
    with pytest.raises(MicStreamError):
        analyze_mic_stream(**capture)


def test_same_tick_burst_is_rejected_without_dividing_by_zero():
    report = analyze_mic_stream(
        arrivals_s=[0.0, 0.0, 0.0],
        payload_sizes=[PACKET_BYTES] * 3,
        elapsed_s=0.01,
    )
    assert report.cadence_ms == pytest.approx(160.0)
    assert report.burst_packets == 2
    assert not report.continuous


def test_percentile_rejects_nonfinite_inputs():
    with pytest.raises(MicStreamError, match="quantile"):
        percentile([1.0], float("nan"))
    with pytest.raises(MicStreamError, match="values"):
        percentile([1.0, float("inf")], 0.5)


def test_percentile_matches_the_original_probe_interpolation():
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 4.0
    assert percentile(values, 0.5) == pytest.approx(2.5)
    assert percentile([], 0.5) is None
