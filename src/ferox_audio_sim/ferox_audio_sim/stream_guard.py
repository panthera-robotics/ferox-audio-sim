"""Continuity guard for host-side AudioChunk playback."""
from __future__ import annotations

import re
from typing import Any


_STREAM_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_KNOWN_FLAGS = 1 | 2 | 4


class StreamContractError(ValueError):
    pass


class StreamGuard:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._stream_id: str | None = None
        self._next_sequence = 0
        self._next_offset = 0

    def _reject(self, reason: str) -> None:
        self.reset()
        raise StreamContractError(reason)

    def accept(self, message: Any, payload: bytes) -> None:
        try:
            version = int(message.contract_version)
            encoding = int(message.encoding)
            stream_id = str(message.stream_id)
            sequence = int(message.sequence)
            offset = int(message.sample_offset)
            flags = int(message.flags)
            channels = int(message.channels)
            width = int(message.sample_width)
        except (AttributeError, TypeError, ValueError, OverflowError):
            self._reject("invalid AudioChunk stream metadata")
        if version != 1 or encoding != 1:
            self._reject("unsupported AudioChunk contract or encoding")
        if not _STREAM_ID.fullmatch(stream_id):
            self._reject("invalid AudioChunk stream_id")
        if sequence < 0 or offset < 0 or flags < 0 or flags & ~_KNOWN_FLAGS:
            self._reject("invalid AudioChunk counters or flags")
        frame_bytes = channels * width
        if channels <= 0 or width != 2 or not payload or len(payload) % frame_bytes:
            self._reject("invalid AudioChunk PCM frame layout")

        start = bool(flags & 1)
        end = bool(flags & 2)
        discontinuity = bool(flags & 4)
        if discontinuity and not start:
            self._reject("discontinuity requires a replacement stream start")
        if start:
            if sequence != 0 or offset != 0:
                self._reject("stream start counters must be zero")
            if self._stream_id is not None and not discontinuity:
                self._reject("active stream replacement requires discontinuity")
            self._stream_id = stream_id
            self._next_sequence = 0
            self._next_offset = 0
        elif self._stream_id is None:
            self._reject("first chunk must start a stream")
        if stream_id != self._stream_id:
            self._reject("stream_id changed without replacement start")
        if sequence != self._next_sequence:
            self._reject("AudioChunk sequence gap")
        if offset != self._next_offset:
            self._reject("AudioChunk sample offset gap")
        self._next_sequence += 1
        self._next_offset += len(payload) // frame_bytes
        if end:
            self.reset()
