#!/usr/bin/env python3
"""Run the packaged native AEC3 binary through three synthetic scenarios."""
from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import tempfile
import wave
from pathlib import Path


RATE = 48_000
DURATION_S = 20


def write_wav(path: Path, samples: list[int]) -> None:
    payload = bytearray()
    for sample in samples:
        payload.extend(max(-32768, min(32767, sample)).to_bytes(
            2, "little", signed=True))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(payload)


def speech_like_near_end(count: int) -> list[int]:
    """Create deterministic, non-stationary voiced content for an APM smoke.

    White noise is intentionally not used as near-end speech: WebRTC's
    sub-band analysis/synthesis can preserve its level while decorrelating its
    samples.  The changing pitch, harmonics, syllabic envelope, and pauses here
    exercise near-end preservation without embedding a licensed speech clip.
    """
    result: list[int] = []
    phase = 0.0
    for index in range(count):
        time_s = index / RATE
        fundamental_hz = 123.0 + 17.0 * math.sin(2.0 * math.pi * 0.41 * time_s)
        phase += 2.0 * math.pi * fundamental_hz / RATE
        syllable = 0.12 + 0.88 * (
            0.5 + 0.5 * math.sin(2.0 * math.pi * 3.17 * time_s)) ** 2
        within_phrase = time_s % 3.7
        phrase = 0.0 if 3.25 <= within_phrase < 3.7 else 1.0
        voiced = (
            math.sin(phase)
            + 0.52 * math.sin(2.0 * phase + 0.2)
            + 0.31 * math.sin(3.0 * phase + 0.5)
            + 0.18 * math.sin(5.0 * phase + 0.8)
            + 0.12 * math.sin(8.0 * phase + 0.4)
        )
        result.append(round(4_500 * syllable * phrase * voiced))
    return result


def signals() -> tuple[list[int], list[int], list[int]]:
    random_source = random.Random(20260827)
    count = RATE * DURATION_S
    render: list[int] = []
    for _ in range(count):
        render.append(round(10_000 * random_source.uniform(-1.0, 1.0)))
    delay = round(0.064 * RATE)
    echo = []
    for index in range(count):
        direct = render[index - delay] if index >= delay else 0
        echo.append(round(0.5 * direct))
    near = speech_like_near_end(count)
    return render, echo, near


def run_scenario(
    root: Path,
    *,
    scenario: str,
    runtime: str,
    certificate: str,
    render: list[int],
    capture: list[int],
    clean_near: list[int] | None,
    convergence_skip_s: int = 8,
) -> dict[str, object]:
    scenario_root = root / scenario
    scenario_root.mkdir()
    render_path = scenario_root / "render.wav"
    capture_path = scenario_root / "capture.wav"
    output_path = scenario_root / "output.wav"
    report_path = scenario_root / "runtime.json"
    certificate_path = scenario_root / "certificate.json"
    write_wav(render_path, render)
    write_wav(capture_path, capture)
    subprocess.run([
        runtime,
        "--render-wav", str(render_path),
        "--capture-wav", str(capture_path),
        "--output-wav", str(output_path),
        "--report", str(report_path),
        "--stream-delay-ms", "0",
    ], check=True)
    command = [
        certificate,
        "--scenario", scenario,
        "--render-wav", str(render_path),
        "--capture-wav", str(capture_path),
        "--output-wav", str(output_path),
        "--runtime-report", str(report_path),
        "--convergence-skip-s", str(convergence_skip_s),
        "--output", str(certificate_path),
    ]
    if clean_near is not None:
        clean_path = scenario_root / "clean-near.wav"
        write_wav(clean_path, clean_near)
        command.extend(["--clean-near-end-wav", str(clean_path)])
    subprocess.run(command, check=True)
    document = json.loads(certificate_path.read_text())
    if document.get("offline_functional_gate_passed") is not True:
        raise RuntimeError(f"offline AEC3 gate failed for {scenario}")
    return {
        "scenario": scenario,
        "metrics": document["metrics"],
        "production_ready": document["production_ready"],
        "tclw_qualified": document["tclw_qualified"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--certificate", required=True)
    options = parser.parse_args()
    render, echo, near = signals()
    silence = [0] * len(render)
    double_talk_near = [
        0 if index < 8 * RATE else sample
        for index, sample in enumerate(near)
    ]
    with tempfile.TemporaryDirectory(prefix="ferox-aec3-smoke-") as directory:
        root = Path(directory)
        reports = [
            run_scenario(
                root, scenario="far_end_single_talk", runtime=options.runtime,
                certificate=options.certificate, render=render, capture=echo,
                clean_near=None),
            run_scenario(
                root, scenario="near_end_single_talk", runtime=options.runtime,
                certificate=options.certificate, render=silence, capture=near,
                clean_near=None),
            run_scenario(
                root, scenario="double_talk", runtime=options.runtime,
                certificate=options.certificate, render=render,
                capture=[left + right for left, right in zip(echo, double_talk_near)],
                clean_near=double_talk_near, convergence_skip_s=10),
        ]
    print(json.dumps({"native_aec3_smoke": reports}, sort_keys=True))


if __name__ == "__main__":
    main()
