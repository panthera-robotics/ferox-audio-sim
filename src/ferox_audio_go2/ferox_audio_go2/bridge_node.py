"""Evidence-gated Go2 microphone and speaker ROS adapter."""
from __future__ import annotations

import secrets
import time

from .audiohub_transaction import AudioHubTransaction
from .health import COUNTERS, audio_health_report
from .mic_bridge import Go2MicBridgeCore, MicIngressError, chunk_to_message
from .profiles import ProfileEvidenceError, get_profile, load_profile_evidence
from .speaker_protocol import SpeakerProtocolError, SpeakerStreamAssembler


def main(args=None) -> None:
    try:
        import rclpy
        from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
        from ferox_msgs.msg import AudioChunk
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
        from unitree_api.msg import Request, Response
        from unitree_go.msg import AudioData
    except ImportError as exc:  # pragma: no cover - ROS image only
        raise RuntimeError("Go2 audio bridge requires ROS Humble interfaces") from exc

    rclpy.init(args=args)
    node = Node("go2_audio_bridge")
    for name, default in (
        ("mic_enabled", False),
        ("speaker_enabled", False),
        ("robot_id", "go2_02"),
        ("hardware_profile", ""),
        ("runtime_firmware", ""),
        ("evidence_path", ""),
        ("evidence_sha256", ""),
        ("evidence_max_age_days", 30.0),
        ("source_topic", "/audiosender"),
        ("mic_topic", "audio/mic_raw"),
        ("speaker_topic", "audio/speaker_out"),
        ("diagnostics_topic", "audio/diagnostics"),
        ("audiohub_request_topic", "/api/audiohub/request"),
        ("audiohub_response_topic", "/api/audiohub/response"),
        ("source_stale_s", 0.25),
        ("max_receive_gap_s", 0.10),
        ("audiohub_timeout_s", 2.0),
        ("max_utterance_s", 30.0),
    ):
        node.declare_parameter(name, default)
    g = lambda name: node.get_parameter(name).value
    mic_enabled = bool(g("mic_enabled"))
    speaker_enabled = bool(g("speaker_enabled"))
    robot_id = str(g("robot_id"))
    profile_name = str(g("hardware_profile"))
    runtime_firmware = str(g("runtime_firmware"))
    expected_sha = str(g("evidence_sha256"))
    source_stale_s = float(g("source_stale_s"))
    latched_fault: list[str] = []

    exact_topics = {
        "source_topic": "/audiosender",
        "mic_topic": "audio/mic_raw",
        "speaker_topic": "audio/speaker_out",
        "diagnostics_topic": "audio/diagnostics",
        "audiohub_request_topic": "/api/audiohub/request",
        "audiohub_response_topic": "/api/audiohub/response",
    }
    mismatched_topics = {
        name: str(g(name)) for name, expected in exact_topics.items()
        if str(g(name)) != expected
    }
    if mismatched_topics:
        node.destroy_node()
        rclpy.shutdown()
        raise RuntimeError(
            f"Go2 audio protocol/topic override is not qualified: {mismatched_topics}")

    namespace_parts = [item for item in node.get_namespace().split("/") if item]
    if len(namespace_parts) < 2 or namespace_parts[-2:] != ["ferox", robot_id]:
        node.destroy_node()
        rclpy.shutdown()
        raise RuntimeError("Go2 audio bridge must run in /ferox/<robot_id>")
    if not (mic_enabled or speaker_enabled):
        node.get_logger().warning(
            "Go2 microphone and speaker disabled by default; no hardware I/O will occur")
    profile = None
    evidence = None
    try:
        if mic_enabled or speaker_enabled:
            profile = get_profile(profile_name)
            evidence = load_profile_evidence(
                str(g("evidence_path")),
                expected_sha256=expected_sha,
                robot_id=robot_id,
                profile=profile,
                runtime_firmware=runtime_firmware,
                require_speaker=speaker_enabled,
                max_age_days=float(g("evidence_max_age_days")),
            )
    except ProfileEvidenceError as exc:
        node.destroy_node()
        rclpy.shutdown()
        raise RuntimeError(f"Go2 audio bridge refused hardware profile: {exc}") from exc

    mic_core = None
    if mic_enabled:
        assert profile is not None
        mic_core = Go2MicBridgeCore(
            profile, max_receive_gap_s=float(g("max_receive_gap_s")))
    speaker = None
    transaction = None
    if speaker_enabled:
        assert profile is not None
        assert profile.speaker_sample_rate is not None
        speaker = SpeakerStreamAssembler(
            sample_rate=profile.speaker_sample_rate,
            max_utterance_s=float(g("max_utterance_s")),
        )
        transaction = AudioHubTransaction(
            timeout_s=float(g("audiohub_timeout_s")),
            identity_seed=secrets.randbelow(2**31),
        )

    mic_pub = (
        node.create_publisher(AudioChunk, str(g("mic_topic")), qos_profile_sensor_data)
        if mic_enabled else None)
    diagnostics_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
    diagnostic_pub = node.create_publisher(
        DiagnosticArray, str(g("diagnostics_topic")), diagnostics_qos)
    request_pub = (
        node.create_publisher(Request, str(g("audiohub_request_topic")), diagnostics_qos)
        if speaker_enabled else None)
    last_source_receive: list[float | None] = [None]
    speaker_rejections = [0]

    def latch(reason: str) -> None:
        if not latched_fault:
            latched_fault.append(str(reason)[:160])
            node.get_logger().fatal(
                f"Go2 audio bridge latched fail-closed: {latched_fault[0]}")

    def on_source(message) -> None:
        if mic_core is None or latched_fault:
            return
        receive_steady = time.monotonic()
        receive_now = node.get_clock().now()
        try:
            chunks = mic_core.ingest(
                bytes(message.data),
                time_frame=int(message.time_frame),
                receive_steady_s=receive_steady,
                receive_time_ns=receive_now.nanoseconds,
            )
            last_source_receive[0] = receive_steady
            for chunk in chunks:
                assert mic_pub is not None
                mic_pub.publish(chunk_to_message(
                    AudioChunk, chunk, receive_now.to_msg(), f"{robot_id}/mic"))
        except MicIngressError as exc:
            node.get_logger().error(f"Go2 microphone frame rejected: {exc}")

    def on_speaker(message) -> None:
        if speaker is None or transaction is None or latched_fault:
            return
        try:
            plan = speaker.accept(message)
            if plan is not None:
                transaction.submit(plan)
        except (SpeakerProtocolError, RuntimeError) as exc:
            speaker_rejections[0] += 1
            latch(str(exc))

    def on_response(message) -> None:
        if transaction is None:
            return
        transaction.acknowledge(
            identity=int(message.header.identity.id),
            api_id=int(message.header.identity.api_id),
            status=int(message.header.status.code),
        )
        if transaction.latched_fault:
            latch(transaction.latched_fault)

    def tick_audiohub() -> None:
        if transaction is None or latched_fault:
            return
        now = time.monotonic()
        transaction.check_timeout(now)
        if transaction.latched_fault:
            latch(transaction.latched_fault)
            return
        pending = transaction.dispatch_next(now)
        if pending is None:
            return
        request = Request()
        request.header.identity.id = pending.identity
        request.header.identity.api_id = pending.api_id
        request.parameter = pending.parameter
        request.binary = []
        assert request_pub is not None
        request_pub.publish(request)

    def publish_diagnostic() -> None:
        now = time.monotonic()
        age_ms = (
            -1.0 if last_source_receive[0] is None
            else min(600_000.0, max(0.0, (now - last_source_receive[0]) * 1000.0))
        )
        mic_live = bool(
            mic_enabled and last_source_receive[0] is not None
            and now - last_source_receive[0] <= source_stale_s)
        counters = {
            "source_frames_total": mic_core.accepted_source_frames if mic_core else 0,
            "source_frames_rejected_total": mic_core.rejected_source_frames if mic_core else 0,
            "mic_chunks_total": mic_core.output_chunks if mic_core else 0,
            "mic_discontinuities_total": mic_core.discontinuities if mic_core else 0,
            "speaker_chunks_rejected_total": speaker_rejections[0],
            "speaker_uploads_completed_total": transaction.completed_total if transaction else 0,
            "audiohub_responses_ok_total": transaction.responses_ok_total if transaction else 0,
        }
        assert set(counters) == set(COUNTERS)
        report = audio_health_report(
            mic_enabled=mic_enabled,
            speaker_enabled=speaker_enabled,
            profile_evidence_valid=evidence is not None,
            speaker_evidence_valid=bool(evidence and evidence.speaker_confirmed),
            mic_stream_live=mic_live,
            audiohub_busy=transaction.busy if transaction else False,
            hardware_profile=profile_name,
            runtime_firmware=runtime_firmware,
            evidence_sha256=expected_sha,
            last_fault=latched_fault[0] if latched_fault else None,
            last_source_age_ms=age_ms,
            counters=counters,
        )
        status = DiagnosticStatus()
        status.level = bytes([report.level])
        status.name = f"ferox/{robot_id}/audio"
        status.message = report.message
        status.hardware_id = robot_id
        status.values = [KeyValue(key=key, value=value)
                         for key, value in report.values]
        output = DiagnosticArray()
        output.header.stamp = node.get_clock().now().to_msg()
        output.status = [status]
        diagnostic_pub.publish(output)

    source_sub = (
        node.create_subscription(
            AudioData, str(g("source_topic")), on_source, qos_profile_sensor_data)
        if mic_enabled else None)
    speaker_sub = (
        node.create_subscription(
            AudioChunk, str(g("speaker_topic")), on_speaker, qos_profile_sensor_data)
        if speaker_enabled else None)
    response_sub = (
        node.create_subscription(
            Response, str(g("audiohub_response_topic")), on_response, diagnostics_qos)
        if speaker_enabled else None)
    tick_timer = node.create_timer(0.01, tick_audiohub) if speaker_enabled else None
    diagnostic_timer = node.create_timer(1.0, publish_diagnostic)
    node.get_logger().info(
        f"Go2 audio adapter started for {robot_id}: profile={profile_name or '<disabled>'} "
        f"mic={mic_enabled} speaker={speaker_enabled}")
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if mic_core is not None:
            mic_core.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
