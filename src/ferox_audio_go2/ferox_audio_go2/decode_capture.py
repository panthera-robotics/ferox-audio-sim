"""Decode a read-only Go2 audio capture under one explicit candidate profile."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import wave

from .mic_bridge import MicIngressError, OpusDecoder, UlawDecoder
from .profiles import get_profile


class CaptureDecodeError(ValueError):
    """The captured frames do not satisfy the selected profile."""


def read_capture(path: str | Path, *, max_frames: int = 10_000):
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise CaptureDecodeError("capture must be a regular non-symlink file")
    frames = []
    previous_time_frame = None
    with source.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if number > max_frames:
                raise CaptureDecodeError("capture exceeds the maximum frame count")
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CaptureDecodeError(f"capture line {number} is invalid JSON") from exc
            if not isinstance(item, dict) or set(item) != {
                    "receive_steady_s", "time_frame", "payload_b64"}:
                raise CaptureDecodeError(f"capture line {number} schema mismatch")
            time_frame_value = item["time_frame"]
            receive_value = item["receive_steady_s"]
            if isinstance(time_frame_value, bool) or not isinstance(time_frame_value, int):
                raise CaptureDecodeError(
                    f"capture line {number} time_frame must be an integer")
            if isinstance(receive_value, bool) or not isinstance(
                    receive_value, (int, float)):
                raise CaptureDecodeError(
                    f"capture line {number} receive time must be numeric")
            try:
                time_frame = int(time_frame_value)
                receive = float(receive_value)
                payload = base64.b64decode(item["payload_b64"], validate=True)
            except (TypeError, ValueError) as exc:
                raise CaptureDecodeError(f"capture line {number} is malformed") from exc
            if time_frame < 0 or not math.isfinite(receive) or receive < 0:
                raise CaptureDecodeError(f"capture line {number} has an invalid timestamp")
            if previous_time_frame is not None and time_frame <= previous_time_frame:
                raise CaptureDecodeError("capture source timestamps are not monotonic")
            previous_time_frame = time_frame
            frames.append(payload)
    if not frames:
        raise CaptureDecodeError("capture contains no frames")
    return frames


def decode_frames(frames, *, profile_name: str) -> tuple[bytes, int, int]:
    profile = get_profile(profile_name)
    decoder = (
        OpusDecoder(profile.mic_sample_rate, 1, profile.mic_frame_samples)
        if profile.mic_codec == "opus" else UlawDecoder()
    )
    output = bytearray()
    count = 0
    try:
        for index, payload in enumerate(frames):
            if len(payload) != profile.mic_frame_bytes:
                raise CaptureDecodeError(
                    f"frame {index} is {len(payload)} bytes; profile requires "
                    f"{profile.mic_frame_bytes}")
            try:
                pcm = decoder.decode(payload)
            except MicIngressError as exc:
                raise CaptureDecodeError(f"frame {index} decode failed: {exc}") from exc
            expected = profile.mic_frame_samples * 2
            if len(pcm) != expected:
                raise CaptureDecodeError(
                    f"frame {index} decoded to {len(pcm)} bytes; expected {expected}")
            output.extend(pcm)
            count += 1
    finally:
        decoder.close()
    return bytes(output), count, profile.mic_sample_rate


def wav_bytes(pcm: bytes, *, sample_rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(int(sample_rate))
        writer.writeframes(pcm)
    return output.getvalue()


def write_new(path: str | Path, data: bytes) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise CaptureDecodeError(f"output already exists: {output}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except Exception:
        try:
            output.unlink()
        except OSError:
            pass
        raise


def write_decode_bundle(
    wav_path: str | Path,
    probe_path: str | Path,
    rendered: bytes,
    probe: dict,
) -> None:
    """Create the WAV and probe together without leaving a partial result."""
    wav_output = Path(wav_path)
    probe_output = Path(probe_path)
    if str(wav_output.absolute()) == str(probe_output.absolute()):
        raise CaptureDecodeError("WAV and probe outputs must be distinct paths")
    existing = [
        str(path) for path in (wav_output, probe_output)
        if path.exists() or path.is_symlink()
    ]
    if existing:
        raise CaptureDecodeError(
            "output already exists; refusing partial decode: " + ", ".join(existing))
    created: list[Path] = []
    try:
        write_new(wav_output, rendered)
        created.append(wav_output)
        write_new(
            probe_output,
            (json.dumps(probe, indent=2, sort_keys=True) + "\n").encode(),
        )
        created.append(probe_output)
    except Exception:
        for output in created:
            try:
                output.unlink()
            except OSError:
                pass
        raise


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--wav-output", required=True)
    parser.add_argument("--probe-output", required=True)
    options = parser.parse_args(args)
    frames = read_capture(options.capture)
    capture_sha256 = hashlib.sha256(Path(options.capture).read_bytes()).hexdigest()
    pcm, count, sample_rate = decode_frames(frames, profile_name=options.profile)
    rendered = wav_bytes(pcm, sample_rate=sample_rate)
    probe = {
        "decoder": get_profile(options.profile).mic_codec,
        "decoded_frames": count,
        "decode_errors": 0,
        "decoded_sample_rate": sample_rate,
        "decoded_channels": 1,
        "operator_audio_intelligible": False,
        "operator_id": "REPLACE_AFTER_LISTENING",
        "capture_sha256": capture_sha256,
        "recording_sha256": hashlib.sha256(rendered).hexdigest(),
    }
    write_decode_bundle(options.wav_output, options.probe_output, rendered, probe)
    print(json.dumps(probe, sort_keys=True))


if __name__ == "__main__":
    main()
