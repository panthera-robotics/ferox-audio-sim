"""Capture and immediately reduce a supervised G1 read-aloud to signal evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

from .readaloud_analysis import ReadAloudEvidenceError, analyze_readaloud_pcm


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="plughw:CARD=II,DEV=0")
    parser.add_argument("--duration-s", type=int, default=20)
    parsed = parser.parse_args(args)
    if not 5 <= parsed.duration_s <= 120:
        parser.error("--duration-s must be in [5, 120]")
    if not parsed.device or any(character in parsed.device for character in "\n\r\x00"):
        parser.error("--device is invalid")

    with tempfile.TemporaryDirectory(prefix="kevin-readaloud-") as capture_dir:
        capture_path = Path(capture_dir) / "capture.raw"
        command = [
            "arecord", "-q", "-D", parsed.device,
            "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw",
            "--buffer-time=40000", "--period-time=10000",
            "-d", str(parsed.duration_s), str(capture_path),
        ]
        started_ns = time.monotonic_ns()
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=parsed.duration_s + 5,
            check=False,
        )
        wall_duration_s = (time.monotonic_ns() - started_ns) / 1e9
        pcm = capture_path.read_bytes() if capture_path.exists() else b""

    stderr = completed.stderr.strip()
    if completed.returncode != 0:
        raise RuntimeError(
            f"arecord failed with {completed.returncode}: {stderr or 'no stderr'}")
    if stderr:
        raise RuntimeError(f"arecord reported unexpected stderr: {stderr}")
    try:
        evidence = analyze_readaloud_pcm(
            pcm, expected_duration_s=parsed.duration_s)
    except ReadAloudEvidenceError as exc:
        raise RuntimeError(str(exc)) from exc
    evidence.update({
        "alsa_returncode": completed.returncode,
        "alsa_stderr": stderr,
        "wall_duration_s": round(wall_duration_s, 3),
        "raw_audio_retained": False,
    })
    if not evidence["speech_detected"] or not evidence["clipping_gate_passed"]:
        raise RuntimeError(
            "read-aloud acceptance failed: " + json.dumps(evidence, sort_keys=True))
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
