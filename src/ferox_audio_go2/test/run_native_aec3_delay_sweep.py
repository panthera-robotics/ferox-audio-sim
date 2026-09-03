#!/usr/bin/env python3
"""Characterize native AEC3 across controlled echo and stream delays."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import subprocess
import tempfile
import wave
from pathlib import Path

from ferox_audio_go2.aec3_offline_certificate import _energy, _load_wav


RATE = 48_000


def write_private_new(path: Path, document: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"output already exists; refusing overwrite: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write((json.dumps(document, indent=2, sort_keys=True) + "\n").encode())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def write_wav(path: Path, samples: list[int]) -> None:
    payload = bytearray()
    for sample in samples:
        payload.extend(int(sample).to_bytes(2, "little", signed=True))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--duration-s", type=int, default=20)
    parser.add_argument("--skip-s", type=int, default=8)
    parser.add_argument("--output", required=True)
    options = parser.parse_args()
    runtime = Path(options.runtime)
    if not runtime.is_file() or runtime.is_symlink():
        raise ValueError("runtime must be a regular non-symlink file")
    if not 10 <= options.duration_s <= 120:
        raise ValueError("--duration-s must be in [10, 120]")
    if not 0 <= options.skip_s < options.duration_s:
        raise ValueError("--skip-s must be in [0, duration-s)")
    count = RATE * options.duration_s
    source = random.Random(20260827)
    render = [round(10_000 * source.uniform(-1.0, 1.0)) for _ in range(count)]
    results = []
    with tempfile.TemporaryDirectory(prefix="ferox-aec3-sweep-") as directory:
        root = Path(directory)
        render_path = root / "render.wav"
        write_wav(render_path, render)
        for echo_delay_ms in (0, 4, 8, 16, 32, 64, 96):
            delay_samples = round(echo_delay_ms * RATE / 1000)
            capture = [
                round(0.5 * render[index - delay_samples])
                if index >= delay_samples else 0
                for index in range(count)
            ]
            capture_path = root / f"capture-{echo_delay_ms}.wav"
            write_wav(capture_path, capture)
            for stream_delay_ms in sorted({0, echo_delay_ms}):
                output_path = root / f"output-{echo_delay_ms}-{stream_delay_ms}.wav"
                report_path = root / f"report-{echo_delay_ms}-{stream_delay_ms}.json"
                subprocess.run([
                    options.runtime,
                    "--render-wav", str(render_path),
                    "--capture-wav", str(capture_path),
                    "--output-wav", str(output_path),
                    "--report", str(report_path),
                    "--stream-delay-ms", str(stream_delay_ms),
                ], check=True, stdout=subprocess.DEVNULL)
                capture_samples, _ = _load_wav(capture_path)
                output_samples, _ = _load_wav(output_path)
                start = options.skip_s * RATE
                erle = 10.0 * math.log10(
                    max(_energy(capture_samples, start), 1e-12) /
                    max(_energy(output_samples, start), 1e-12)
                )
                report = json.loads(report_path.read_text())
                results.append({
                    "echo_delay_ms": echo_delay_ms,
                    "stream_delay_ms": stream_delay_ms,
                    "engineering_erle_db": erle,
                    "reported_delay_ms": report["delay_ms"],
                    "reported_erle_db": report["echo_return_loss_enhancement_db"],
                    "realtime_factor": report["realtime_factor"],
                })
    document: dict[str, object] = {
        "schema_version": 1,
        "evidence_class": "webrtc_aec3_synthetic_delay_sweep",
        "runtime": {
            "path": str(runtime.resolve()),
            "sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        },
        "duration_s": options.duration_s,
        "convergence_skip_s": options.skip_s,
        "results": results,
        "engineering_only": True,
        "production_ready": False,
        "hats_qualified": False,
        "boundary": (
            "Deterministic synthetic electrical echo sweep only; this is not a "
            "real acoustic path, HATS result, TCLw measurement, or production gate."
        ),
    }
    write_private_new(Path(options.output), document)
    print(json.dumps(document, sort_keys=True))


if __name__ == "__main__":
    main()
