#!/usr/bin/env python3
"""Exercise native runtime failure paths and no-overwrite guarantees."""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from run_native_aec3_smoke import write_wav


SENTINEL = b"ferox-preserve-existing-v1\n"


def run_rejected(command: list[str], expected_fragment: str) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        raise RuntimeError(f"negative command unexpectedly passed: {command}")
    if expected_fragment not in result.stderr:
        raise RuntimeError(
            f"negative command did not report {expected_fragment!r}: {result.stderr!r}")


def assert_absent(*paths: Path) -> None:
    unexpected = [str(path) for path in paths if path.exists() or path.is_symlink()]
    if unexpected:
        raise RuntimeError(f"rejected command left output artifacts: {unexpected}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aec3-runtime", required=True)
    parser.add_argument("--timing-runtime", required=True)
    options = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="ferox-native-negative-") as directory:
        root = Path(directory)
        render = root / "render.wav"
        capture = root / "capture.wav"
        short_capture = root / "capture-short.wav"
        malformed = root / "malformed.wav"
        write_wav(render, [0] * 960)
        write_wav(capture, [0] * 960)
        write_wav(short_capture, [0] * 480)
        malformed.write_bytes(b"not-a-wave")

        def aec_command(output: Path, report: Path, *, source: Path = capture) -> list[str]:
            return [
                options.aec3_runtime,
                "--render-wav", str(render),
                "--capture-wav", str(source),
                "--output-wav", str(output),
                "--report", str(report),
            ]

        existing_output = root / "existing-output.wav"
        existing_output.write_bytes(SENTINEL)
        report = root / "existing-output-report.json"
        run_rejected(aec_command(existing_output, report), "cannot create output without overwrite")
        if existing_output.read_bytes() != SENTINEL:
            raise RuntimeError("AEC3 runtime modified an existing output")
        assert_absent(report)

        output = root / "preexisting-report-output.wav"
        existing_report = root / "existing-report.json"
        existing_report.write_bytes(SENTINEL)
        run_rejected(aec_command(output, existing_report), "cannot create output without overwrite")
        if existing_report.read_bytes() != SENTINEL:
            raise RuntimeError("AEC3 runtime modified an existing report")
        assert_absent(output)

        symlink_target = root / "symlink-target.wav"
        symlink_target.write_bytes(SENTINEL)
        symlink_output = root / "symlink-output.wav"
        symlink_output.symlink_to(symlink_target)
        symlink_report = root / "symlink-report.json"
        run_rejected(aec_command(symlink_output, symlink_report), "symlink")
        if symlink_target.read_bytes() != SENTINEL or not symlink_output.is_symlink():
            raise RuntimeError("AEC3 runtime changed a symlink target or link")
        assert_absent(symlink_report)

        malformed_output = root / "malformed-output.wav"
        malformed_report = root / "malformed-report.json"
        run_rejected(
            aec_command(malformed_output, malformed_report, source=malformed),
            "RIFF/WAVE",
        )
        assert_absent(malformed_output, malformed_report)

        unequal_output = root / "unequal-output.wav"
        unequal_report = root / "unequal-report.json"
        run_rejected(
            aec_command(unequal_output, unequal_report, source=short_capture),
            "lengths differ",
        )
        assert_absent(unequal_output, unequal_report)

        frames = root / "timing-frames.jsonl"
        existing_metadata = root / "timing-metadata.jsonl"
        existing_metadata.write_bytes(SENTINEL)
        run_rejected([
            options.timing_runtime,
            "--duration-s", "5",
            "--qos-reliability", "reliable",
            "--frames-output", str(frames),
            "--metadata-output", str(existing_metadata),
        ], "cannot create output without overwrite")
        if existing_metadata.read_bytes() != SENTINEL:
            raise RuntimeError("timing runtime modified existing metadata")
        assert_absent(frames)

        invalid_frames = root / "invalid-frames.jsonl"
        invalid_metadata = root / "invalid-metadata.jsonl"
        run_rejected([
            options.timing_runtime,
            "--duration-s", "4.99",
            "--qos-reliability", "best_effort",
            "--frames-output", str(invalid_frames),
            "--metadata-output", str(invalid_metadata),
        ], "[5, 120]")
        assert_absent(invalid_frames, invalid_metadata)

    print("native failure-boundary smoke passed")


if __name__ == "__main__":
    main()
