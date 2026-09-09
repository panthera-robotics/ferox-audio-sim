#!/usr/bin/env python3
"""Run digest-bound Microsoft AECMOS external smoke cases.

This deliberately does not aggregate a challenge score: a handful of public
clips cannot represent the official test set or establish HATS qualification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import wave
from pathlib import Path


SCORE_PATTERN = re.compile(
    r"The AECMOS echo score is ([0-9.eE+-]+), and \(other\) degradation score is "
    r"([0-9.eE+-]+)\."
)
SCENARIOS = {
    "double_talk": "dt",
    "near_end_single_talk": "nst",
    "far_end_single_talk": "st",
}


def regular_file(path: str | Path, *, maximum_bytes: int) -> tuple[Path, bytes]:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"input must be a regular non-symlink file: {source}")
    payload = source.read_bytes()
    if not payload or len(payload) > maximum_bytes:
        raise ValueError(f"input size is invalid: {source}")
    return source, payload


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def wav_binding(path: str | Path) -> dict[str, object]:
    source, payload = regular_file(path, maximum_bytes=100_000_000)
    try:
        with wave.open(str(source), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
            compression = handle.getcomptype()
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"invalid WAV: {source}") from exc
    if channels != 1 or sample_width != 2 or sample_rate != 48_000 or compression != "NONE":
        raise ValueError(f"AECMOS smoke WAV must be 48 kHz mono PCM16: {source}")
    return {
        "path": str(source.resolve()),
        "sha256": sha256(payload),
        "size_bytes": len(payload),
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "pcm_format": "S16_LE",
        "duration_s": frame_count / sample_rate,
    }


def write_private_new(path: str | Path, document: dict[str, object]) -> None:
    output = Path(path)
    if output.exists() or output.is_symlink():
        raise ValueError(f"output already exists; refusing overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write((json.dumps(document, indent=2, sort_keys=True) + "\n").encode())
    except Exception:
        try:
            output.unlink()
        except OSError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aec-repo", required=True)
    parser.add_argument("--aecmos-script", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--candidate-reference", required=True)
    parser.add_argument(
        "--case", nargs=5, action="append", required=True,
        metavar=("SCENARIO", "METHOD", "LPB", "MIC", "ENH"),
    )
    parser.add_argument("--output", required=True)
    options = parser.parse_args()

    repo = Path(options.aec_repo).resolve()
    script, script_payload = regular_file(options.aecmos_script, maximum_bytes=2_000_000)
    model, model_payload = regular_file(options.model, maximum_bytes=100_000_000)
    repo_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", repo_sha):
        raise ValueError("AEC Challenge repository HEAD is invalid")
    if subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", "--", str(script)]
    ).returncode != 0:
        raise ValueError("official AECMOS script has local modifications")

    results: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for scenario, method, lpb, mic, enhanced in options.case:
        if scenario not in SCENARIOS:
            raise ValueError(f"unsupported scenario: {scenario}")
        identity = (scenario, method)
        if identity in seen:
            raise ValueError(f"duplicate scenario/method: {identity}")
        seen.add(identity)
        bindings = {
            "loopback": wav_binding(lpb),
            "microphone": wav_binding(mic),
            "enhanced": wav_binding(enhanced),
        }
        durations = {round(float(item["duration_s"]), 9) for item in bindings.values()}
        if len(durations) != 1:
            raise ValueError(f"AECMOS rated inputs have unequal lengths: {identity}")
        completed = subprocess.run([
            sys.executable, str(script),
            "--talk_type", SCENARIOS[scenario],
            "--model_path", str(model),
            "--lpb_path", str(Path(lpb).resolve()),
            "--mic_path", str(Path(mic).resolve()),
            "--enh_path", str(Path(enhanced).resolve()),
        ], check=True, capture_output=True, text=True)
        match = SCORE_PATTERN.fullmatch(completed.stdout.strip())
        if match is None:
            raise ValueError(f"unexpected AECMOS output: {completed.stdout!r}")
        results.append({
            "scenario": scenario,
            "method": method,
            "talk_type": SCENARIOS[scenario],
            "rated_duration_s": durations.pop(),
            "echo_mos": float(match.group(1)),
            "other_degradation_mos": float(match.group(2)),
            "inputs": bindings,
        })

    document: dict[str, object] = {
        "schema_version": 1,
        "evidence_class": "microsoft_aecmos_public_sample_smoke",
        "candidate_reference": options.candidate_reference,
        "official_tool": {
            "repository": "https://github.com/microsoft/AEC-Challenge",
            "repository_sha": repo_sha,
            "script_path": str(script),
            "script_sha256": sha256(script_payload),
            "model_path": str(model),
            "model_sha256": sha256(model_payload),
            "model_sample_rate_hz": 48_000,
        },
        "case_count": len(results),
        "results": results,
        "complete_official_benchmark": False,
        "representative_dataset_claimed": False,
        "hats_qualified": False,
        "production_ready": False,
        "boundary": (
            "External smoke on one public official clip per available scenario. "
            "Scores are retained per clip and are not averaged into an official "
            "challenge score, a representative quality claim, or HATS qualification."
        ),
    }
    write_private_new(options.output, document)
    print(json.dumps(document, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
