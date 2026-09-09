import base64
import io
import json
from types import SimpleNamespace
import wave

import pytest

from ferox_audio_go2.speaker_protocol import (
    SpeakerProtocolError,
    SpeakerStreamAssembler,
    build_audiohub_plan,
)
from ferox_audio_go2.stream_contract import FLAG_END, FLAG_START


def message(*, sequence=0, offset=0, flags=FLAG_START, payload=bytes(4_410)):
    return SimpleNamespace(
        contract_version=1,
        encoding=1,
        stream_id="tts-a",
        sequence=sequence,
        sample_offset=offset,
        flags=flags,
        sample_rate=22_050,
        channels=1,
        sample_width=2,
        data=payload,
    )


def test_builds_audiohub_start_then_bounded_wav_blocks():
    pcm = bytes(80_000)
    plan = build_audiohub_plan(pcm, stream_id="tts-a", raw_block_bytes=46_080)
    assert plan.requests[0].api_id == 4001
    assert plan.requests[0].parameter == ""
    assert all(request.api_id == 4003 for request in plan.requests[1:])
    rebuilt = bytearray()
    for index, request in enumerate(plan.requests[1:]):
        body = json.loads(request.parameter)
        assert body["current_block_index"] == index
        assert body["total_block_number"] == len(plan.requests) - 1
        assert body["current_block_size"] == len(body["block_content"])
        rebuilt.extend(base64.b64decode(body["block_content"], validate=True))
    assert bytes(rebuilt) == plan.wav_bytes
    with wave.open(io.BytesIO(plan.wav_bytes), "rb") as reader:
        assert (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) == (
            22_050, 1, 2)
        assert reader.readframes(reader.getnframes()) == pcm


def test_assembler_waits_for_complete_utterance_before_any_plan():
    assembler = SpeakerStreamAssembler(max_utterance_s=1.0)
    assert assembler.accept(message()) is None
    plan = assembler.accept(
        message(sequence=1, offset=2_205, flags=FLAG_END, payload=bytes(882)))
    assert plan is not None
    assert len(plan.wav_bytes) == 4_410 + 882 + 44
    assert assembler.buffered_bytes == 0


def test_assembler_discards_gap_and_oversize_utterance():
    assembler = SpeakerStreamAssembler(max_utterance_s=0.1)
    assembler.accept(message(payload=bytes(4_410)))
    with pytest.raises(SpeakerProtocolError, match="sequence gap"):
        assembler.accept(message(sequence=2, offset=2_205, flags=FLAG_END,
                                 payload=bytes(320)))
    assert assembler.buffered_bytes == 0

    assembler = SpeakerStreamAssembler(max_utterance_s=0.1)
    with pytest.raises(SpeakerProtocolError, match="duration"):
        assembler.accept(message(payload=bytes(4_412)))


@pytest.mark.parametrize("pcm", [b"", b"x"])
def test_refuses_empty_or_partial_pcm(pcm):
    with pytest.raises(SpeakerProtocolError):
        build_audiohub_plan(pcm, stream_id="bad")
