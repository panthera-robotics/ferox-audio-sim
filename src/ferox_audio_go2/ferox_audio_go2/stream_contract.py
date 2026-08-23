"""Versioned Ferox AudioChunk continuity and format validation.

PCM transport only. There is no echo-return, residual, or TCLw field on
this contract. ferox-audio-go2 has no canceller; see aec_unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


CONTRACT_VERSION = 1
ENCODING_PCM_S16LE = 1
FLAG_START = 1
FLAG_END = 2
FLAG_DISCONTINUITY = 4
_KNOWN_FLAGS = FLAG_START | FLAG_END | FLAG_DISCONTINUITY
_STREAM_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class AudioStreamError(ValueError):
    """An AudioChunk cannot safely continue the active stream."""


@dataclass(frozen=True)
class AcceptedChunk:
    payload: bytes
    stream_id: str
    sequence: int
    sample_offset: int
    sample_count: int
    started: bool
    ended: bool
    discontinuity: bool


class AudioStreamGuard:
    def __init__(self, *, sample_rate: int = 16_000, channels: int = 1,
                 sample_width: int = 2, max_chunk_bytes: int = 64_000) -> None:
        if sample_rate <= 0 or channels <= 0 or sample_width <= 0:
            raise ValueError("audio format values must be positive")
        if max_chunk_bytes <= 0:
            raise ValueError("max_chunk_bytes must be positive")
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.sample_width = int(sample_width)
        self.max_chunk_bytes = int(max_chunk_bytes)
        self.reset()

    def reset(self) -> None:
        self._stream_id: str | None = None
        self._next_sequence = 0
        self._next_sample_offset = 0

    def _reject(self, reason: str) -> None:
        self.reset()
        raise AudioStreamError(reason)

    def accept(self, message: Any) -> AcceptedChunk:
        try:
            version = int(message.contract_version)
            encoding = int(message.encoding)
            stream_id = str(message.stream_id)
            sequence = int(message.sequence)
            offset = int(message.sample_offset)
            flags = int(message.flags)
            sample_rate = int(message.sample_rate)
            channels = int(message.channels)
            sample_width = int(message.sample_width)
            payload = bytes(message.data)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            self._reject(f"invalid AudioChunk metadata: {exc}")
        if version != CONTRACT_VERSION or encoding != ENCODING_PCM_S16LE:
            self._reject("unsupported AudioChunk contract version or encoding")
        if not _STREAM_ID.fullmatch(stream_id):
            self._reject("AudioChunk stream_id is invalid")
        if min(sequence, offset, flags) < 0 or flags & ~_KNOWN_FLAGS:
            self._reject("AudioChunk counters or flags are invalid")
        if (sample_rate, channels, sample_width) != (
                self.sample_rate, self.channels, self.sample_width):
            self._reject("AudioChunk PCM format does not match the adapter contract")
        frame_bytes = channels * sample_width
        if not payload or len(payload) > self.max_chunk_bytes or len(payload) % frame_bytes:
            self._reject("AudioChunk payload is empty, oversized, or partial")
        started = bool(flags & FLAG_START)
        ended = bool(flags & FLAG_END)
        discontinuity = bool(flags & FLAG_DISCONTINUITY)
        if discontinuity and not started:
            self._reject("a discontinuity must begin a replacement stream")
        if started:
            if sequence != 0 or offset != 0:
                self._reject("a stream must start at sequence and sample offset zero")
            if self._stream_id is not None and not discontinuity:
                self._reject("active stream replacement requires a discontinuity")
            self._stream_id = stream_id
            self._next_sequence = 0
            self._next_sample_offset = 0
        elif self._stream_id is None:
            self._reject("first accepted chunk must carry FLAG_START")
        if stream_id != self._stream_id:
            self._reject("stream_id changed without an explicit replacement")
        if sequence != self._next_sequence:
            self._reject(
                f"AudioChunk sequence gap: expected {self._next_sequence}, got {sequence}")
        if offset != self._next_sample_offset:
            self._reject(
                f"AudioChunk sample gap: expected {self._next_sample_offset}, got {offset}")
        sample_count = len(payload) // frame_bytes
        accepted = AcceptedChunk(
            payload=payload,
            stream_id=stream_id,
            sequence=sequence,
            sample_offset=offset,
            sample_count=sample_count,
            started=started,
            ended=ended,
            discontinuity=discontinuity,
        )
        self._next_sequence += 1
        self._next_sample_offset += sample_count
        if ended:
            self.reset()
        return accepted
