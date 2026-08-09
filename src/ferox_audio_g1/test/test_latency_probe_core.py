import struct

import pytest

from ferox_audio_g1.latency_probe_core import synthesize_chirp
from ferox_audio_g1.unitree_voice_contract import (
    GET_VOLUME_API_ID,
    PLAY_STREAM_API_ID,
    SET_VOLUME_API_ID,
    STOP_PLAY_API_ID,
)


def test_voice_api_ids_match_the_official_g1_audio_contract():
    assert (PLAY_STREAM_API_ID, STOP_PLAY_API_ID) == (1003, 1004)
    assert (GET_VOLUME_API_ID, SET_VOLUME_API_ID) == (1005, 1006)


def test_chirp_is_bounded_deterministic_16khz_s16le():
    first = synthesize_chirp(duration_ms=400, amplitude=0.08)
    second = synthesize_chirp(duration_ms=400, amplitude=0.08)
    assert first == second
    assert len(first) == 16_000 * 2 * 400 // 1_000
    samples = struct.unpack(f"<{len(first) // 2}h", first)
    assert samples[0] == 0
    assert samples[-1] == 0
    assert max(abs(value) for value in samples) <= round(32_767 * 0.08)
    assert max(abs(value) for value in samples) > 2_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"duration_ms": 99},
        {"duration_ms": 1_001},
        {"amplitude": 0.0},
        {"amplitude": 0.11},
        {"start_hz": 2_000.0, "end_hz": 1_000.0},
    ],
)
def test_chirp_rejects_unbounded_stimuli(kwargs):
    with pytest.raises(ValueError):
        synthesize_chirp(**kwargs)
