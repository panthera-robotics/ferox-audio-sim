from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_g1_runtime_is_safe_by_default_and_does_not_claim_a_microphone():
    config = (ROOT / "src/ferox_audio_g1/config/g1_voice_bridge.yaml").read_text()
    node = (ROOT / "src/ferox_audio_g1/ferox_audio_g1/voice_bridge_node.py").read_text()
    assert "speaker_enabled: false" in config
    assert "create_publisher(\n            AudioChunk" not in node
    assert "mic_raw is intentionally absent" in node


def test_arm64_image_keeps_required_rosidl_and_test_gates():
    dockerfile = (ROOT / "docker/Dockerfile.g1").read_text()
    assert "ros-${ROS_DISTRO}-rosidl-generator-dds-idl" in dockerfile
    assert "python3 -m pytest -q /workspace/src/ferox_audio_g1/test" in dockerfile
    assert "ENV CYCLONEDDS_URI=file:///tmp/cyclonedds.xml" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "'set -eo pipefail'" in dockerfile
    assert "'set -euo pipefail'" not in dockerfile


def test_dds_entrypoint_rejects_unexpanded_templates():
    entrypoint = (ROOT / "docker/entrypoint-g1-dds.sh").read_text()
    assert 'if "${" in source:' in entrypoint
    assert "unexpanded placeholder remains" in entrypoint
