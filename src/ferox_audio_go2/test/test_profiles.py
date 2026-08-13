from datetime import datetime, timezone
import json

import pytest

from ferox_audio_go2.profiles import (
    ProfileEvidenceError,
    get_profile,
    load_profile_evidence,
    validate_profile_evidence,
)


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def manifest(profile="go2_opus48_audiohub_v1", speaker=True):
    document = {
        "schema_version": 1,
        "robot_id": "go2_02",
        "hardware_profile": profile,
        "source_firmware": "go2-fw-1.1.7",
        "source_topic": "/audiosender",
        "source_type": "unitree_go/msg/AudioData",
        "captured_utc": "2026-08-13T12:00:00Z",
        "observation": {
            "duration_s": 12.0,
            "frame_count": 600,
            "nonempty_ratio": 1.0,
            "payload_bytes_mode": 160,
            "payload_bytes_min": 160,
            "payload_bytes_max": 160,
            "interval_p50_ms": 20.0,
            "interval_p95_ms": 22.0,
            "time_frame_monotonic": True,
            "framed_payload_sha256": "d" * 64,
            "capture_sha256": "e" * 64,
        },
        "codec_probe": {
            "decoder": "opus" if profile.startswith("go2_opus") else "ulaw",
            "decoded_frames": 600,
            "decode_errors": 0,
            "decoded_sample_rate": 48_000 if profile.startswith("go2_opus") else 8_000,
            "decoded_channels": 1,
            "operator_audio_intelligible": True,
            "operator_id": "operator-01",
            "capture_sha256": "e" * 64,
            "recording_sha256": "a" * 64,
        },
        "speaker_probe": ({
            "protocol": "audiohub_v1",
            "start_api_id": 4001,
            "block_api_id": 4003,
            "request_topic": "/api/audiohub/request",
            "response_topic": "/api/audiohub/response",
            "api_errors": 0,
            "operator_heard_test_phrase": True,
            "operator_confirmed_no_repeat": True,
            "operator_confirmed_no_truncation": True,
            "operator_confirmed_no_delayed_replay_10s": True,
            "operator_id": "operator-01",
            "sample_rate": 22_050,
            "channels": 1,
            "sample_width": 2,
            "test_wav_sha256": "b" * 64,
        } if speaker else None),
    }
    return document


def validate(document, *, require_speaker=True, profile=None):
    selected = get_profile(profile or document["hardware_profile"])
    return validate_profile_evidence(
        document,
        document_sha256="c" * 64,
        robot_id="go2_02",
        profile=selected,
        runtime_firmware="go2-fw-1.1.7",
        require_speaker=require_speaker,
        now=NOW,
    )


def test_accepts_exact_recent_operator_confirmed_profile():
    result = validate(manifest())
    assert result.profile.mic_codec == "opus"
    assert result.frame_count == 600
    assert result.speaker_confirmed is True


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda d: d.update(source_firmware="other"), "firmware"),
        (lambda d: d["observation"].update(payload_bytes_mode=80), "frame size"),
        (lambda d: d["observation"].update(payload_bytes_min=80), "frame size"),
        (lambda d: d["observation"].update(framed_payload_sha256="unknown"), "sha256"),
        (lambda d: d["codec_probe"].update(capture_sha256="f" * 64), "capture SHA"),
        (lambda d: d["observation"].update(interval_p50_ms=50), "cadence"),
        (lambda d: d["codec_probe"].update(operator_audio_intelligible=False), "operator"),
        (lambda d: d["codec_probe"].update(decode_errors=1), "decode errors"),
        (lambda d: d["speaker_probe"].update(sample_rate=16_000), "PCM format"),
        (lambda d: d["speaker_probe"].update(request_topic="/other"), "topics"),
        (lambda d: d.update(captured_utc="2026-01-01T00:00:00Z"), "stale"),
        (lambda d: d.update(source_topic="/rt/audiosender"), "topic"),
    ],
)
def test_rejects_profile_assumptions_without_matching_evidence(mutate, reason):
    document = manifest()
    mutate(document)
    with pytest.raises(ProfileEvidenceError, match=reason):
        validate(document)


def test_mic_only_profile_cannot_authorize_speaker():
    document = manifest("go2_ulaw8_mic_only", speaker=False)
    assert validate(document, require_speaker=False).speaker_confirmed is False
    with pytest.raises(ProfileEvidenceError, match="speaker output"):
        validate(document, require_speaker=True)


def test_evidence_file_is_sha_pinned(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(manifest(), sort_keys=True))
    with pytest.raises(ProfileEvidenceError, match="SHA-256 mismatch"):
        load_profile_evidence(
            path,
            expected_sha256="0" * 64,
            robot_id="go2_02",
            profile=get_profile("go2_opus48_audiohub_v1"),
            runtime_firmware="go2-fw-1.1.7",
            require_speaker=True,
            now=NOW,
        )
