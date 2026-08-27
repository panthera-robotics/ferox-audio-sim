"""Evidence-bound Go2 speaker-command to microphone-observation latency."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import wave

import numpy as np
from scipy import signal

from .decode_capture import decode_frames, read_capture
from .profiles import get_profile


_BOOT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}$")


class AcousticLatencyError(ValueError):
    """Latency evidence is malformed, ambiguous, or not clock-bound."""


def _regular_bytes(path: str | Path, maximum_bytes: int) -> tuple[Path, bytes]:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise AcousticLatencyError(f"input must be a regular non-symlink file: {source}")
    payload = source.read_bytes()
    if not payload or len(payload) > maximum_bytes:
        raise AcousticLatencyError(f"input size is invalid: {source}")
    return source, payload


def _json(path: str | Path, maximum_bytes: int = 10_000_000) -> tuple[dict, dict]:
    source, payload = _regular_bytes(path, maximum_bytes)
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcousticLatencyError(f"invalid JSON: {source}") from exc
    if not isinstance(document, dict):
        raise AcousticLatencyError(f"JSON input must be one object: {source}")
    return document, {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _metadata(path: str | Path) -> tuple[dict, list[dict], dict, dict]:
    source, payload = _regular_bytes(path, 100_000_000)
    rows = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcousticLatencyError(
                f"invalid metadata JSON at line {line_number}") from exc
        if not isinstance(row, dict):
            raise AcousticLatencyError("metadata rows must be objects")
        rows.append(row)
    if len(rows) < 3 or rows[0].get("record_type") != "capture_start" \
            or rows[-1].get("record_type") != "capture_end":
        raise AcousticLatencyError("metadata must have capture_start, frames, capture_end")
    frames = rows[1:-1]
    if any(row.get("record_type") != "frame" for row in frames):
        raise AcousticLatencyError("metadata contains a non-frame interior row")
    callbacks = [row.get("callback_steady_ns") for row in frames]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in callbacks):
        raise AcousticLatencyError("frame callback steady timestamps are invalid")
    if any(right <= left for left, right in zip(callbacks, callbacks[1:])):
        raise AcousticLatencyError("frame callback steady timestamps are not increasing")
    if rows[-1].get("frame_count") != len(frames):
        raise AcousticLatencyError("metadata trailer frame_count mismatch")
    return rows[0], frames, rows[-1], {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "frame_count": len(frames),
    }


def _wav_mono_s16(path: str | Path) -> tuple[np.ndarray, int, dict]:
    source, payload = _regular_bytes(path, 500_000)
    try:
        with wave.open(str(source), "rb") as reader:
            if (reader.getcomptype(), reader.getnchannels(), reader.getsampwidth()) \
                    != ("NONE", 1, 2):
                raise AcousticLatencyError("probe WAV must be mono PCM S16_LE")
            rate = int(reader.getframerate())
            frames = int(reader.getnframes())
            if rate <= 0 or frames <= 0 or frames > rate * 2:
                raise AcousticLatencyError("probe WAV duration or sample rate is invalid")
            pcm = reader.readframes(frames)
    except (EOFError, wave.Error) as exc:
        raise AcousticLatencyError(f"malformed probe WAV: {exc}") from exc
    if len(pcm) != frames * 2:
        raise AcousticLatencyError("probe WAV payload is truncated")
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64), rate, {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def matched_filter(
    capture: np.ndarray,
    reference: np.ndarray,
    *,
    minimum_peak: float = 0.20,
    minimum_peak_ratio: float = 1.50,
) -> dict[str, object]:
    """Return normalized matched-filter localization with ambiguity rejection."""
    observed = np.asarray(capture, dtype=np.float64)
    expected = np.asarray(reference, dtype=np.float64)
    if observed.ndim != 1 or expected.ndim != 1 or expected.size < 64:
        raise AcousticLatencyError("capture/reference arrays must be one-dimensional")
    if observed.size < expected.size * 2:
        raise AcousticLatencyError("capture must be at least twice the reference length")
    if not np.all(np.isfinite(observed)) or not np.all(np.isfinite(expected)):
        raise AcousticLatencyError("capture/reference contains non-finite samples")
    expected = expected - np.mean(expected)
    reference_energy = float(np.dot(expected, expected))
    if reference_energy <= 1e-9:
        raise AcousticLatencyError("reference has no usable energy")

    numerator = signal.fftconvolve(observed, expected[::-1], mode="valid")
    count = expected.size
    cumulative = np.concatenate(([0.0], np.cumsum(observed, dtype=np.float64)))
    cumulative_sq = np.concatenate((
        [0.0], np.cumsum(observed * observed, dtype=np.float64)))
    sums = cumulative[count:] - cumulative[:-count]
    sums_sq = cumulative_sq[count:] - cumulative_sq[:-count]
    local_energy = np.maximum(sums_sq - sums * sums / count, 1e-12)
    normalized = np.abs(numerator) / np.sqrt(reference_energy * local_energy)
    peak_index = int(np.argmax(normalized))
    peak = float(normalized[peak_index])
    exclusion = max(1, count // 2)
    candidates = normalized.copy()
    candidates[max(0, peak_index - exclusion):min(
        candidates.size, peak_index + exclusion + 1)] = 0.0
    second = float(np.max(candidates)) if candidates.size else 0.0
    ratio = peak / max(second, 1e-12)
    return {
        "onset_sample": peak_index,
        "normalized_peak": peak,
        "second_peak": second,
        "peak_ratio": ratio,
        "reference_samples": int(count),
        "capture_samples": int(observed.size),
        "localization_passed": peak >= minimum_peak and ratio >= minimum_peak_ratio,
        "thresholds": {
            "normalized_peak_min": minimum_peak,
            "peak_ratio_min": minimum_peak_ratio,
        },
    }


def evaluate_acoustic_latency(
    *,
    capture_pcm: bytes,
    capture_sample_rate: int,
    frame_samples: int,
    frame_callbacks_steady_ns: list[int],
    reference: np.ndarray,
    reference_sample_rate: int,
    publish_steady_ns: int,
    maximum_latency_ms: float,
) -> dict[str, object]:
    if len(capture_pcm) % 2:
        raise AcousticLatencyError("decoded capture ends in a partial S16_LE sample")
    if capture_sample_rate <= 0 or frame_samples <= 0:
        raise AcousticLatencyError("capture format is invalid")
    if not math.isfinite(maximum_latency_ms) or not 20.0 <= maximum_latency_ms <= 2000.0:
        raise AcousticLatencyError("maximum latency must be finite and in [20, 2000] ms")
    capture = np.frombuffer(capture_pcm, dtype="<i2").astype(np.float64)
    expected = signal.resample_poly(
        reference, capture_sample_rate, reference_sample_rate)
    correlation = matched_filter(capture, expected)
    onset = int(correlation["onset_sample"])
    frame_index = onset // frame_samples
    offset = onset % frame_samples
    expected_frames = math.ceil(capture.size / frame_samples)
    if len(frame_callbacks_steady_ns) != expected_frames or frame_index >= len(
            frame_callbacks_steady_ns):
        raise AcousticLatencyError("decoded frames and callback metadata do not align")
    callback_ns = int(frame_callbacks_steady_ns[frame_index])
    latency_ms = (callback_ns - publish_steady_ns) / 1e6
    frame_duration_ms = 1000.0 * frame_samples / capture_sample_rate
    checks = {
        "matched_filter_unambiguous": bool(correlation["localization_passed"]),
        "mic_callback_after_speaker_block_publish": latency_ms >= 0.0,
        "command_to_mic_callback_latency_within_limit": (
            0.0 <= latency_ms <= maximum_latency_ms),
    }
    return {
        "correlation": correlation,
        "metrics": {
            "command_to_mic_callback_latency_ms": latency_ms,
            "matched_onset_sample": onset,
            "matched_onset_frame_index": frame_index,
            "matched_onset_offset_in_frame_ms": 1000.0 * offset / capture_sample_rate,
            "capture_frame_duration_ms": frame_duration_ms,
            "capture_sample_rate": capture_sample_rate,
        },
        "checks": checks,
        "measured": all(checks.values()),
    }


def build_certificate(
    *,
    frames_path: str | Path,
    metadata_path: str | Path,
    speaker_result_path: str | Path,
    probe_wav_path: str | Path,
    profile_name: str,
    maximum_latency_ms: float,
) -> dict[str, object]:
    speaker, speaker_binding = _json(speaker_result_path)
    header, metadata_frames, trailer, metadata_binding = _metadata(metadata_path)
    reference, reference_rate, wav_binding = _wav_mono_s16(probe_wav_path)
    frames_source, frames_payload = _regular_bytes(frames_path, 100_000_000)
    encoded_frames = read_capture(frames_source, max_frames=20_000)
    pcm, decoded_count, sample_rate = decode_frames(
        encoded_frames, profile_name=profile_name)
    profile = get_profile(profile_name)
    token = header.get("supervised_speaker_capture_token")
    speaker_metadata = speaker.get("probe_metadata")
    speaker_probe = speaker.get("speaker_probe")
    if not isinstance(speaker_metadata, dict) or not isinstance(speaker_probe, dict):
        raise AcousticLatencyError("speaker result is not a completed probe result")
    events = speaker_metadata.get("request_publish_events")
    if not isinstance(events, list) or not events:
        raise AcousticLatencyError("speaker result has no request publication events")
    block_events = [event for event in events if isinstance(event, dict)
                    and event.get("api_id") == 4003]
    if not block_events:
        raise AcousticLatencyError("speaker result has no API 4003 block publication")
    first_block = block_events[0]
    publish_ns = first_block.get("publish_steady_ns")
    if isinstance(publish_ns, bool) or not isinstance(publish_ns, int):
        raise AcousticLatencyError("speaker block steady timestamp is invalid")
    boot_id = header.get("host_boot_id")
    speaker_boot_id = speaker_metadata.get("host_boot_id")
    speaker_token = speaker_metadata.get("latency_capture_token")
    integrity = {
        "same_linux_boot": (
            isinstance(boot_id, str) and _BOOT_ID.fullmatch(boot_id) is not None
            and boot_id == speaker_boot_id),
        "same_nonempty_capture_token": (
            isinstance(token, str) and _TOKEN.fullmatch(token) is not None
            and token == speaker_token),
        "capture_declared_supervised_speaker_expected": (
            header.get("speaker_or_audiohub_expected") is True
            and trailer.get("speaker_or_audiohub_called") is True),
        "capture_spans_first_audio_block_publish": (
            isinstance(header.get("capture_start_steady_ns"), int)
            and isinstance(trailer.get("capture_end_steady_ns"), int)
            and header["capture_start_steady_ns"] <= publish_ns
            <= trailer["capture_end_steady_ns"]),
        "decoded_frames_match_metadata": decoded_count == len(metadata_frames),
        "speaker_wav_digest_matches": (
            speaker_probe.get("test_wav_sha256") == wav_binding["sha256"]),
        "speaker_api_reported_no_errors": speaker_probe.get("api_errors") == 0,
        "collector_created_no_publisher": header.get("publisher_created") is False,
    }
    if not all(integrity.values()):
        return {
            "schema_version": 1,
            "evidence_class": "go2_supervised_acoustic_latency",
            "integrity_checks": integrity,
            "failures": [name for name, passed in integrity.items() if not passed],
            "one_way_latency_measured": False,
            "absolute_latency_gate_passed": False,
            "production_ready": False,
            "speaker_enable_authorized": False,
        }
    measured = evaluate_acoustic_latency(
        capture_pcm=pcm,
        capture_sample_rate=sample_rate,
        frame_samples=profile.mic_frame_samples,
        frame_callbacks_steady_ns=[
            int(row["callback_steady_ns"]) for row in metadata_frames],
        reference=reference,
        reference_sample_rate=reference_rate,
        publish_steady_ns=publish_ns,
        maximum_latency_ms=maximum_latency_ms,
    )
    checks = {**integrity, **measured["checks"]}
    passed = all(checks.values())
    return {
        "schema_version": 1,
        "evidence_class": "go2_supervised_acoustic_latency",
        "inputs": {
            "frames": {
                "path": str(frames_source.resolve()),
                "sha256": hashlib.sha256(frames_payload).hexdigest(),
                "size_bytes": len(frames_payload),
            },
            "metadata": metadata_binding,
            "speaker_result": speaker_binding,
            "probe_wav": wav_binding,
            "profile": profile_name,
        },
        "thresholds": {"maximum_command_to_mic_callback_latency_ms": maximum_latency_ms},
        "integrity_checks": integrity,
        **measured,
        "failures": [name for name, value in checks.items() if not value],
        "one_way_latency_measured": passed,
        "absolute_latency_gate_passed": passed,
        "production_ready": False,
        "speaker_enable_authorized": False,
        "control_authorized": False,
        "boundary": (
            "This is a same-boot, same-monotonic-clock measurement from the first "
            "AudioHub 4003 publication to availability of the matched acoustic signal "
            "in a decoded /audiosender callback. It includes speaker, acoustic, mic, "
            "codec, DDS and callback delay; it is not ADC-only transport latency. One "
            "supervised trial cannot establish HATS/AEC quality or production readiness."
        ),
    }


def _write_new(path: str | Path, document: dict[str, object]) -> None:
    output = Path(path)
    if output.exists() or output.is_symlink():
        raise AcousticLatencyError(f"output already exists: {output}")
    if not output.parent.is_dir():
        raise AcousticLatencyError("output parent does not exist")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write((json.dumps(document, indent=2, sort_keys=True) + "\n").encode())


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--speaker-result", required=True)
    parser.add_argument("--probe-wav", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--maximum-latency-ms", type=float, default=250.0)
    parser.add_argument("--output", required=True)
    options = parser.parse_args(args)
    try:
        report = build_certificate(
            frames_path=options.frames,
            metadata_path=options.metadata,
            speaker_result_path=options.speaker_result,
            probe_wav_path=options.probe_wav,
            profile_name=options.profile,
            maximum_latency_ms=options.maximum_latency_ms,
        )
        _write_new(options.output, report)
    except AcousticLatencyError as exc:
        parser.error(str(exc))
    print(json.dumps(report, sort_keys=True))
    if not report.get("absolute_latency_gate_passed", False):
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
