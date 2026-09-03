"""Continuity analysis for the official Unitree G1 microphone multicast stream.

Pure core: it consumes recorded packet arrivals and never touches a socket, so
the whole verdict is unit-testable off-robot.  The existing receive-only probe
reports p50/p95/max interarrival, which is enough to see that the stream is
jittery but not enough to say *why*.  A left-skewed distribution (median above
mean) means packets arrive in bursts, and a burst followed by a long gap is a
very different ingestion problem from uniformly late delivery.

Nothing here authorizes a microphone path.  It produces the evidence that a
decision about `audio/mic_raw` would have to be based on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

PCM_SAMPLE_RATE = 16_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2

# Gap buckets in units of the nominal packet interval.  A source that never
# exceeds 2x nominal can be absorbed by a one-packet jitter buffer.
_GAP_MULTIPLES = (1.25, 1.5, 2.0, 3.0)


class MicStreamError(ValueError):
    """The recorded capture cannot be interpreted as the G1 mic contract."""


def percentile(values: list[float], quantile: float) -> float | None:
    """Linear-interpolated percentile, matching the existing G1 mic probe."""
    if not values:
        return None
    if not math.isfinite(quantile) or not 0.0 <= quantile <= 1.0:
        raise MicStreamError("quantile must be in [0, 1]")
    if any(not math.isfinite(value) for value in values):
        raise MicStreamError("percentile values must be finite")
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


@dataclass(frozen=True)
class MicStreamContract:
    """The packet shape the G1 multicast source is expected to hold."""

    packet_bytes: int = 5_120
    sample_rate: int = PCM_SAMPLE_RATE
    channels: int = PCM_CHANNELS
    sample_width: int = PCM_SAMPLE_WIDTH

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.channels != 1 or self.sample_width != 2:
            raise MicStreamError("G1 mic PCM must be mono signed 16-bit")
        if self.packet_bytes <= 0 or self.packet_bytes % self.sample_width:
            raise MicStreamError("packet_bytes must hold whole samples")

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_width

    @property
    def nominal_interval_s(self) -> float:
        """Wall time each packet represents if the source runs in real time."""
        return self.packet_bytes / self.bytes_per_second


@dataclass(frozen=True)
class MicStreamReport:
    contract_held: bool
    continuous: bool
    reasons: tuple[str, ...]
    packets: int
    bytes_total: int
    # Full probe window, recorded for custody; never used for the verdict.
    elapsed_s: float
    # First arrival to the end of the last packet's audio. The verdict basis.
    span_s: float
    audio_s: float
    # Positive means less audio arrived than the span it was spread across.
    drift_s: float
    drift_ratio: float
    # Negative means the source delivers less audio than wall time, i.e. its
    # clock runs slow against the host. Reported, not judged.
    rate_deviation_ppm: float
    expected_packets: int
    missing_packets: int
    packet_sizes: dict[int, int] = field(default_factory=dict)
    nominal_interval_ms: float = 0.0
    cadence_ms: float = 0.0
    interarrival_ms: dict[str, float | None] = field(default_factory=dict)
    burst_packets: int = 0
    gap_counts: dict[str, int] = field(default_factory=dict)
    worst_window_packets: int | None = None
    window_packets: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_held": self.contract_held,
            "continuous": self.continuous,
            "reasons": list(self.reasons),
            "packets": self.packets,
            "bytes_total": self.bytes_total,
            "elapsed_s": round(self.elapsed_s, 6),
            "span_s": round(self.span_s, 6),
            "audio_s": round(self.audio_s, 6),
            "drift_s": round(self.drift_s, 6),
            "drift_ratio": round(self.drift_ratio, 9),
            "rate_deviation_ppm": round(self.rate_deviation_ppm, 3),
            "expected_packets": self.expected_packets,
            "missing_packets": self.missing_packets,
            "packet_sizes": {str(k): v for k, v in sorted(self.packet_sizes.items())},
            "nominal_interval_ms": round(self.nominal_interval_ms, 6),
            "cadence_ms": round(self.cadence_ms, 6),
            "interarrival_ms": {
                key: (round(value, 6) if value is not None else None)
                for key, value in self.interarrival_ms.items()
            },
            "burst_packets": self.burst_packets,
            "gap_counts": dict(self.gap_counts),
            "worst_window_packets": self.worst_window_packets,
            "window_packets": list(self.window_packets),
        }


def analyze_mic_stream(
    *,
    arrivals_s: list[float],
    payload_sizes: list[int],
    elapsed_s: float,
    contract: MicStreamContract | None = None,
    window_s: float = 10.0,
    max_drift_ratio: float = 0.01,
) -> MicStreamReport:
    """Judge whether a recorded capture is a usable continuous mic source.

    `arrivals_s` are monotonic receive timestamps, one per packet, in the order
    received.  `elapsed_s` is the full probe window including any silence before
    the first packet, so audio-versus-wall drift stays honest.
    """
    contract = contract or MicStreamContract()
    if len(arrivals_s) != len(payload_sizes):
        raise MicStreamError("arrivals and payload sizes must be parallel")
    if elapsed_s <= 0.0 or not math.isfinite(elapsed_s):
        raise MicStreamError("elapsed_s must be positive and finite")
    if not math.isfinite(window_s) or window_s <= 0.0:
        raise MicStreamError("window_s must be positive and finite")
    if not math.isfinite(max_drift_ratio) or not 0.0 <= max_drift_ratio <= 1.0:
        raise MicStreamError("max_drift_ratio must be finite and in [0, 1]")
    if any(not math.isfinite(arrival) or arrival < 0.0 for arrival in arrivals_s):
        raise MicStreamError("arrival timestamps must be non-negative and finite")
    if any(
        later < earlier
        for earlier, later in zip(arrivals_s, arrivals_s[1:])
    ):
        raise MicStreamError("arrival timestamps must be non-decreasing")
    if any(not isinstance(size, int) or size < 0 for size in payload_sizes):
        raise MicStreamError("payload sizes must be non-negative integers")

    reasons: list[str] = []
    packets = len(arrivals_s)
    bytes_total = sum(payload_sizes)
    packet_sizes: dict[int, int] = {}
    for size in payload_sizes:
        packet_sizes[size] = packet_sizes.get(size, 0) + 1

    nominal_interval_s = contract.nominal_interval_s
    nominal_ms = nominal_interval_s * 1_000.0
    audio_s = bytes_total / contract.bytes_per_second

    interarrivals_ms = [
        (later - earlier) * 1_000.0
        for earlier, later in zip(arrivals_s, arrivals_s[1:])
    ]

    # Loss is measured against the stream's own observed cadence, never against
    # the nominal rate.  The live G1 source runs a bit over a thousand ppm slow
    # against host monotonic time, and a nominal-rate expectation turns that
    # slow accumulation into phantom dropped packets.  A genuine drop instead
    # leaves a hole: a single interval that is a whole multiple of the cadence.
    #
    # The cadence estimator is the mean, not the median.  The mean is just the
    # span divided by the interval count, so it tracks real drift and cannot be
    # dragged off by burst pairs the way a median can.
    #
    # The 1.75x hole threshold sits in the empty band between jitter and loss:
    # the live capture's worst jitter reached 1.51x cadence, while losing one
    # packet necessarily costs a full extra interval at 2x.
    observed_cadence_ms = (
        sum(interarrivals_ms) / len(interarrivals_ms)
        if interarrivals_ms else nominal_ms
    )
    # A coarse monotonic clock can stamp a short burst at one instant. Keep
    # reporting the burst and let the span-drift gate reject it, but never
    # divide by zero while estimating holes.
    cadence_ms = observed_cadence_ms if observed_cadence_ms > 0.0 else nominal_ms
    hole_threshold_ms = cadence_ms * 1.75
    missing_packets = sum(
        max(0, int(round(value / cadence_ms)) - 1)
        for value in interarrivals_ms
        if value > hole_threshold_ms
    )
    expected_packets = packets + missing_packets

    # The span runs from the first arrival to the end of the audio the last
    # packet carried, so it is independent of where the probe window happened
    # to open inside a packet interval.
    span_s = (
        (arrivals_s[-1] - arrivals_s[0]) + nominal_interval_s
        if packets else elapsed_s
    )
    drift_s = span_s - audio_s
    drift_ratio = drift_s / span_s if span_s else 0.0
    rate_deviation_ppm = (
        (audio_s / span_s - 1.0) * 1_000_000.0 if span_s else 0.0)

    contract_held = True
    if not packets:
        contract_held = False
        reasons.append("no packets received")
    off_contract = {
        size: count
        for size, count in packet_sizes.items()
        if size != contract.packet_bytes
    }
    if off_contract:
        contract_held = False
        reasons.append(
            "packet sizes deviate from the "
            f"{contract.packet_bytes}-byte contract: {sorted(off_contract)}"
        )

    burst_packets = sum(
        1 for value in interarrivals_ms if value < nominal_ms * 0.5)
    gap_counts = {
        f"over_{multiple:g}x_nominal": sum(
            1 for value in interarrivals_ms if value > nominal_ms * multiple)
        for multiple in _GAP_MULTIPLES
    }

    window_packets: list[int] = []
    worst_window_packets: int | None = None
    if packets:
        window_count = max(1, int(math.ceil(elapsed_s / window_s)))
        buckets = [0] * window_count
        origin = arrivals_s[0]
        for arrival in arrivals_s:
            index = min(window_count - 1, int((arrival - origin) // window_s))
            buckets[index] += 1
        window_packets = buckets
        # The final window is usually clipped by the probe deadline, so it is
        # not evidence of a dropout and must not decide the verdict.
        judged = buckets[:-1] if len(buckets) > 1 else buckets
        worst_window_packets = min(judged)

    continuous = contract_held
    if missing_packets:
        continuous = False
        reasons.append(
            f"{missing_packets} packet(s) lost in stream holes against a "
            f"{cadence_ms:.3f} ms observed cadence")
    if abs(drift_ratio) > max_drift_ratio:
        continuous = False
        reasons.append(
            f"audio-versus-span drift {drift_ratio * 100:.3f}% exceeds "
            f"{max_drift_ratio * 100:.3f}%")

    return MicStreamReport(
        contract_held=contract_held,
        continuous=continuous,
        reasons=tuple(reasons),
        packets=packets,
        bytes_total=bytes_total,
        elapsed_s=elapsed_s,
        span_s=span_s,
        audio_s=audio_s,
        drift_s=drift_s,
        drift_ratio=drift_ratio,
        rate_deviation_ppm=rate_deviation_ppm,
        expected_packets=expected_packets,
        missing_packets=missing_packets,
        packet_sizes=packet_sizes,
        nominal_interval_ms=nominal_ms,
        cadence_ms=cadence_ms,
        interarrival_ms={
            "count": float(len(interarrivals_ms)),
            "min": min(interarrivals_ms) if interarrivals_ms else None,
            "mean": (
                sum(interarrivals_ms) / len(interarrivals_ms)
                if interarrivals_ms else None),
            "p50": percentile(interarrivals_ms, 0.50),
            "p95": percentile(interarrivals_ms, 0.95),
            "p99": percentile(interarrivals_ms, 0.99),
            "max": max(interarrivals_ms) if interarrivals_ms else None,
        },
        burst_packets=burst_packets,
        gap_counts=gap_counts,
        worst_window_packets=worst_window_packets,
        window_packets=tuple(window_packets),
    )


def required_jitter_buffer_ms(report: MicStreamReport) -> float | None:
    """Smallest jitter buffer that would have absorbed this capture.

    A consumer that emits fixed-cadence chunks must hold at least the worst
    observed overshoot past nominal, or it underruns exactly once per gap.
    """
    worst = report.interarrival_ms.get("max")
    if worst is None:
        return None
    return max(0.0, worst - report.nominal_interval_ms)
