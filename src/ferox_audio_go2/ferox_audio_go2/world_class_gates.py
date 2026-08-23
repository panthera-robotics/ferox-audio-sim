"""World-class Go2 hardware production gates. Fail-closed.

Transport PASS does not qualify speech. Firmware + operator intelligibility
+ live WER + speaker probe must all be present. This module never enables
speaker or motion. There is no AEC canceller in ferox-audio-go2; AEC gates
stay missing_measurement and speaker_enable_authorized stays false.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from .aec_unavailable import aec_unavailable

POLICY_ID = "ferox-audio-world-class-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLACEHOLDER = ("REPLACE", "REPLACE_ME", "REPLACE_AFTER_LISTENING", "REPLACE_WITH")


def _text(value: object) -> str:
    return str(value or "").strip()


def _digest(value: object) -> bool:
    return bool(_SHA256.fullmatch(_text(value).removeprefix("sha256:").lower()))


def _placeholder(value: object) -> bool:
    text = _text(value)
    return (not text) or any(token in text for token in _PLACEHOLDER)


def evaluate_go2_hardware_production(evidence: Mapping[str, object]) -> dict[str, object]:
    codec = evidence.get("codec_probe")
    speaker = evidence.get("speaker_probe")
    observation = evidence.get("observation")
    if not isinstance(codec, Mapping):
        codec = {}
    if not isinstance(observation, Mapping):
        observation = {}
    firmware_ok = not _placeholder(evidence.get("source_firmware"))
    intelligible = codec.get("operator_audio_intelligible") is True
    operator_id_ok = not _placeholder(codec.get("operator_id"))
    capture_ok = _digest(codec.get("capture_sha256"))
    recording_ok = _digest(codec.get("recording_sha256"))
    observation_ok = _digest(observation.get("capture_sha256"))
    hashes_agree = (
        capture_ok
        and observation_ok
        and _text(codec.get("capture_sha256")) == _text(observation.get("capture_sha256"))
    )
    speaker_ready = False
    speaker_supervised = False
    if isinstance(speaker, Mapping):
        speaker_supervised = (
            speaker.get("operator_heard_test_phrase") is True
            and speaker.get("operator_confirmed_no_delayed_replay_10s") is True
            and speaker.get("confirm_supervised_safe_volume") is True
        )
        speaker_ready = speaker_supervised
    live_wer = evidence.get("live_wer")
    live_campaign = live_wer is not None
    aec = aec_unavailable(evidence.get("aec") if isinstance(evidence.get("aec"), Mapping) else None)
    aec_gates = aec.get("gates") if isinstance(aec.get("gates"), list) else []
    aec_missing = bool(aec_gates) and all(
        isinstance(gate, Mapping)
        and gate.get("reason") == "missing_measurement"
        and gate.get("passed") is not True
        and gate.get("measured") is None
        for gate in aec_gates
    )
    checks = {
        "control_authorized_false": evidence.get("control_authorized") is not True,
        "firmware_fingerprint_present": firmware_ok,
        "operator_intelligible": intelligible,
        "operator_id_present": operator_id_ok,
        "capture_sha256_present": capture_ok,
        "recording_sha256_present": recording_ok,
        "observation_capture_matches_codec": hashes_agree,
        "live_speech_campaign_present": live_campaign is True,
        "speaker_probe_supervised": speaker_ready,
        "speech_claim_not_fabricated_without_operator": (
            codec.get("operator_audio_intelligible") is not True
            or operator_id_ok
        ),
        "aec_canceller_absent": aec.get("canceller_present") is False,
        "aec_gates_missing_measurement": aec_missing,
        "aec_tclw_not_claimed": aec.get("tclw_authorized") is False,
        "aec_hats_campaign_present": False,
        "speaker_enable_authorized_false": True,
    }
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "policy_id": POLICY_ID,
        "evidence_class": "go2_hardware_world_class_production",
        "control_authorized": False,
        "mic_enable_authorized": False,
        "speaker_enable_authorized": False,
        "production_ready": False,
        "tclw_authorized": False,
        "passed": False,
        "aec": aec,
        "checks": checks,
        "failures": failures,
        "reason": (
            "hardware production remains fail-closed until firmware, operator "
            "intelligibility, live multilingual WER, and supervised speaker "
            "evidence all pass; AEC gates are missing_measurement because "
            "ferox-audio-go2 has no canceller; this evaluator never flips "
            "enablement bits and never claims ETSI TCLw"
        ),
    }
