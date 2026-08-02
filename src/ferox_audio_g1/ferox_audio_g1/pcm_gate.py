"""Fail-closed PCM validation and bounded buffering for the G1 voice API."""
from __future__ import annotations

from dataclasses import dataclass


class PcmContractError(ValueError):
    """The upstream chunk cannot be represented by the G1 voice contract."""


@dataclass(frozen=True)
class PcmContract:
    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2
    max_chunk_bytes: int = 32_000
    target_request_bytes: int = 32_000
    max_buffer_bytes: int = 96_000
    max_source_age_s: float = 0.5
    max_future_s: float = 0.1
    idle_flush_s: float = 0.15

    def __post_init__(self) -> None:
        if self.sample_rate != 16_000 or self.channels != 1 or self.sample_width != 2:
            raise ValueError("G1 PlayStream requires 16 kHz mono signed 16-bit PCM")
        if self.max_chunk_bytes <= 0 or self.max_chunk_bytes % 2:
            raise ValueError("max_chunk_bytes must be a positive whole-sample size")
        if not 0 < self.target_request_bytes <= self.max_buffer_bytes:
            raise ValueError("target_request_bytes must fit in max_buffer_bytes")
        if self.target_request_bytes % 2 or self.max_buffer_bytes % 2:
            raise ValueError("buffer limits must preserve whole int16 samples")
        if min(self.max_source_age_s, self.max_future_s, self.idle_flush_s) <= 0:
            raise ValueError("time gates must be positive")


class PcmGate:
    """Validate source chronology and assemble bounded PlayStream requests."""

    def __init__(self, contract: PcmContract | None = None) -> None:
        self.contract = contract or PcmContract()
        self._buffer = bytearray()
        self._last_source_stamp_s: float | None = None
        self._last_receive_steady_s: float | None = None

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()

    def accept(
        self,
        *,
        data: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
        source_stamp_s: float,
        receive_ros_s: float,
        receive_steady_s: float,
    ) -> None:
        c = self.contract
        if (int(sample_rate), int(channels), int(sample_width)) != (
            c.sample_rate,
            c.channels,
            c.sample_width,
        ):
            raise PcmContractError(
                "expected 16000 Hz, mono, signed 16-bit little-endian PCM")
        payload = bytes(data)
        if not payload:
            raise PcmContractError("empty PCM chunks are not valid stream evidence")
        if len(payload) > c.max_chunk_bytes:
            raise PcmContractError("PCM chunk exceeds the configured request bound")
        if len(payload) % c.sample_width:
            raise PcmContractError("PCM chunk ends in a partial sample")
        if source_stamp_s <= 0:
            raise PcmContractError("source timestamp must be non-zero")
        age_s = receive_ros_s - source_stamp_s
        if age_s > c.max_source_age_s:
            raise PcmContractError("PCM chunk is stale")
        if age_s < -c.max_future_s:
            raise PcmContractError("PCM chunk timestamp is in the future")
        if (
            self._last_source_stamp_s is not None
            and source_stamp_s <= self._last_source_stamp_s
        ):
            raise PcmContractError("PCM timestamp did not advance")
        if len(self._buffer) + len(payload) > c.max_buffer_bytes:
            self.clear()
            raise PcmContractError("PCM buffer overflow; stale audio was discarded")

        self._buffer.extend(payload)
        self._last_source_stamp_s = source_stamp_s
        self._last_receive_steady_s = receive_steady_s

    def ready(self, now_steady_s: float) -> bool:
        if len(self._buffer) >= self.contract.target_request_bytes:
            return True
        return bool(
            self._buffer
            and self._last_receive_steady_s is not None
            and now_steady_s - self._last_receive_steady_s >= self.contract.idle_flush_s
        )

    def pop_request(self, now_steady_s: float) -> bytes | None:
        if not self.ready(now_steady_s):
            return None
        count = min(len(self._buffer), self.contract.target_request_bytes)
        count -= count % self.contract.sample_width
        payload = bytes(self._buffer[:count])
        del self._buffer[:count]
        return payload
