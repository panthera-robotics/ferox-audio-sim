from __future__ import annotations

import math

import pytest

from ferox_audio_g1.pcm_gate import (
    CONTRACT_VERSION,
    ENCODING_PCM_S16LE,
    FLAG_DISCONTINUITY,
    FLAG_END,
    FLAG_START,
    PcmContract,
    PcmContractError,
    PcmGate,
)


def accept(
    gate: PcmGate,
    *,
    sequence: int = 0,
    offset: int = 0,
    flags: int = FLAG_START,
    stream: str = "speaker-a",
    now: float = 10.0,
    data: bytes | None = None,
):
    gate.accept(
        data=data if data is not None else bytes(3_200),
        sample_rate=16_000,
        channels=1,
        sample_width=2,
        contract_version=CONTRACT_VERSION,
        encoding=ENCODING_PCM_S16LE,
        stream_id=stream,
        sequence=sequence,
        sample_offset=offset,
        flags=flags,
        receive_steady_s=now,
    )


@pytest.mark.parametrize(
    "override",
    [
        {"sample_rate": 22_050},
        {"channels": 2},
        {"sample_width": 1},
        {"data": b""},
        {"data": b"x"},
        {"contract_version": 2},
        {"encoding": 2},
        {"stream_id": "bad stream"},
        {"sequence": -1},
        {"flags": 8},
        {"receive_steady_s": math.nan},
    ],
)
def test_rejects_invalid_or_untrustworthy_chunks(override):
    gate = PcmGate()
    kwargs = {
        "data": bytes(3_200),
        "sample_rate": 16_000,
        "channels": 1,
        "sample_width": 2,
        "contract_version": CONTRACT_VERSION,
        "encoding": ENCODING_PCM_S16LE,
        "stream_id": "speaker-a",
        "sequence": 0,
        "sample_offset": 0,
        "flags": FLAG_START,
        "receive_steady_s": 10.0,
    }
    kwargs.update(override)
    with pytest.raises(PcmContractError):
        gate.accept(**kwargs)


def test_rejects_sequence_and_sample_gaps_and_invalidates_stream():
    gate = PcmGate()
    accept(gate)
    with pytest.raises(PcmContractError, match="sequence gap"):
        accept(gate, sequence=2, offset=1_600, flags=0, now=10.1)
    with pytest.raises(PcmContractError, match="FLAG_START"):
        accept(gate, sequence=1, offset=1_600, flags=0, now=10.2)

    accept(gate, stream="speaker-b", now=10.3)
    with pytest.raises(PcmContractError, match="sample offset gap"):
        accept(
            gate,
            stream="speaker-b",
            sequence=1,
            offset=1_599,
            flags=0,
            now=10.4,
        )


def test_batches_one_second_of_pcm_without_reordering():
    gate = PcmGate()
    chunks = []
    for index in range(10):
        payload = bytes([index]) * 3_200
        chunks.append(payload)
        accept(
            gate,
            sequence=index,
            offset=index * 1_600,
            flags=FLAG_START if index == 0 else 0,
            now=9.1 + index * 0.01,
            data=payload,
        )
    assert gate.ready(9.2)
    assert gate.pop_request(9.2) == b"".join(chunks)
    assert gate.buffered_bytes == 0


def test_end_flag_flushes_partial_tail_immediately():
    gate = PcmGate()
    accept(gate, flags=FLAG_START | FLAG_END)
    assert gate.pop_request(10.0) == bytes(3_200)
    assert gate.buffered_bytes == 0


def test_flushes_open_stream_tail_only_after_idle_deadline():
    gate = PcmGate()
    accept(gate, now=10.0)
    assert gate.pop_request(10.149) is None
    assert gate.pop_request(10.151) == bytes(3_200)


def test_receive_gap_rejects_without_cross_host_clock_comparison():
    gate = PcmGate()
    accept(gate, now=10.0)
    with pytest.raises(PcmContractError, match="receive gap"):
        accept(gate, sequence=1, offset=1_600, flags=0, now=10.501)


def test_active_stream_replacement_requires_discontinuity():
    gate = PcmGate()
    accept(gate)
    with pytest.raises(PcmContractError, match="replacement"):
        accept(gate, stream="speaker-b", now=10.1)
    accept(
        gate,
        stream="speaker-b",
        flags=FLAG_START | FLAG_DISCONTINUITY,
        now=10.2,
    )


def test_overflow_discards_the_entire_stale_buffer():
    gate = PcmGate(PcmContract(max_chunk_bytes=32_000, max_buffer_bytes=32_000))
    accept(gate, data=bytes(32_000))
    with pytest.raises(PcmContractError, match="overflow"):
        accept(gate, sequence=1, offset=16_000, flags=0, now=10.1, data=bytes(2))
    assert gate.buffered_bytes == 0


def test_contract_rejects_unsafe_configuration():
    with pytest.raises(ValueError):
        PcmContract(target_request_bytes=96_002, max_buffer_bytes=96_000)
