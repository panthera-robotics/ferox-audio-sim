import hashlib
from pathlib import Path

import pytest

from ferox_audio_go2.discovery_probe import (
    DiscoveryProbeError,
    ObservedFrame,
    summarize_frames,
    write_capture_bundle,
    write_frame_capture,
    write_observation,
)


def frames(count=600):
    return [ObservedFrame(
        receive_steady_s=index * 0.02,
        time_frame=index + 1,
        payload=bytes([index % 256]) * 160,
    ) for index in range(count)]


def test_probe_reports_wire_shape_without_claiming_a_codec():
    result = summarize_frames(frames(), duration_s=12.0)
    assert result["frame_count"] == 600
    assert result["payload_bytes_mode"] == 160
    assert result["interval_p50_ms"] == pytest.approx(20.0)
    assert result["time_frame_monotonic"] is True
    assert result["interpretation"] == "none"
    assert len(result["framed_payload_sha256"]) == 64
    assert len(result["capture_sha256"]) == 64


def test_probe_rejects_absence_and_does_not_overwrite_evidence(tmp_path):
    with pytest.raises(DiscoveryProbeError, match="no Go2 audio"):
        summarize_frames([], duration_s=10.0)
    output = tmp_path / "observation.json"
    write_observation(output, summarize_frames(frames(2), duration_s=0.04))
    with pytest.raises(DiscoveryProbeError, match="overwrite"):
        write_observation(output, {})


def test_probe_preserves_framed_capture_privately(tmp_path):
    output = tmp_path / "frames.jsonl"
    write_frame_capture(output, frames(2))
    assert output.stat().st_mode & 0o777 == 0o600
    lines = output.read_text().splitlines()
    assert len(lines) == 2
    assert '"payload_b64"' in lines[0]
    observation = summarize_frames(frames(2), duration_s=0.04)
    assert hashlib.sha256(output.read_bytes()).hexdigest() == observation["capture_sha256"]
    with pytest.raises(DiscoveryProbeError, match="overwrite"):
        write_frame_capture(output, frames(1))


def test_bundle_preflight_never_leaves_a_partial_capture(tmp_path):
    observation = tmp_path / "observation.json"
    capture = tmp_path / "frames.jsonl"
    capture.write_text("owned-by-operator\n")
    with pytest.raises(DiscoveryProbeError, match="partial capture"):
        write_capture_bundle(
            observation, capture,
            summarize_frames(frames(2), duration_s=0.04), frames(2))
    assert not observation.exists()
    assert capture.read_text() == "owned-by-operator\n"


def test_bundle_requires_distinct_outputs(tmp_path):
    output = tmp_path / "same"
    with pytest.raises(DiscoveryProbeError, match="distinct"):
        write_capture_bundle(
            output, output,
            summarize_frames(frames(2), duration_s=0.04), frames(2))
    assert not output.exists()
