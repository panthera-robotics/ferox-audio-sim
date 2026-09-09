from __future__ import annotations

from array import array
import sys

import pytest

from ferox_audio_g1.pcm_resample import (
    LinearPcmResampler,
    PcmResampleError,
    resample_s16le,
)


def pcm(values: list[int]) -> bytes:
    output = array("h", values)
    if sys.byteorder != "little":
        output.byteswap()
    return output.tobytes()


def values(data: bytes) -> list[int]:
    output = array("h")
    output.frombytes(data)
    if sys.byteorder != "little":
        output.byteswap()
    return list(output)


def test_streaming_chunk_boundaries_are_bit_identical_to_one_shot():
    source = pcm([((index * 7919 + 32_768) % 65_536) - 32_768
                  for index in range(5_103)])
    expected = resample_s16le(source, source_rate=22_050, target_rate=16_000)
    resampler = LinearPcmResampler(22_050, 16_000)
    chunks = [source[:2_202], source[2_202:8_000], source[8_000:19_998], source[19_998:]]
    actual = b"".join(
        resampler.feed(chunk, end=index == len(chunks) - 1)
        for index, chunk in enumerate(chunks)
    )
    assert actual == expected
    assert len(values(actual)) == round(5_103 * 16_000 / 22_050)


def test_resampler_preserves_constant_pcm_and_exact_duration():
    source_samples = 22_050 * 2
    output = values(resample_s16le(
        pcm([1234] * source_samples), source_rate=22_050, target_rate=16_000))
    assert len(output) == 32_000
    assert set(output) == {1234}


def test_identity_rate_is_byte_exact():
    source = pcm([-32768, -1, 0, 1, 32767])
    assert resample_s16le(source, source_rate=16_000, target_rate=16_000) == source


@pytest.mark.parametrize("data", [b"x", b""])
def test_invalid_or_empty_final_stream_is_rejected(data: bytes):
    with pytest.raises(PcmResampleError):
        resample_s16le(data, source_rate=22_050, target_rate=16_000)


def test_data_after_end_is_rejected():
    resampler = LinearPcmResampler(22_050, 16_000)
    resampler.feed(pcm([1, 2, 3]), end=True)
    with pytest.raises(PcmResampleError, match="after the end"):
        resampler.feed(pcm([4]))
