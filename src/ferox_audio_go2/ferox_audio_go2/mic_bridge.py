"""Decode evidence-qualified Go2 microphone frames into Ferox PCM chunks."""
from __future__ import annotations

import audioop
from array import array
import ctypes
import ctypes.util
from dataclasses import dataclass
import math
import sys
import time
import uuid
from typing import Callable, Protocol

from .profiles import Go2AudioProfile
from .stream_contract import (
    CONTRACT_VERSION,
    ENCODING_PCM_S16LE,
    FLAG_DISCONTINUITY,
    FLAG_START,
)


class MicIngressError(ValueError):
    """A source frame cannot be decoded under the selected hardware profile."""


class FrameDecoder(Protocol):
    def decode(self, payload: bytes) -> bytes: ...
    def reset(self) -> None: ...
    def close(self) -> None: ...


class UlawDecoder:
    def decode(self, payload: bytes) -> bytes:
        try:
            return audioop.ulaw2lin(payload, 2)
        except audioop.error as exc:
            raise MicIngressError(f"G.711 u-law decode failed: {exc}") from exc

    def reset(self) -> None:
        return

    def close(self) -> None:
        return


class OpusDecoder:
    """Small libopus binding with explicit frame size and bounded output."""

    def __init__(self, sample_rate: int = 48_000, channels: int = 1,
                 frame_samples: int = 960, library: str | None = None) -> None:
        if sys.byteorder != "little":
            raise MicIngressError("Go2 PCM adapter requires a little-endian host")
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.frame_samples = int(frame_samples)
        path = library or ctypes.util.find_library("opus")
        if not path:
            raise MicIngressError("libopus is unavailable; install the runtime library")
        self._lib = ctypes.CDLL(path)
        self._lib.opus_decoder_create.argtypes = [
            ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self._lib.opus_decoder_create.restype = ctypes.c_void_p
        self._lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        self._lib.opus_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._lib.opus_decode.restype = ctypes.c_int
        self._decoder: int | None = None
        self.reset()

    def reset(self) -> None:
        self.close()
        error = ctypes.c_int(-1)
        decoder = self._lib.opus_decoder_create(
            self.sample_rate, self.channels, ctypes.byref(error))
        if not decoder or error.value != 0:
            raise MicIngressError(f"opus decoder creation failed with code {error.value}")
        self._decoder = decoder

    def decode(self, payload: bytes) -> bytes:
        if self._decoder is None:
            raise MicIngressError("opus decoder is closed")
        encoded = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        output = (ctypes.c_int16 * (self.frame_samples * self.channels))()
        decoded = self._lib.opus_decode(
            self._decoder,
            encoded,
            len(payload),
            output,
            self.frame_samples,
            0,
        )
        if decoded < 0:
            raise MicIngressError(f"opus decode failed with code {decoded}")
        if decoded != self.frame_samples:
            raise MicIngressError(
                f"opus frame produced {decoded} samples; expected {self.frame_samples}")
        return ctypes.string_at(output, decoded * self.channels * 2)

    def close(self) -> None:
        if getattr(self, "_decoder", None):
            self._lib.opus_decoder_destroy(self._decoder)
            self._decoder = None

    def __del__(self):  # pragma: no cover - interpreter shutdown is nondeterministic
        try:
            self.close()
        except Exception:
            pass


class RateConverter:
    def __init__(self, source_rate: int, target_rate: int) -> None:
        self.source_rate = int(source_rate)
        self.target_rate = int(target_rate)
        self._state = None

    def convert(self, pcm_s16le: bytes) -> bytes:
        if not pcm_s16le or len(pcm_s16le) % 2:
            raise MicIngressError("decoder output is empty or ends in a partial sample")
        # The only qualified upsample profile is 8 kHz -> 16 kHz.  Use exact
        # integer duplication so the first frame does not lose one sample to
        # ratecv's filter warm-up and every five 20 ms source frames still make
        # one exact 100 ms Ferox chunk.
        if self.target_rate >= self.source_rate:
            if self.target_rate % self.source_rate:
                raise MicIngressError("non-integer microphone upsampling is unsupported")
            ratio = self.target_rate // self.source_rate
            samples = array("h")
            samples.frombytes(pcm_s16le)
            if sys.byteorder != "little":
                samples.byteswap()
            expanded = array("h", (sample for sample in samples for _ in range(ratio)))
            if sys.byteorder != "little":
                expanded.byteswap()
            return expanded.tobytes()
        try:
            converted, self._state = audioop.ratecv(
                pcm_s16le, 2, 1, self.source_rate, self.target_rate, self._state)
        except audioop.error as exc:
            raise MicIngressError(f"PCM resampling failed: {exc}") from exc
        if not converted or len(converted) % 2:
            raise MicIngressError("resampler produced empty or partial PCM")
        return converted

    def reset(self) -> None:
        self._state = None


@dataclass(frozen=True)
class MicChunk:
    payload: bytes
    sequence: int
    sample_offset: int
    flags: int
    stream_id: str
    receive_time_ns: int
    contract_version: int = CONTRACT_VERSION
    encoding: int = ENCODING_PCM_S16LE
    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2


class Go2MicBridgeCore:
    """Pure source decoder, cadence gate, and exact 100 ms chunker."""

    def __init__(
        self,
        profile: Go2AudioProfile,
        *,
        decoder: FrameDecoder | None = None,
        stream_id_factory: Callable[[], str] | None = None,
        max_receive_gap_s: float = 0.100,
    ) -> None:
        if profile.output_sample_rate != 16_000:
            raise ValueError("Ferox speech ingress currently requires 16 kHz PCM")
        if not 0.04 <= max_receive_gap_s <= 1.0:
            raise ValueError("max_receive_gap_s must be in [0.04, 1.0]")
        self.profile = profile
        if decoder is None:
            decoder = (
                OpusDecoder(profile.mic_sample_rate, 1, profile.mic_frame_samples)
                if profile.mic_codec == "opus" else UlawDecoder()
            )
        self._decoder = decoder
        self._converter = RateConverter(
            profile.mic_sample_rate, profile.output_sample_rate)
        self._stream_id_factory = stream_id_factory or (lambda: uuid.uuid4().hex)
        self._max_receive_gap_s = float(max_receive_gap_s)
        self._output_chunk_bytes = 1_600 * 2
        self.accepted_source_frames = 0
        self.rejected_source_frames = 0
        self.output_chunks = 0
        self.discontinuities = 0
        self._reset_stream(discontinuity=False)

    def _new_stream_id(self) -> str:
        value = str(self._stream_id_factory())
        if not value or len(value) > 64 or not all(
                char.isalnum() or char in "_.:-" for char in value):
            raise MicIngressError("stream ID factory returned an invalid identifier")
        return value

    def _reset_stream(self, *, discontinuity: bool) -> None:
        self._pending = bytearray()
        self._stream_id = self._new_stream_id()
        self._sequence = 0
        self._sample_offset = 0
        self._started = False
        self._discontinuity_pending = bool(discontinuity)
        self._last_receive_s: float | None = None
        self._last_time_frame: int | None = None
        if discontinuity:
            self.discontinuities += 1
            self._decoder.reset()
            self._converter.reset()

    def close(self) -> None:
        self._decoder.close()

    def reject_and_reset(self, reason: str) -> None:
        self.rejected_source_frames += 1
        self._reset_stream(discontinuity=True)
        raise MicIngressError(reason)

    def ingest(
        self,
        payload: bytes,
        *,
        time_frame: int,
        receive_steady_s: float,
        receive_time_ns: int,
    ) -> list[MicChunk]:
        try:
            raw = bytes(payload)
        except (TypeError, ValueError) as exc:
            self.reject_and_reset(f"AudioData payload is not byte-like: {exc}")
        receive_steady_s = float(receive_steady_s)
        time_frame = int(time_frame)
        receive_time_ns = int(receive_time_ns)
        if not math.isfinite(receive_steady_s) or receive_steady_s < 0:
            self.reject_and_reset("steady receive time is invalid")
        if receive_time_ns <= 0 or time_frame < 0:
            self.reject_and_reset("source or receive timestamp is invalid")
        if len(raw) != self.profile.mic_frame_bytes:
            self.reject_and_reset(
                f"source frame is {len(raw)} bytes; profile requires "
                f"{self.profile.mic_frame_bytes}")
        if self._last_receive_s is not None:
            if receive_steady_s < self._last_receive_s:
                self.reject_and_reset("steady receive clock moved backwards")
            if receive_steady_s - self._last_receive_s > self._max_receive_gap_s:
                self._reset_stream(discontinuity=True)
        if self._last_time_frame is not None and time_frame <= self._last_time_frame:
            self.reject_and_reset("AudioData.time_frame did not increase")
        try:
            decoded = self._decoder.decode(raw)
            expected_bytes = self.profile.mic_frame_samples * 2
            if len(decoded) != expected_bytes:
                raise MicIngressError(
                    f"decoder produced {len(decoded)} bytes; expected {expected_bytes}")
            converted = self._converter.convert(decoded)
        except MicIngressError:
            self.rejected_source_frames += 1
            self._reset_stream(discontinuity=True)
            raise

        self._pending.extend(converted)
        self._last_receive_s = receive_steady_s
        self._last_time_frame = time_frame
        self.accepted_source_frames += 1
        chunks: list[MicChunk] = []
        while len(self._pending) >= self._output_chunk_bytes:
            output = bytes(self._pending[:self._output_chunk_bytes])
            del self._pending[:self._output_chunk_bytes]
            flags = 0
            if not self._started:
                flags |= FLAG_START
                if self._discontinuity_pending:
                    flags |= FLAG_DISCONTINUITY
            chunk = MicChunk(
                payload=output,
                sequence=self._sequence,
                sample_offset=self._sample_offset,
                flags=flags,
                stream_id=self._stream_id,
                receive_time_ns=receive_time_ns,
            )
            chunks.append(chunk)
            self._sequence += 1
            self._sample_offset += 1_600
            self._started = True
            self._discontinuity_pending = False
            self.output_chunks += 1
        return chunks


def chunk_to_message(message_type, chunk: MicChunk, stamp, frame_id: str):
    message = message_type()
    message.header.stamp = stamp
    message.header.frame_id = frame_id
    message.contract_version = chunk.contract_version
    message.encoding = chunk.encoding
    message.stream_id = chunk.stream_id
    message.sequence = chunk.sequence
    message.sample_offset = chunk.sample_offset
    message.flags = chunk.flags
    message.sample_rate = chunk.sample_rate
    message.channels = chunk.channels
    message.sample_width = chunk.sample_width
    message.data = list(chunk.payload)
    return message
