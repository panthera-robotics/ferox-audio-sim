from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_g1_runtime_is_safe_by_default_and_does_not_claim_a_microphone():
    config = (ROOT / "src/ferox_audio_g1/config/g1_voice_bridge.yaml").read_text()
    node = (ROOT / "src/ferox_audio_g1/ferox_audio_g1/voice_bridge_node.py").read_text()
    assert "speaker_enabled: false" in config
    assert "create_publisher(\n            AudioChunk" not in node
    assert "mic_raw is intentionally absent" in node


def test_g1_runtime_waits_for_native_voice_endpoints_and_caps_volume():
    config = (ROOT / "src/ferox_audio_g1/config/g1_voice_bridge.yaml").read_text()
    node = (ROOT / "src/ferox_audio_g1/ferox_audio_g1/voice_bridge_node.py").read_text()
    assert "endpoint_discovery_timeout_s: 5.0" in config
    assert "max_enabled_volume: 25" in config
    assert "get_subscription_count() == 1" in node
    assert 'count_publishers("/api/voice/response") == 1' in node
    assert "speaker output requires query_volume_on_start=true" in node
    assert "reported volume exceeds supervised playback ceiling" in node


def test_supervised_probe_requires_both_unique_voice_endpoints():
    probe = (ROOT / (
        "src/ferox_audio_g1/ferox_audio_g1/speaker_latency_probe.py")).read_text()
    assert "request_subscriptions == 1 and response_publishers == 1" in probe
    assert "request_subscriptions=" in probe
    assert "response_publishers=" in probe


def test_g1_container_propagates_voice_node_failure():
    dockerfile = (ROOT / "docker/Dockerfile.g1").read_text()
    entrypoint = next(
        line for line in dockerfile.splitlines()
        if "exec ros2 run ferox_audio_g1 g1_voice_bridge" in line)
    assert "--ros-args -r __ns:=/ferox/${ROBOT_ID:-g1_01}" in entrypoint
    assert "--params-file /workspace/install/share/ferox_audio_g1/config/" in entrypoint
    assert '"$@"' in entrypoint
    assert "exec ros2 launch ferox_audio_g1" not in dockerfile


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
