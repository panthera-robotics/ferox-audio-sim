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


def test_g1_deployment_requires_immutable_image_and_least_privilege():
    compose = (ROOT / "docker/docker-compose.g1.yml").read_text()
    assert compose.count("FEROX_AUDIO_G1_IMAGE:?") == 2
    assert compose.count("read_only: true") == 2
    assert compose.count("cap_drop: [ALL]") == 2
    assert compose.count('security_opt: ["no-new-privileges:true"]') == 2
    assert compose.count("pids_limit: 128") == 2
    assert "/tmp:rw,nosuid,nodev,noexec" in compose
    assert "privileged:" not in compose


def test_audio_gateway_has_only_reviewed_directional_interfaces():
    gateway = (ROOT / (
        "src/ferox_audio_g1/ferox_audio_g1/audio_domain_gateway.py")).read_text()
    assert "AudioChunk 42->0, diagnostics 0->42 only" in gateway
    for forbidden in ("cmd_vel", "motor_cmd", "unitree_api", "create_service"):
        assert forbidden not in gateway
