"""Bounded Ferox PCM to observed Go2 audiohub_v1 upload requests."""
from __future__ import annotations

from dataclasses import dataclass
import base64
import io
import json
import wave

from .stream_contract import AudioStreamGuard


class SpeakerProtocolError(ValueError):
    """A speaker stream or audiohub upload request is unsafe."""


@dataclass(frozen=True)
class AudioHubRequest:
    api_id: int
    parameter: str


@dataclass(frozen=True)
class AudioHubPlan:
    stream_id: str
    sample_rate: int
    wav_bytes: bytes
    requests: tuple[AudioHubRequest, ...]


def pcm_to_wav(pcm_s16le: bytes, *, sample_rate: int = 22_050) -> bytes:
    if not pcm_s16le or len(pcm_s16le) % 2:
        raise SpeakerProtocolError("speaker PCM is empty or ends in a partial sample")
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(int(sample_rate))
        writer.writeframes(pcm_s16le)
    return output.getvalue()


def build_audiohub_plan(
    pcm_s16le: bytes,
    *,
    stream_id: str,
    sample_rate: int = 22_050,
    raw_block_bytes: int = 46_080,
) -> AudioHubPlan:
    """Build the hardware-observed 4001 start + 4003 WAV block sequence."""
    if not 1_024 <= int(raw_block_bytes) <= 46_080:
        raise SpeakerProtocolError("audiohub raw block size must be in [1024, 46080]")
    wav_bytes = pcm_to_wav(pcm_s16le, sample_rate=sample_rate)
    blocks = [wav_bytes[index:index + raw_block_bytes]
              for index in range(0, len(wav_bytes), raw_block_bytes)]
    if not blocks:
        raise SpeakerProtocolError("audiohub upload contains no WAV blocks")
    requests = [AudioHubRequest(api_id=4001, parameter="")]
    for index, block in enumerate(blocks):
        encoded = base64.b64encode(block).decode("ascii")
        parameter = json.dumps({
            "current_block_index": index,
            "total_block_number": len(blocks),
            "current_block_size": len(encoded),
            "block_content": encoded,
        }, separators=(",", ":"), sort_keys=True)
        requests.append(AudioHubRequest(api_id=4003, parameter=parameter))
    return AudioHubPlan(
        stream_id=str(stream_id), sample_rate=int(sample_rate),
        wav_bytes=wav_bytes, requests=tuple(requests))


class SpeakerStreamAssembler:
    """Collect one bounded complete utterance before issuing any robot request."""

    def __init__(self, *, sample_rate: int = 22_050,
                 max_utterance_s: float = 30.0) -> None:
        if int(sample_rate) not in (16_000, 22_050):
            raise ValueError("Go2 speaker sample_rate must be 16000 or 22050")
        if not 0.1 <= float(max_utterance_s) <= 60.0:
            raise ValueError("max_utterance_s must be in [0.1, 60]")
        self.sample_rate = int(sample_rate)
        self._max_bytes = int(self.sample_rate * 2 * float(max_utterance_s))
        self._guard = AudioStreamGuard(
            sample_rate=self.sample_rate, max_chunk_bytes=88_200)
        self._buffer = bytearray()
        self._stream_id: str | None = None

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._guard.reset()
        self._buffer.clear()
        self._stream_id = None

    def accept(self, message) -> AudioHubPlan | None:
        try:
            accepted = self._guard.accept(message)
            if accepted.started:
                self._buffer.clear()
                self._stream_id = accepted.stream_id
            if len(self._buffer) + len(accepted.payload) > self._max_bytes:
                raise SpeakerProtocolError("speaker utterance exceeds the configured duration")
            self._buffer.extend(accepted.payload)
            if not accepted.ended:
                return None
            pcm = bytes(self._buffer)
            stream_id = self._stream_id or accepted.stream_id
            plan = build_audiohub_plan(
                pcm, stream_id=stream_id, sample_rate=self.sample_rate)
            self.reset()
            return plan
        except Exception as exc:
            self.reset()
            if isinstance(exc, SpeakerProtocolError):
                raise
            raise SpeakerProtocolError(str(exc)) from exc
