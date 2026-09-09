import hashlib
import io
import wave

import pytest

from ferox_audio_go2.speaker_probe import (
    SpeakerProbeError,
    canonicalize_probe_wav,
    main,
    prepare_main,
    prepare_probe_wav,
)


def wav_bytes(*, rate=22_050, channels=1, width=2, frames=22_050):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setframerate(rate)
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.writeframes(bytes(frames * channels * width))
    return output.getvalue()


def test_prepares_exact_bounded_on_wire_speaker_probe(tmp_path):
    source = tmp_path / "probe.wav"
    source.write_bytes(wav_bytes())
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    plan, metadata = prepare_probe_wav(
        source, expected_on_wire_sha256=expected)
    assert plan.requests[0].api_id == 4001
    assert metadata["duration_s"] == 1.0
    assert metadata["test_wav_sha256"] == expected


@pytest.mark.parametrize(
    "payload,reason",
    [
        (wav_bytes(rate=16_000), "22050"),
        (wav_bytes(frames=44_101), "duration"),
    ],
)
def test_rejects_wrong_format_or_unbounded_probe(tmp_path, payload, reason):
    source = tmp_path / "probe.wav"
    source.write_bytes(payload)
    with pytest.raises(SpeakerProbeError, match=reason):
        prepare_probe_wav(
            source,
            expected_on_wire_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_rejects_unreviewed_on_wire_wav_hash(tmp_path):
    source = tmp_path / "probe.wav"
    source.write_bytes(wav_bytes())
    with pytest.raises(SpeakerProbeError, match="mismatch"):
        prepare_probe_wav(source, expected_on_wire_sha256="0" * 64)


def test_offline_prepare_writes_exact_private_canonical_wav(tmp_path):
    source = tmp_path / "source.wav"
    output = tmp_path / "canonical.wav"
    source.write_bytes(wav_bytes())
    expected_plan, _ = canonicalize_probe_wav(source)
    prepare_main(["--wav", str(source), "--output", str(output)])
    assert output.read_bytes() == expected_plan.wav_bytes
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit):
        prepare_main(["--wav", str(source), "--output", str(output)])


def test_hardware_probe_refuses_missing_network_contract(tmp_path, monkeypatch):
    source = tmp_path / "canonical.wav"
    source.write_bytes(wav_bytes())
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    for key in (
        "ROS_DOMAIN_ID", "RMW_IMPLEMENTATION", "FEROX_DDS_INTERFACE",
        "CYCLONEDDS_URI",
    ):
        monkeypatch.delenv(key, raising=False)
    output = tmp_path / "result.json"
    with pytest.raises(SystemExit):
        main([
            "--wav", str(source),
            "--expected-on-wire-sha256", digest,
            "--output", str(output),
            "--robot-id", "go2_02",
            "--runtime-firmware", "go2-fw-test",
            "--operator-id", "operator-01",
            "--confirm-supervised-safe-volume",
        ])
    assert not output.exists()
