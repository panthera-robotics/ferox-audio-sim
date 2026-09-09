import base64
import io
import json
import wave

import pytest

from ferox_audio_go2.decode_capture import (
    CaptureDecodeError,
    decode_frames,
    read_capture,
    wav_bytes,
    write_decode_bundle,
    write_new,
)


def write_capture(path, payloads):
    lines = []
    for index, payload in enumerate(payloads):
        lines.append(json.dumps({
            "receive_steady_s": index * 0.02,
            "time_frame": index + 1,
            "payload_b64": base64.b64encode(payload).decode(),
        }))
    path.write_text("\n".join(lines) + "\n")


def test_ulaw_capture_decodes_to_auditable_wav(tmp_path):
    source = tmp_path / "frames.jsonl"
    write_capture(source, [bytes([255]) * 160 for _ in range(5)])
    frames = read_capture(source)
    pcm, count, rate = decode_frames(frames, profile_name="go2_ulaw8_mic_only")
    assert (count, rate, len(pcm)) == (5, 8_000, 1_600)
    rendered = wav_bytes(pcm, sample_rate=rate)
    with wave.open(io.BytesIO(rendered), "rb") as reader:
        assert reader.getframerate() == 8_000
        assert reader.getnframes() == 800


def test_capture_reader_rejects_non_monotonic_source_and_bad_frame(tmp_path):
    source = tmp_path / "frames.jsonl"
    write_capture(source, [bytes(160), bytes(160)])
    lines = source.read_text().splitlines()
    second = json.loads(lines[1])
    second["time_frame"] = 1
    source.write_text(lines[0] + "\n" + json.dumps(second) + "\n")
    with pytest.raises(CaptureDecodeError, match="not monotonic"):
        read_capture(source)
    with pytest.raises(CaptureDecodeError, match="profile requires"):
        decode_frames([bytes(159)], profile_name="go2_ulaw8_mic_only")


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("time_frame", True, "integer"),
        ("time_frame", 1.5, "integer"),
        ("receive_steady_s", True, "numeric"),
        ("receive_steady_s", float("nan"), "timestamp"),
    ],
)
def test_capture_reader_rejects_ambiguous_or_nonfinite_metadata(
        tmp_path, field, value, reason):
    source = tmp_path / "frames.jsonl"
    write_capture(source, [bytes(160)])
    item = json.loads(source.read_text())
    item[field] = value
    source.write_text(json.dumps(item) + "\n")
    with pytest.raises(CaptureDecodeError, match=reason):
        read_capture(source)


def test_decode_outputs_never_overwrite(tmp_path):
    output = tmp_path / "decoded.wav"
    write_new(output, b"first")
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(CaptureDecodeError, match="already exists"):
        write_new(output, b"second")


def test_decode_bundle_never_leaves_a_partial_result(tmp_path):
    wav_output = tmp_path / "decoded.wav"
    probe_output = tmp_path / "probe.json"
    probe_output.write_text("owned-by-operator\n")
    with pytest.raises(CaptureDecodeError, match="partial decode"):
        write_decode_bundle(wav_output, probe_output, b"wav", {"ok": True})
    assert not wav_output.exists()
    assert probe_output.read_text() == "owned-by-operator\n"


def test_decode_main_writes_physical_metrics_and_refuses_speech_claim(tmp_path):
    source = tmp_path / "frames.jsonl"
    write_capture(source, [bytes([255]) * 160 for _ in range(3)])
    wav_output = tmp_path / "decoded.wav"
    probe_output = tmp_path / "probe.json"
    from ferox_audio_go2.decode_capture import main as decode_main
    decode_main([
        "--capture", str(source),
        "--profile", "go2_ulaw8_mic_only",
        "--wav-output", str(wav_output),
        "--probe-output", str(probe_output),
    ])
    probe = json.loads(probe_output.read_text())
    assert probe["speech_claim_authorized"] is False
    assert probe["operator_audio_intelligible"] is False
    assert probe["signal_metrics"]["interpretation"] == "physical_pcm_only"
    assert probe["signal_metrics"]["sample_count"] == 480
    output = tmp_path / "same"
    with pytest.raises(CaptureDecodeError, match="distinct"):
        write_decode_bundle(output, output, b"wav", {"ok": True})
    assert not output.exists()
