import ctypes
import ctypes.util
from types import SimpleNamespace

import pytest

from ferox_audio_go2.mic_bridge import (
    Go2MicBridgeCore,
    MicIngressError,
    OpusDecoder,
    UlawDecoder,
    chunk_to_message,
)
from ferox_audio_go2.profiles import get_profile
from ferox_audio_go2.stream_contract import (
    AudioStreamGuard,
    FLAG_DISCONTINUITY,
    FLAG_START,
)


class FakeDecoder:
    def __init__(self, output_bytes):
        self.output_bytes = output_bytes
        self.reset_count = 0

    def decode(self, payload):
        return self.output_bytes

    def reset(self):
        self.reset_count += 1

    def close(self):
        return


class Message:
    def __init__(self):
        self.header = SimpleNamespace(stamp=None, frame_id="")
        self.data = []


def test_five_opus_frames_make_one_exact_100ms_ferox_chunk():
    profile = get_profile("go2_opus48_audiohub_v1")
    core = Go2MicBridgeCore(
        profile,
        decoder=FakeDecoder(bytes(profile.mic_frame_samples * 2)),
        stream_id_factory=lambda: "go2-mic-a",
    )
    chunks = []
    for index in range(5):
        chunks += core.ingest(
            bytes(160), time_frame=index + 1,
            receive_steady_s=10.0 + index * 0.02,
            receive_time_ns=100 + index,
        )
    assert len(chunks) == 1
    chunk = chunks[0]
    assert len(chunk.payload) == 3_200
    assert (chunk.sequence, chunk.sample_offset, chunk.flags) == (0, 0, FLAG_START)
    message = chunk_to_message(Message, chunk, "stamp", "go2_02/mic")
    accepted = AudioStreamGuard().accept(message)
    assert accepted.sample_count == 1_600


def test_five_ulaw_frames_upsample_to_one_exact_chunk():
    profile = get_profile("go2_ulaw8_mic_only")
    core = Go2MicBridgeCore(
        profile,
        decoder=FakeDecoder(bytes(profile.mic_frame_samples * 2)),
        stream_id_factory=lambda: "go2-mic-ulaw",
    )
    chunks = []
    for index in range(5):
        chunks += core.ingest(
            bytes(160), time_frame=index + 1,
            receive_steady_s=20.0 + index * 0.02,
            receive_time_ns=200 + index,
        )
    assert len(chunks) == 1
    assert len(chunks[0].payload) == 3_200
    assert len(core.decode_latencies_ms) == 5
    assert core.source_to_chunk_latencies_ms[0] == pytest.approx(80.0)


def test_latency_telemetry_is_a_bounded_rolling_window():
    profile = get_profile("go2_opus48_audiohub_v1")
    core = Go2MicBridgeCore(
        profile,
        decoder=FakeDecoder(bytes(profile.mic_frame_samples * 2)),
        stream_id_factory=lambda: "go2-mic-window",
    )
    for index in range(3_005):
        core.ingest(
            bytes(160), time_frame=index + 1,
            receive_steady_s=30.0 + index * 0.02,
            receive_time_ns=1_000 + index,
        )
    assert core.accepted_source_frames == 3_005
    assert core.output_chunks == 601
    assert len(core.decode_latencies_ms) == 3_000
    assert core.decode_latencies_ms.maxlen == 3_000
    assert len(core.source_to_chunk_latencies_ms) == 600
    assert core.source_to_chunk_latencies_ms.maxlen == 600


def test_receive_gap_starts_explicit_discontinuity_stream():
    profile = get_profile("go2_opus48_audiohub_v1")
    ids = iter(("before-gap", "after-gap"))
    decoder = FakeDecoder(bytes(profile.mic_frame_samples * 2))
    core = Go2MicBridgeCore(
        profile, decoder=decoder, stream_id_factory=lambda: next(ids))
    for index in range(4):
        assert core.ingest(
            bytes(160), time_frame=index + 1,
            receive_steady_s=index * 0.02,
            receive_time_ns=index + 1,
        ) == []
    chunks = []
    for index in range(5):
        chunks += core.ingest(
            bytes(160), time_frame=10 + index,
            receive_steady_s=1.0 + index * 0.02,
            receive_time_ns=10 + index,
        )
    assert len(chunks) == 1
    assert chunks[0].stream_id == "after-gap"
    assert chunks[0].flags == (FLAG_START | FLAG_DISCONTINUITY)
    assert core.discontinuities == 1
    assert decoder.reset_count == 1


@pytest.mark.parametrize(
    "payload,time_frame,receive_s,reason",
    [
        (bytes(159), 1, 0.0, "frame is 159 bytes"),
        (bytes(160), -1, 0.0, "timestamp"),
        (bytes(160), 1, float("nan"), "steady receive time"),
    ],
)
def test_rejects_unqualified_source_frames(payload, time_frame, receive_s, reason):
    profile = get_profile("go2_opus48_audiohub_v1")
    core = Go2MicBridgeCore(
        profile,
        decoder=FakeDecoder(bytes(profile.mic_frame_samples * 2)),
        stream_id_factory=lambda: "safe-id",
    )
    with pytest.raises(MicIngressError, match=reason):
        core.ingest(payload, time_frame=time_frame, receive_steady_s=receive_s,
                    receive_time_ns=1)
    assert core.rejected_source_frames == 1


def test_non_monotonic_source_timestamp_invalidates_stream():
    profile = get_profile("go2_opus48_audiohub_v1")
    decoder = FakeDecoder(bytes(profile.mic_frame_samples * 2))
    core = Go2MicBridgeCore(profile, decoder=decoder,
                            stream_id_factory=lambda: "safe-id")
    core.ingest(bytes(160), time_frame=2, receive_steady_s=0.0, receive_time_ns=1)
    with pytest.raises(MicIngressError, match="did not increase"):
        core.ingest(bytes(160), time_frame=2, receive_steady_s=0.02, receive_time_ns=2)


def test_ulaw_decoder_produces_exact_s16le_samples():
    output = UlawDecoder().decode(bytes([255]) * 160)
    assert len(output) == 320


def test_real_libopus_encoder_decoder_abi_when_available():
    library = ctypes.util.find_library("opus")
    if not library:
        pytest.skip("libopus is not installed on this host")
    opus = ctypes.CDLL(library)
    opus.opus_encoder_create.argtypes = [
        ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    opus.opus_encoder_create.restype = ctypes.c_void_p
    opus.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    opus.opus_encode.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32]
    opus.opus_encode.restype = ctypes.c_int32
    error = ctypes.c_int(-1)
    encoder = opus.opus_encoder_create(48_000, 1, 2048, ctypes.byref(error))
    assert encoder and error.value == 0
    try:
        samples = (ctypes.c_int16 * 960)()
        encoded = (ctypes.c_ubyte * 4_000)()
        encoded_bytes = opus.opus_encode(encoder, samples, 960, encoded, 4_000)
        assert encoded_bytes > 0
        decoder = OpusDecoder(sample_rate=48_000, channels=1, frame_samples=960)
        try:
            pcm = decoder.decode(bytes(encoded[:encoded_bytes]))
        finally:
            decoder.close()
        assert len(pcm) == 960 * 2
    finally:
        opus.opus_encoder_destroy(encoder)
