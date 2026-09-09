"""Stdlib PCM integrity metrics. Never claims human speech.

Peak, RMS, clipping, and crest factor are physical. An operator must still
listen before operator_audio_intelligible may become true. This module
refuses to set that field.
"""
from __future__ import annotations

import array
import math
import sys
from collections.abc import Mapping


class SignalMetricsError(ValueError):
    """PCM is not a qualified 16-bit little-endian mono stream."""


def analyze_pcm_s16le(pcm: bytes, *, sample_rate: int) -> dict[str, object]:
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate < 1:
        raise SignalMetricsError("sample_rate must be a positive integer")
    if len(pcm) % 2 != 0:
        raise SignalMetricsError("PCM length must be even")
    samples = array.array("h")
    samples.frombytes(pcm)
    if samples.itemsize != 2:
        raise SignalMetricsError("host int16 itemsize is not 2 bytes")
    if sys.byteorder != "little":
        samples.byteswap()
    count = len(samples)
    if count == 0:
        raise SignalMetricsError("PCM is empty")
    peak = 0
    total = 0.0
    clipped = 0
    nonzero = 0
    dc = 0.0
    for value in samples:
        abs_value = abs(int(value))
        if abs_value > peak:
            peak = abs_value
        total += float(value) * float(value)
        dc += float(value)
        if abs_value >= 32767:
            clipped += 1
        if value != 0:
            nonzero += 1
    rms = math.sqrt(total / count)
    duration_s = count / float(sample_rate)
    peak_dbfs = 20.0 * math.log10(peak / 32767.0) if peak > 0 else float("-inf")
    rms_dbfs = 20.0 * math.log10(rms / 32767.0) if rms > 0 else float("-inf")
    return {
        "sample_rate": sample_rate,
        "channels": 1,
        "sample_width_bytes": 2,
        "sample_count": count,
        "duration_s": duration_s,
        "peak_abs_s16": peak,
        "rms_s16": rms,
        "dc_s16": dc / count,
        "peak_dbfs": peak_dbfs,
        "rms_dbfs": rms_dbfs,
        "crest_factor": (peak / rms) if rms > 0 else None,
        "clipped_samples": clipped,
        "clipping_fraction": clipped / count,
        "nonzero_sample_fraction": nonzero / count,
        "speech_claim_authorized": False,
        "operator_audio_intelligible": False,
        "interpretation": "physical_pcm_only",
    }


def assert_no_speech_claim(metrics: Mapping[str, object]) -> None:
    if metrics.get("speech_claim_authorized") is not False:
        raise SignalMetricsError("speech_claim_authorized must remain false")
    if metrics.get("operator_audio_intelligible") is not False:
        raise SignalMetricsError("operator_audio_intelligible cannot be set by signal metrics")
    if metrics.get("interpretation") != "physical_pcm_only":
        raise SignalMetricsError("interpretation must remain physical_pcm_only")


def analyze_wav(path: str) -> dict[str, object]:
    import hashlib
    import wave
    from pathlib import Path

    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise SignalMetricsError("wav must be a regular non-symlink file")
    with wave.open(str(source), "rb") as reader:
        if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
            raise SignalMetricsError("wav must be mono signed 16-bit")
        sample_rate = int(reader.getframerate())
        pcm = reader.readframes(reader.getnframes())
    metrics = analyze_pcm_s16le(pcm, sample_rate=sample_rate)
    metrics["wav_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    metrics["wav_path"] = str(source)
    assert_no_speech_claim(metrics)
    return metrics


def main(args=None) -> None:
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True)
    parser.add_argument("--output", required=True)
    options = parser.parse_args(args)
    metrics = analyze_wav(options.wav)
    output = Path(options.output)
    if output.exists() or output.is_symlink():
        raise SignalMetricsError(f"output already exists: {output}")
    output.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    print(json.dumps(metrics, sort_keys=True))


if __name__ == "__main__":
    main()
