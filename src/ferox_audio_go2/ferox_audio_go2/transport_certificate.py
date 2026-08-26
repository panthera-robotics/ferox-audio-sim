"""Build a digest-bound, fail-closed Go2 audio transport certificate.

The certificate qualifies only the observed ``/audiosender`` transport and
Opus decode summaries.  It does not claim intelligible speech, ASR quality,
speaker safety, echo cancellation, or production readiness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path


POLICY_ID = "ferox-go2-audio-transport-v1"
STRICT_RECEIVE_INTERVAL_P95_MS = 40.0


class TransportCertificateError(RuntimeError):
    """Input evidence is malformed or certificate output is unsafe."""


def _read_json(path: str | Path) -> tuple[dict[str, object], dict[str, object]]:
    source = Path(path)
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise TransportCertificateError(f"cannot read evidence: {source}") from exc
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransportCertificateError(f"invalid JSON evidence: {source}") from exc
    if not isinstance(document, dict):
        raise TransportCertificateError(f"evidence must be a JSON object: {source}")
    binding = {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    return document, binding


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _digest(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if len(text) == 64 and all(character in "0123456789abcdef" for character in text):
        return text
    return None


def _evaluate_lane(
    *,
    name: str,
    expected_reliability: str,
    observation: Mapping[str, object],
    codec: Mapping[str, object],
) -> tuple[dict[str, bool], dict[str, object]]:
    duration_s = _number(observation.get("duration_s"))
    rate_hz = _number(observation.get("effective_rate_hz"))
    p95_ms = _number(observation.get("interval_p95_ms"))
    frame_count = _integer(observation.get("frame_count"))
    decoded_frames = _integer(codec.get("decoded_frames"))
    observation_capture = _digest(observation.get("capture_sha256"))
    codec_capture = _digest(codec.get("capture_sha256"))

    checks = {
        f"{name}_observation_schema_v2": observation.get("schema_version") == 2,
        f"{name}_source_topic_audiosender": observation.get("source_topic") == "/audiosender",
        f"{name}_source_type_audio_data": observation.get("source_type") == "unitree_go/msg/AudioData",
        f"{name}_subscriber_reliability": observation.get("subscriber_reliability") == expected_reliability,
        f"{name}_duration_at_least_120s": duration_s is not None and duration_s >= 120.0,
        f"{name}_frame_count_at_least_6000": frame_count is not None and frame_count >= 6_000,
        f"{name}_rate_49_to_51hz": rate_hz is not None and 49.0 <= rate_hz <= 51.0,
        f"{name}_payload_exactly_160_bytes": all(
            observation.get(field) == 160
            for field in ("payload_bytes_min", "payload_bytes_max", "payload_bytes_mode")
        ),
        f"{name}_all_payloads_nonempty": _number(observation.get("nonempty_ratio")) == 1.0,
        f"{name}_source_time_monotonic": observation.get("time_frame_monotonic") is True,
        f"{name}_source_step_200000": observation.get("time_frame_step_mode") == 200_000,
        f"{name}_zero_source_step_outliers": _integer(
            observation.get("time_frame_step_outlier_count")) == 0,
        f"{name}_decoder_opus": codec.get("decoder") == "opus",
        f"{name}_decoded_48khz_mono": (
            codec.get("decoded_sample_rate") == 48_000
            and codec.get("decoded_channels") == 1
        ),
        f"{name}_all_frames_decoded": (
            frame_count is not None
            and decoded_frames is not None
            and decoded_frames == frame_count
        ),
        f"{name}_zero_decode_errors": _integer(codec.get("decode_errors")) == 0,
        f"{name}_capture_digest_matches": (
            observation_capture is not None
            and codec_capture is not None
            and observation_capture == codec_capture
        ),
        f"{name}_receive_interval_p95_at_most_40ms": (
            p95_ms is not None and 0.0 <= p95_ms <= STRICT_RECEIVE_INTERVAL_P95_MS
        ),
    }
    metrics = {
        "duration_s": duration_s,
        "frame_count": frame_count,
        "effective_rate_hz": rate_hz,
        "receive_interval_p95_ms": p95_ms,
        "decoded_frames": decoded_frames,
        "decode_errors": codec.get("decode_errors"),
        "capture_sha256": observation_capture,
        "framed_payload_sha256": _digest(observation.get("framed_payload_sha256")),
        "recording_sha256": _digest(codec.get("recording_sha256")),
        "operator_audio_intelligible": codec.get("operator_audio_intelligible") is True,
    }
    return checks, metrics


def evaluate_transport_evidence(
    *,
    reliable_observation: Mapping[str, object],
    reliable_codec: Mapping[str, object],
    best_effort_observation: Mapping[str, object],
    best_effort_codec: Mapping[str, object],
    input_bindings: Mapping[str, object] | None = None,
) -> dict[str, object]:
    reliable_checks, reliable_metrics = _evaluate_lane(
        name="reliable",
        expected_reliability="reliable",
        observation=reliable_observation,
        codec=reliable_codec,
    )
    best_effort_checks, best_effort_metrics = _evaluate_lane(
        name="best_effort",
        expected_reliability="best_effort",
        observation=best_effort_observation,
        codec=best_effort_codec,
    )
    checks = {**reliable_checks, **best_effort_checks}
    checks.update({
        "dual_qos_framed_payload_digest_matches": (
            reliable_metrics["framed_payload_sha256"] is not None
            and reliable_metrics["framed_payload_sha256"]
            == best_effort_metrics["framed_payload_sha256"]
        ),
        "dual_qos_recording_digest_matches": (
            reliable_metrics["recording_sha256"] is not None
            and reliable_metrics["recording_sha256"]
            == best_effort_metrics["recording_sha256"]
        ),
    })
    latency_checks = {
        name for name in checks if name.endswith("receive_interval_p95_at_most_40ms")
    }
    integrity_checks = {name: passed for name, passed in checks.items() if name not in latency_checks}
    transport_integrity_passed = all(integrity_checks.values())
    strict_transport_gate_passed = transport_integrity_passed and all(
        checks[name] for name in latency_checks
    )
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "schema_version": 1,
        "policy_id": POLICY_ID,
        "policy_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "evidence_class": "go2_audio_dual_qos_transport_certificate",
        "thresholds": {
            "minimum_duration_s": 120.0,
            "minimum_frame_count": 6_000,
            "source_rate_hz": [49.0, 51.0],
            "receive_interval_p95_max_ms": STRICT_RECEIVE_INTERVAL_P95_MS,
        },
        "inputs": dict(input_bindings or {}),
        "lanes": {
            "reliable": reliable_metrics,
            "best_effort": best_effort_metrics,
        },
        "checks": checks,
        "failures": failures,
        "transport_integrity_passed": transport_integrity_passed,
        "strict_transport_gate_passed": strict_transport_gate_passed,
        "passed": strict_transport_gate_passed,
        "production_ready": False,
        "operator_audio_intelligible": False,
        "mic_enable_authorized": False,
        "speaker_enable_authorized": False,
        "control_authorized": False,
        "boundary": (
            "Digest-bound summary qualification only. Raw capture bytes were not "
            "re-read by this tool. This certificate does not establish speech "
            "intelligibility, multilingual WER, speaker safety, AEC performance, "
            "or production readiness and never authorizes hardware or control."
        ),
    }


def _write_new_private(path: str | Path, document: Mapping[str, object]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise TransportCertificateError(
            f"output already exists; refusing overwrite: {output}") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(rendered)


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reliable-observation", required=True)
    parser.add_argument("--reliable-codec", required=True)
    parser.add_argument("--best-effort-observation", required=True)
    parser.add_argument("--best-effort-codec", required=True)
    parser.add_argument("--output", required=True)
    options = parser.parse_args(args)
    output = Path(options.output)
    if output.exists() or output.is_symlink():
        parser.error("output already exists; refusing overwrite")

    documents: dict[str, dict[str, object]] = {}
    bindings: dict[str, dict[str, object]] = {}
    for name, path in (
        ("reliable_observation", options.reliable_observation),
        ("reliable_codec", options.reliable_codec),
        ("best_effort_observation", options.best_effort_observation),
        ("best_effort_codec", options.best_effort_codec),
    ):
        documents[name], bindings[name] = _read_json(path)
    report = evaluate_transport_evidence(
        reliable_observation=documents["reliable_observation"],
        reliable_codec=documents["reliable_codec"],
        best_effort_observation=documents["best_effort_observation"],
        best_effort_codec=documents["best_effort_codec"],
        input_bindings=bindings,
    )
    _write_new_private(output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["strict_transport_gate_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover - console entry point
    main()
