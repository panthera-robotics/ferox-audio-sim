"""Fail-closed PCM validation and bounded buffering for the G1 voice API."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re


class PcmContractError(ValueError):
    """The upstream chunk cannot be represented by the G1 voice contract."""


CONTRACT_VERSION = 1
ENCODING_PCM_S16LE = 1
FLAG_START = 1
FLAG_END = 2
FLAG_DISCONTINUITY = 4
_KNOWN_FLAGS = FLAG_START | FLAG_END | FLAG_DISCONTINUITY
_STREAM_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@dataclass(frozen=True)
class PcmContract:
    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2
    max_chunk_bytes: int = 32_000
    target_request_bytes: int = 32_000
    max_buffer_bytes: int = 96_000
    max_interarrival_s: float = 0.5
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
        if min(self.max_interarrival_s, self.idle_flush_s) <= 0:
            raise ValueError("time gates must be positive")


class PcmGate:
    """Validate source chronology and assemble bounded PlayStream requests."""

    def __init__(self, contract: PcmContract | None = None) -> None:
        self.contract = contract or PcmContract()
        self._buffer = bytearray()
        self._last_receive_steady_s: float | None = None
        self._stream_id: str | None = None
        self._next_sequence = 0
        self._next_sample_offset = 0
        self._end_received = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()
        self._last_receive_steady_s = None
        self._stream_id = None
        self._next_sequence = 0
        self._next_sample_offset = 0
        self._end_received = False

    def discard_buffer(self) -> None:
        """Discard PCM while preserving validation state for a disabled sink."""
        self._buffer.clear()
        if self._end_received:
            self.clear()

    def _reject(self, reason: str) -> None:
        self.clear()
        raise PcmContractError(reason)

    def accept(
        self,
        *,
        data: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
        contract_version: int,
        encoding: int,
        stream_id: str,
        sequence: int,
        sample_offset: int,
        flags: int,
        receive_steady_s: float,
    ) -> None:
        c = self.contract
        contract_version = int(contract_version)
        encoding = int(encoding)
        stream_id = str(stream_id)
        sequence = int(sequence)
        sample_offset = int(sample_offset)
        flags = int(flags)
        receive_steady_s = float(receive_steady_s)
        if sequence < 0 or sample_offset < 0 or flags < 0:
            self._reject("AudioChunk counters and flags must be non-negative")
        if not math.isfinite(receive_steady_s):
            self._reject("steady receive timestamp must be finite")
        if contract_version != CONTRACT_VERSION:
            self._reject(f"unsupported AudioChunk contract version {contract_version}")
        if encoding != ENCODING_PCM_S16LE:
            self._reject(f"unsupported AudioChunk encoding {encoding}")
        if not _STREAM_ID.fullmatch(stream_id):
            self._reject("stream_id must be 1-64 portable identifier characters")
        if flags & ~_KNOWN_FLAGS:
            self._reject("AudioChunk contains unknown flags")
        if (int(sample_rate), int(channels), int(sample_width)) != (
            c.sample_rate,
            c.channels,
            c.sample_width,
        ):
            self._reject("expected 16000 Hz, mono, signed 16-bit little-endian PCM")
        payload = bytes(data)
        if not payload:
            self._reject("empty PCM chunks are not valid stream evidence")
        if len(payload) > c.max_chunk_bytes:
            self._reject("PCM chunk exceeds the configured request bound")
        if len(payload) % c.sample_width:
            self._reject("PCM chunk ends in a partial sample")
        started = bool(flags & FLAG_START)
        ended = bool(flags & FLAG_END)
        discontinuity = bool(flags & FLAG_DISCONTINUITY)
        if discontinuity and not started:
            self._reject("a discontinuity must begin a replacement stream")
        if started:
            if sequence != 0 or sample_offset != 0:
                self._reject("a stream must start at sequence and sample offset zero")
            if self._stream_id is not None and not discontinuity:
                self._reject("active stream replacement requires a discontinuity flag")
            self.clear()
            self._stream_id = stream_id
        elif self._stream_id is None:
            self._reject("first accepted chunk must carry FLAG_START")
        if self._end_received:
            self._reject("received data after FLAG_END")
        if stream_id != self._stream_id:
            self._reject("stream_id changed without an explicit replacement start")
        if sequence != self._next_sequence:
            self._reject(
                f"AudioChunk sequence gap: expected {self._next_sequence}, got {sequence}")
        if sample_offset != self._next_sample_offset:
            self._reject(
                "AudioChunk sample offset gap: "
                f"expected {self._next_sample_offset}, got {sample_offset}")
        if (
            self._last_receive_steady_s is not None
            and receive_steady_s - self._last_receive_steady_s > c.max_interarrival_s
        ):
            self._reject("AudioChunk receive gap exceeded the continuity deadline")
        if (
            self._last_receive_steady_s is not None
            and receive_steady_s < self._last_receive_steady_s
        ):
            self._reject("steady receive clock moved backwards")
        if len(self._buffer) + len(payload) > c.max_buffer_bytes:
            self._reject("PCM buffer overflow; stale audio was discarded")

        self._buffer.extend(payload)
        self._last_receive_steady_s = receive_steady_s
        self._next_sequence += 1
        self._next_sample_offset += len(payload) // c.sample_width
        self._end_received = ended

    def ready(self, now_steady_s: float) -> bool:
        if len(self._buffer) >= self.contract.target_request_bytes:
            return True
        if self._buffer and self._end_received:
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
        if not self._buffer and self._end_received:
            self.clear()
        return payload
