from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_go2_audio_is_fail_closed_and_evidence_pinned_by_default():
    config = (ROOT / "src/ferox_audio_go2/config/go2_audio_bridge.yaml").read_text()
    compose = (ROOT / "docker/docker-compose.go2.yml").read_text()
    launch = (ROOT / "src/ferox_audio_go2/launch/go2_audio_bridge.launch.py").read_text()
    assert "mic_enabled: false" in config
    assert "speaker_enabled: false" in config
    assert config.count('"disabled"') >= 4
    assert "GO2_AUDIO_EVIDENCE_SHA256" in compose
    assert "GO2_AUDIO_RUNTIME_FIRMWARE" in compose
    assert "GO2_AUDIO_PROFILE:-disabled" in compose
    assert "GO2_AUDIO_RUNTIME_FIRMWARE:-disabled" in compose
    assert "GO2_AUDIO_EVIDENCE_SHA256:-disabled" in compose
    assert "FEROX_DDS_INTERFACE:?" in compose
    for argument in (
            "mic_enabled", "speaker_enabled", "hardware_profile",
            "runtime_firmware", "evidence_path", "evidence_sha256"):
        assert f'DeclareLaunchArgument("{argument}"' in launch
    node = (ROOT / "src/ferox_audio_go2/ferox_audio_go2/bridge_node.py").read_text()
    assert "status.level = bytes([report.level])" in node
    assert 'f"{robot_id}/mic"' in node
    assert "ReliabilityPolicy.RELIABLE" in node
    assert "source_to_chunk_p99_ms" in node
    assert "source_to_chunk_max_ms" in node
    assert "Go2 audio protocol/topic override is not qualified" in node


def test_disabled_compose_uses_only_the_invalid_evidence_template():
    compose = (ROOT / "docker/docker-compose.go2.yml").read_text()
    assert (
        "GO2_AUDIO_EVIDENCE_PATH:-../src/ferox_audio_go2/evidence/"
        "go2_audio_evidence.template.json" in compose
    )
    template = (ROOT / (
        "src/ferox_audio_go2/evidence/go2_audio_evidence.template.json"
    )).read_text()
    assert "REPLACE_WITH" in template


def test_go2_deployment_is_immutable_and_least_privilege():
    compose = (ROOT / "docker/docker-compose.go2.yml").read_text()
    assert compose.count("FEROX_AUDIO_GO2_IMAGE:?") == 2
    assert compose.count("read_only: true") == 2
    assert compose.count("cap_drop: [ALL]") == 2
    assert compose.count('security_opt: ["no-new-privileges:true"]') == 2
    assert compose.count("pids_limit: 128") == 2
    assert "privileged:" not in compose
    assert "/dev/snd" not in compose


def test_domain_gateway_is_audio_only():
    gateway = (ROOT / (
        "src/ferox_audio_go2/ferox_audio_go2/domain_gateway.py")).read_text()
    assert "diagnostics robot->application" in gateway
    assert "mic robot->application=" in gateway
    assert "speaker application->robot=" in gateway
    for forbidden in ("cmd_vel", "motor_cmd", "sport/request", "create_service"):
        assert forbidden not in gateway
    assert "SingleThreadedExecutor" in gateway
    assert "MultiThreadedExecutor" not in gateway
    compose = (ROOT / "docker/docker-compose.go2.yml").read_text()
    assert "--mic-enabled" in compose
    assert "--speaker-enabled" in compose


def test_discovery_is_read_only_and_does_not_name_a_codec():
    source = (ROOT / (
        "src/ferox_audio_go2/ferox_audio_go2/discovery_probe.py")).read_text()
    assert "create_subscription" in source
    assert "create_publisher" not in source
    assert '"interpretation": "none"' in source
    assert 'if __name__ == "__main__"' in source
    assert "ReliabilityPolicy.RELIABLE" in source
    assert "create_publisher" not in (
        ROOT / "src/ferox_audio_go2/ferox_audio_go2/decode_capture.py").read_text()
    live_core = (ROOT / (
        "src/ferox_audio_go2/ferox_audio_go2/live_core_qualification.py"
    )).read_text()
    assert "create_subscription" in live_core
    assert "create_publisher" not in live_core
    decode = (ROOT / "src/ferox_audio_go2/ferox_audio_go2/decode_capture.py").read_text()
    assert "signal_metrics" in decode
    assert '"speech_claim_authorized": False' in decode
    assert "operator_audio_intelligible" in decode
    assert "create_client" not in live_core
    assert "unitree_api" not in live_core
    assert "speaker_out" not in live_core
    aec = (ROOT / "src/ferox_audio_go2/ferox_audio_go2/aec_unavailable.py").read_text()
    assert "canceller_present" in aec
    assert "missing_measurement" in aec
    assert "speaker_enable_authorized" in aec
    assert "engineering ERLE" in aec
    for forbidden in (
            "webrtc", "speex", "nlms", "PlayStream", "audiohub",
            "4001", "4003", "1001", "1003", "1006"):
        assert forbidden not in aec
    runbook = (ROOT / "docs/hats-1m-spoken-runbook.md").read_text()
    assert "Do not execute this capture now" in runbook
    assert "388b4e31942772ddb248d31576fe3191aa1f6126553a34882ea1f6e89273662e" in runbook
    assert "Do **not** unmute the robot speaker" in runbook
    assert "Do **not** claim TCLw" in runbook


def test_speaker_probe_is_bounded_one_shot_and_needs_human_confirmation():
    source = (ROOT / (
        "src/ferox_audio_go2/ferox_audio_go2/speaker_probe.py")).read_text()
    assert "--confirm-supervised-safe-volume" in source
    assert '"operator_heard_test_phrase": False' in source
    assert '"operator_confirmed_no_delayed_replay_10s": False' in source
    assert "completed_total != 1" in source
    assert "post_deadline = time.monotonic() + 10.0" in source
    assert '"status": "started_not_authorizing"' in source
    assert 'attempt["hardware_publish_started"] = True' in source


def test_arm64_image_builds_exact_unitree_commit_and_runs_gates():
    dockerfile = (ROOT / "docker/Dockerfile.go2").read_text()
    bridge_entrypoint = (ROOT / "docker/entrypoint-go2-bridge.sh").read_text()
    assert "ROS_BASE_IMAGE=ros:humble-ros-base@sha256:7bea3d9aa2483d3ca34c8e30d921b79273b0913bd7dc64bebf51d082b5d107e4" in dockerfile
    assert "ARG FEROX_MSGS_IMAGE" in dockerfile
    assert "FROM ${FEROX_MSGS_IMAGE} AS ferox_msgs_artifact" in dockerfile
    assert "FEROX_MSGS_TAG" not in dockerfile
    assert "UNITREE_ROS2_REF=668d1ec5a05d1c38d3306bdca7d59f2ba3581a88" in dockerfile
    assert "--packages-select unitree_go unitree_api" in dockerfile
    assert "python3 -m pytest -q /workspace/src/ferox_audio_go2/test" in dockerfile
    assert "docs/hats-1m-spoken-runbook.md /workspace/docs/" in dockerfile
    assert "ros_go2_audio_domain_gateway_smoke.py" in dockerfile
    assert "exec ros2 run ferox_audio_go2 go2_audio_domain_gateway" in dockerfile
    assert "> /entrypoint-go2-gateway.sh" in dockerfile
    assert "ENV ROS_LOG_DIR=/tmp/ros-logs" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "exec ros2 run ferox_audio_go2 go2_audio_bridge" in bridge_entrypoint
    assert "unsupported Go2 audio bridge argument" in bridge_entrypoint
    assert "exec ros2 launch" not in bridge_entrypoint
