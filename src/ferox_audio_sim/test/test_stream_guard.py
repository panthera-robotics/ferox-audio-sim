from types import SimpleNamespace

import pytest

from ferox_audio_sim.stream_guard import StreamContractError, StreamGuard


def message(*, sequence=0, offset=0, flags=1, stream_id="stream-a"):
    return SimpleNamespace(
        contract_version=1,
        encoding=1,
        stream_id=stream_id,
        sequence=sequence,
        sample_offset=offset,
        flags=flags,
        channels=1,
        sample_width=2,
    )


def test_tracks_exact_stream_continuity():
    guard = StreamGuard()
    guard.accept(message(), bytes(3_200))
    guard.accept(message(sequence=1, offset=1_600, flags=2), bytes(640))
    guard.accept(message(stream_id="stream-b"), bytes(320))


def test_gap_is_fail_closed_until_new_start():
    guard = StreamGuard()
    guard.accept(message(), bytes(320))
    with pytest.raises(StreamContractError, match="sequence gap"):
        guard.accept(message(sequence=2, offset=160, flags=0), bytes(320))
    with pytest.raises(StreamContractError, match="first chunk"):
        guard.accept(message(sequence=1, offset=160, flags=0), bytes(320))
