import pytest

from ferox_audio_g1.pcm_gate import PcmContract, PcmContractError, PcmGate


def accept(gate: PcmGate, *, stamp: float = 9.9, now: float = 10.0, data=None):
    gate.accept(
        data=data if data is not None else bytes(3_200),
        sample_rate=16_000,
        channels=1,
        sample_width=2,
        source_stamp_s=stamp,
        receive_ros_s=now,
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
        {"stamp": 0.0},
        {"stamp": 9.0},
        {"stamp": 10.2},
    ],
)
def test_rejects_invalid_or_untrustworthy_chunks(override):
    gate = PcmGate()
    kwargs = {
        "data": bytes(3_200),
        "sample_rate": 16_000,
        "channels": 1,
        "sample_width": 2,
        "source_stamp_s": 9.9,
        "receive_ros_s": 10.0,
        "receive_steady_s": 10.0,
    }
    if "stamp" in override:
        kwargs["source_stamp_s"] = override["stamp"]
    else:
        kwargs.update(override)
    with pytest.raises(PcmContractError):
        gate.accept(**kwargs)


def test_rejects_non_advancing_source_timestamps():
    gate = PcmGate()
    accept(gate)
    with pytest.raises(PcmContractError, match="did not advance"):
        accept(gate)


def test_batches_one_second_of_pcm_without_reordering():
    gate = PcmGate()
    chunks = []
    for index in range(10):
        payload = bytes([index]) * 3_200
        chunks.append(payload)
        accept(gate, stamp=9.01 + index * 0.01, now=9.1 + index * 0.01, data=payload)
    assert gate.ready(9.2)
    assert gate.pop_request(9.2) == b"".join(chunks)
    assert gate.buffered_bytes == 0


def test_flushes_partial_tail_only_after_idle_deadline():
    gate = PcmGate()
    accept(gate, now=10.0)
    assert gate.pop_request(10.149) is None
    assert gate.pop_request(10.151) == bytes(3_200)


def test_overflow_discards_the_entire_stale_buffer():
    gate = PcmGate(PcmContract(max_chunk_bytes=32_000, max_buffer_bytes=32_000))
    accept(gate, data=bytes(32_000))
    with pytest.raises(PcmContractError, match="overflow"):
        accept(gate, stamp=9.91, data=bytes(2))
    assert gate.buffered_bytes == 0


def test_contract_rejects_unsafe_configuration():
    with pytest.raises(ValueError):
        PcmContract(target_request_bytes=96_002, max_buffer_bytes=96_000)
