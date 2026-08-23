"""Evidence-bound Go2 audio hardware profiles.

The Unitree ``AudioData`` payload is not self describing.  In particular, the
same 160-byte frame shape has been observed as both G.711 u-law at 8 kHz and
Opus at 48 kHz on different Go2 firmware.  A deployment therefore cannot infer
the codec from the DDS type or payload length.  It must select a named profile
and present a recent, hash-pinned hardware evidence manifest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


class ProfileEvidenceError(ValueError):
    """A hardware profile or its evidence is incomplete or contradictory."""


@dataclass(frozen=True)
class Go2AudioProfile:
    name: str
    mic_codec: str
    mic_sample_rate: int
    mic_frame_samples: int
    mic_frame_bytes: int
    output_sample_rate: int = 16_000
    speaker_protocol: str | None = None
    speaker_sample_rate: int | None = None

    @property
    def frame_duration_ms(self) -> float:
        return self.mic_frame_samples * 1000.0 / self.mic_sample_rate


PROFILES = {
    "go2_opus48_audiohub_v1": Go2AudioProfile(
        name="go2_opus48_audiohub_v1",
        mic_codec="opus",
        mic_sample_rate=48_000,
        mic_frame_samples=960,
        mic_frame_bytes=160,
        speaker_protocol="audiohub_v1",
        speaker_sample_rate=22_050,
    ),
    "go2_ulaw8_mic_only": Go2AudioProfile(
        name="go2_ulaw8_mic_only",
        mic_codec="ulaw",
        mic_sample_rate=8_000,
        mic_frame_samples=160,
        mic_frame_bytes=160,
        speaker_protocol=None,
        speaker_sample_rate=None,
    ),
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROBOT_ID = re.compile(r"^go2_[0-9]{2}$")
_PORTABLE = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}$")
_TOP_KEYS = {
    "schema_version", "robot_id", "hardware_profile", "source_firmware",
    "source_topic", "source_type", "subscriber_reliability", "captured_utc", "observation",
    "codec_probe", "speaker_probe",
}


def get_profile(name: str) -> Go2AudioProfile:
    try:
        return PROFILES[str(name)]
    except KeyError as exc:
        raise ProfileEvidenceError(
            f"unknown Go2 audio profile {name!r}; expected one of "
            f"{', '.join(sorted(PROFILES))}"
        ) from exc


def _number(mapping: dict[str, Any], key: str) -> float:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileEvidenceError(f"evidence {key} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ProfileEvidenceError(f"evidence {key} must be finite")
    return result


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProfileEvidenceError("captured_utc must be an ISO-8601 UTC value ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProfileEvidenceError("captured_utc is invalid") from exc
    if parsed.tzinfo is None:
        raise ProfileEvidenceError("captured_utc must be timezone aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ValidatedProfileEvidence:
    profile: Go2AudioProfile
    sha256: str
    robot_id: str
    source_firmware: str
    captured_utc: str
    frame_count: int
    duration_s: float
    speaker_confirmed: bool


def validate_profile_evidence(
    document: dict[str, Any],
    *,
    document_sha256: str,
    robot_id: str,
    profile: Go2AudioProfile,
    runtime_firmware: str,
    require_speaker: bool,
    now: datetime | None = None,
    max_age_days: float = 30.0,
) -> ValidatedProfileEvidence:
    """Validate exact hardware evidence before interpreting or emitting audio."""
    if set(document) != _TOP_KEYS:
        missing = sorted(_TOP_KEYS - set(document))
        extra = sorted(set(document) - _TOP_KEYS)
        raise ProfileEvidenceError(
            f"evidence top-level schema mismatch; missing={missing}, extra={extra}")
    if document.get("schema_version") != 2:
        raise ProfileEvidenceError("evidence schema_version must be 2")
    if not _SHA256.fullmatch(document_sha256):
        raise ProfileEvidenceError("evidence SHA-256 must be 64 lowercase hex characters")
    if not _ROBOT_ID.fullmatch(robot_id) or document.get("robot_id") != robot_id:
        raise ProfileEvidenceError("evidence robot_id does not match this deployment")
    if document.get("hardware_profile") != profile.name:
        raise ProfileEvidenceError("evidence hardware_profile does not match deployment")
    if not _PORTABLE.fullmatch(str(runtime_firmware)):
        raise ProfileEvidenceError("runtime_firmware must be an explicit portable fingerprint")
    if document.get("source_firmware") != runtime_firmware:
        raise ProfileEvidenceError("runtime firmware differs from the qualified audio firmware")
    if document.get("source_topic") != "/audiosender":
        raise ProfileEvidenceError("qualified Go2 microphone topic must be /audiosender")
    if document.get("source_type") != "unitree_go/msg/AudioData":
        raise ProfileEvidenceError("qualified Go2 microphone type must be unitree_go/msg/AudioData")
    if document.get("subscriber_reliability") != "reliable":
        raise ProfileEvidenceError("qualified Go2 microphone capture must use reliable QoS")

    captured = _parse_utc(document.get("captured_utc"))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_s = (current - captured).total_seconds()
    if age_s < -300:
        raise ProfileEvidenceError("audio evidence is dated in the future")
    if not 0 < max_age_days <= 365 or age_s > max_age_days * 86_400:
        raise ProfileEvidenceError("audio evidence is stale")

    observation = document.get("observation")
    if not isinstance(observation, dict):
        raise ProfileEvidenceError("observation must be an object")
    expected_observation_keys = {
        "duration_s", "frame_count", "nonempty_ratio", "payload_bytes_mode",
        "payload_bytes_min", "payload_bytes_max", "interval_p50_ms",
        "interval_p95_ms", "interval_p99_ms", "interval_max_ms",
        "effective_rate_hz", "receive_gap_count", "receive_gap_fraction",
        "receive_burst_count", "receive_burst_fraction",
        "time_frame_monotonic", "time_frame_step_mode",
        "time_frame_step_outlier_count", "framed_payload_sha256", "capture_sha256",
    }
    if set(observation) != expected_observation_keys:
        raise ProfileEvidenceError("observation schema mismatch")
    duration_s = _number(observation, "duration_s")
    frame_count_value = _number(observation, "frame_count")
    if not frame_count_value.is_integer():
        raise ProfileEvidenceError("frame_count must be an integer")
    frame_count = int(frame_count_value)
    if duration_s < 10.0 or frame_count < 400:
        raise ProfileEvidenceError("audio observation must contain at least 10 s and 400 frames")
    if _number(observation, "nonempty_ratio") != 1.0:
        raise ProfileEvidenceError("audio observation contains empty frames")
    observed_sizes = {
        int(_number(observation, key))
        for key in ("payload_bytes_mode", "payload_bytes_min", "payload_bytes_max")
    }
    if observed_sizes != {profile.mic_frame_bytes}:
        raise ProfileEvidenceError(
            "all observed frame sizes must match the selected profile")
    if not _SHA256.fullmatch(str(observation.get("framed_payload_sha256", ""))):
        raise ProfileEvidenceError("observation framed_payload_sha256 is invalid")
    if not _SHA256.fullmatch(str(observation.get("capture_sha256", ""))):
        raise ProfileEvidenceError("observation capture_sha256 is invalid")
    expected_ms = profile.frame_duration_ms
    expected_rate_hz = 1000.0 / expected_ms
    effective_rate_hz = _number(observation, "effective_rate_hz")
    if not expected_rate_hz * 0.98 <= effective_rate_hz <= expected_rate_hz * 1.02:
        raise ProfileEvidenceError("audio effective frame rate does not match the profile")
    p50 = _number(observation, "interval_p50_ms")
    p95 = _number(observation, "interval_p95_ms")
    p99 = _number(observation, "interval_p99_ms")
    interval_max = _number(observation, "interval_max_ms")
    if not expected_ms * 0.70 <= p50 <= expected_ms * 1.30:
        raise ProfileEvidenceError("audio frame median cadence does not match the profile")
    if not 0 <= p50 <= p95 <= p99 <= interval_max:
        raise ProfileEvidenceError("audio receive interval percentiles are inconsistent")
    # The current Go2 transport delivers complete source frames in scheduler
    # bursts.  Source timestamp continuity is the loss gate; receive intervals
    # remain a bounded transport-latency signal and must never be mistaken for
    # source-frame loss.
    if p99 > expected_ms * 4.0 or interval_max > expected_ms * 5.0:
        raise ProfileEvidenceError("audio receive transport stall exceeds 100 ms")
    interval_count = max(1, frame_count - 1)
    for count_key, fraction_key in (
        ("receive_gap_count", "receive_gap_fraction"),
        ("receive_burst_count", "receive_burst_fraction"),
    ):
        count_value = _number(observation, count_key)
        fraction = _number(observation, fraction_key)
        if not count_value.is_integer() or not 0 <= count_value <= frame_count - 1:
            raise ProfileEvidenceError(f"{count_key} is invalid")
        if not 0.0 <= fraction <= 1.0:
            raise ProfileEvidenceError(f"{fraction_key} is invalid")
        expected_fraction = int(count_value) / interval_count
        if not math.isclose(fraction, expected_fraction, abs_tol=1e-6):
            raise ProfileEvidenceError(
                f"{fraction_key} does not match {count_key}")
    source_outliers = _number(observation, "time_frame_step_outlier_count")
    if not source_outliers.is_integer() or source_outliers != 0:
        raise ProfileEvidenceError("AudioData.time_frame step is inconsistent")
    source_step = observation.get("time_frame_step_mode")
    if isinstance(source_step, bool) or not isinstance(source_step, int) or source_step <= 0:
        raise ProfileEvidenceError("AudioData.time_frame step mode is invalid")
    if observation.get("time_frame_monotonic") is not True:
        raise ProfileEvidenceError("AudioData.time_frame was not monotonic during qualification")

    codec = document.get("codec_probe")
    if not isinstance(codec, dict) or set(codec) != {
        "decoder", "decoded_frames", "decode_errors", "decoded_sample_rate",
        "decoded_channels", "operator_audio_intelligible", "operator_id",
        "capture_sha256", "recording_sha256",
    }:
        raise ProfileEvidenceError("codec_probe schema mismatch")
    if codec.get("decoder") != profile.mic_codec:
        raise ProfileEvidenceError("codec probe decoder does not match the selected profile")
    decoded_frames = _number(codec, "decoded_frames")
    if not decoded_frames.is_integer() or decoded_frames < 400:
        raise ProfileEvidenceError("codec probe must decode at least 400 frames")
    if _number(codec, "decode_errors") != 0:
        raise ProfileEvidenceError("codec probe reported decode errors")
    if int(_number(codec, "decoded_sample_rate")) != profile.mic_sample_rate:
        raise ProfileEvidenceError("codec probe sample rate does not match profile")
    if int(_number(codec, "decoded_channels")) != 1:
        raise ProfileEvidenceError("codec probe must be mono")
    if codec.get("operator_audio_intelligible") is not True:
        raise ProfileEvidenceError("an operator has not confirmed intelligible decoded audio")
    if not _PORTABLE.fullmatch(str(codec.get("operator_id", ""))):
        raise ProfileEvidenceError("codec probe operator_id is invalid")
    if not _SHA256.fullmatch(str(codec.get("recording_sha256", ""))):
        raise ProfileEvidenceError("codec probe recording_sha256 is invalid")
    if codec.get("capture_sha256") != observation.get("capture_sha256"):
        raise ProfileEvidenceError(
            "codec probe capture SHA-256 does not match the discovery artifact")

    speaker = document.get("speaker_probe")
    speaker_confirmed = False
    if speaker is not None:
        if not isinstance(speaker, dict) or set(speaker) != {
            "protocol", "start_api_id", "block_api_id", "api_errors",
            "operator_heard_test_phrase", "operator_id", "test_wav_sha256",
            "operator_confirmed_no_repeat", "operator_confirmed_no_truncation",
            "operator_confirmed_no_delayed_replay_10s", "sample_rate",
            "channels", "sample_width", "request_topic", "response_topic",
        }:
            raise ProfileEvidenceError("speaker_probe schema mismatch")
        if speaker.get("protocol") != profile.speaker_protocol:
            raise ProfileEvidenceError("speaker probe protocol does not match profile")
        if speaker.get("start_api_id") != 4001 or speaker.get("block_api_id") != 4003:
            raise ProfileEvidenceError("speaker probe API IDs do not match audiohub_v1")
        if (
            speaker.get("request_topic") != "/api/audiohub/request"
            or speaker.get("response_topic") != "/api/audiohub/response"
        ):
            raise ProfileEvidenceError("speaker probe audiohub topics do not match profile")
        if (
            speaker.get("sample_rate") != profile.speaker_sample_rate
            or speaker.get("channels") != 1
            or speaker.get("sample_width") != 2
        ):
            raise ProfileEvidenceError("speaker probe PCM format does not match profile")
        if speaker.get("api_errors") != 0:
            raise ProfileEvidenceError("speaker probe reported API errors")
        if speaker.get("operator_heard_test_phrase") is not True:
            raise ProfileEvidenceError("an operator has not heard the speaker test phrase")
        for key in (
            "operator_confirmed_no_repeat", "operator_confirmed_no_truncation",
            "operator_confirmed_no_delayed_replay_10s",
        ):
            if speaker.get(key) is not True:
                raise ProfileEvidenceError(
                    f"speaker probe lacks operator confirmation: {key}")
        if not _PORTABLE.fullmatch(str(speaker.get("operator_id", ""))):
            raise ProfileEvidenceError("speaker probe operator_id is invalid")
        if not _SHA256.fullmatch(str(speaker.get("test_wav_sha256", ""))):
            raise ProfileEvidenceError("speaker probe test_wav_sha256 is invalid")
        speaker_confirmed = True
    if require_speaker and (profile.speaker_protocol is None or not speaker_confirmed):
        raise ProfileEvidenceError("speaker output requires matching operator-confirmed evidence")

    return ValidatedProfileEvidence(
        profile=profile,
        sha256=document_sha256,
        robot_id=robot_id,
        source_firmware=runtime_firmware,
        captured_utc=str(document["captured_utc"]),
        frame_count=frame_count,
        duration_s=duration_s,
        speaker_confirmed=speaker_confirmed,
    )


def load_profile_evidence(
    path: str | Path,
    *,
    expected_sha256: str,
    robot_id: str,
    profile: Go2AudioProfile,
    runtime_firmware: str,
    require_speaker: bool,
    now: datetime | None = None,
    max_age_days: float = 30.0,
) -> ValidatedProfileEvidence:
    evidence_path = Path(path)
    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise ProfileEvidenceError("evidence manifest must be a regular non-symlink file")
    raw = evidence_path.read_bytes()
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ProfileEvidenceError(
            f"evidence SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileEvidenceError("evidence manifest is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ProfileEvidenceError("evidence manifest root must be an object")
    return validate_profile_evidence(
        document,
        document_sha256=actual_sha256,
        robot_id=robot_id,
        profile=profile,
        runtime_firmware=runtime_firmware,
        require_speaker=require_speaker,
        now=now,
        max_age_days=max_age_days,
    )
