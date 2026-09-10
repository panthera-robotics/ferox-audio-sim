"""No ROS, Opus library, network, playback or speech corpus required."""
import pytest
from types import SimpleNamespace

from ferox_audio_go2.mic_bridge import Go2MicBridgeCore, MicIngressError, chunk_to_message
from ferox_audio_go2.profiles import get_profile
from ferox_audio_go2.stream_contract import AudioStreamGuard


class FakeDecoder:
    def __init__(self, output):
        self.output = output

    def decode(self, payload):
        return self.output

    def reset(self):
        pass


class Message:
    def __init__(self):
        self.header = SimpleNamespace(stamp=None, frame_id="")


@pytest.mark.parametrize("replacement", [frozenset({b"a", b"b"}), frozenset({b"b"})])
def test_late_or_same_count_replacement_gets_start(replacement):
    profile = get_profile("go2_opus48_audiohub_v1")
    core = Go2MicBridgeCore(profile, decoder=FakeDecoder(bytes(1920)))
    index = 0

    def next_chunk(subscribers):
        nonlocal index
        chunks = []
        for _ in range(5):
            core.observe_subscribers(subscribers)
            chunks += core.ingest(bytes(160), time_frame=index + 1,
                                  receive_steady_s=10 + index * .02,
                                  receive_time_ns=100 + index)
            index += 1
        return chunk_to_message(Message, chunks[0], "stamp", "go2_02/mic")

    existing = AudioStreamGuard()
    existing.accept(next_chunk(frozenset({b"a"})))
    message = next_chunk(replacement)
    AudioStreamGuard().accept(message)
    existing.accept(message)
    continued = next_chunk(replacement)
    assert continued.sequence == 1
    existing.accept(continued)


def test_subscriber_change_does_not_erase_source_replay_guard():
    core = Go2MicBridgeCore(get_profile("go2_opus48_audiohub_v1"),
                            decoder=FakeDecoder(bytes(1920)))
    core.ingest(bytes(160), time_frame=1, receive_steady_s=10,
                receive_time_ns=100)
    core.observe_subscribers(frozenset({b"new"}))
    with pytest.raises(MicIngressError, match="did not increase"):
        core.ingest(bytes(160), time_frame=1, receive_steady_s=10.02,
                    receive_time_ns=120)


def test_departure_keeps_pending_audio_and_stream():
    core = Go2MicBridgeCore(get_profile("go2_opus48_audiohub_v1"),
                            decoder=FakeDecoder(bytes(1920)))
    core.observe_subscribers(frozenset({b"a", b"b"}))
    core.ingest(bytes(160), time_frame=1, receive_steady_s=10,
                receive_time_ns=100)
    before = (core._stream_id, bytes(core._pending), core.discontinuities)
    core.observe_subscribers(frozenset({b"a"}))
    assert (core._stream_id, bytes(core._pending), core.discontinuities) == before
