#!/usr/bin/env python3
"""Real two-domain ROS smoke for the narrow Go2 audio gateway."""
from __future__ import annotations

import threading
import time

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from ferox_audio_go2.domain_gateway import Go2AudioDomainGateway
from ferox_audio_go2.health import (
    BOOLEANS, COUNTERS, TEXT, TIMINGS, audio_health_report)
from ferox_msgs.msg import AudioChunk
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data


def context_node(domain, name):
    context = Context()
    context.init(domain_id=domain, initialize_logging=False)
    node = Node(name, context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    return context, node, executor, thread


def audio(node, *, rate, stream, sequence, offset, flags, samples):
    message = AudioChunk()
    message.header.stamp = node.get_clock().now().to_msg()
    message.contract_version = 1
    message.encoding = 1
    message.stream_id = stream
    message.sequence = sequence
    message.sample_offset = offset
    message.flags = flags
    message.sample_rate = rate
    message.channels = 1
    message.sample_width = 2
    message.data = bytes(samples * 2)
    return message


def diagnostic(node):
    report = audio_health_report(
        mic_enabled=True,
        speaker_enabled=True,
        profile_evidence_valid=True,
        speaker_evidence_valid=True,
        mic_stream_live=True,
        audiohub_busy=False,
        hardware_profile="go2_opus48_audiohub_v1",
        runtime_firmware="smoke-fw",
        evidence_sha256="a" * 64,
        last_fault=None,
        last_source_age_ms=10.0,
        decode_p50_ms=0.01,
        decode_p95_ms=0.02,
        decode_p99_ms=0.03,
        decode_max_ms=0.04,
        source_to_chunk_p50_ms=80.0,
        source_to_chunk_p95_ms=80.0,
        source_to_chunk_p99_ms=107.4,
        source_to_chunk_max_ms=108.6,
        counters={key: 0 for key in COUNTERS},
    )
    assert set(key for key, _ in report.values) == set((*BOOLEANS, *COUNTERS, *TEXT, *TIMINGS))
    status = DiagnosticStatus()
    status.level = bytes([report.level])
    status.name = "ferox/go2_02/audio"
    status.message = report.message
    status.hardware_id = "go2_02"
    status.values = [KeyValue(key=key, value=value) for key, value in report.values]
    message = DiagnosticArray()
    message.header.stamp = node.get_clock().now().to_msg()
    message.status = [status]
    return message


def wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def main():
    gateway = Go2AudioDomainGateway(
        robot_domain=27, application_domain=28,
        robot_id="go2_02", speaker_sample_rate=22_050,
        mic_enabled=True, speaker_enabled=True)
    robot = context_node(27, "go2_audio_gateway_smoke_robot")
    app = context_node(28, "go2_audio_gateway_smoke_app")
    robot_context, robot_node, robot_executor, robot_thread = robot
    app_context, app_node, app_executor, app_thread = app
    received_mic = []
    received_speaker = []
    received_diagnostic = []
    mic_pub = robot_node.create_publisher(
        AudioChunk, "/ferox/go2_02/audio/mic_raw", qos_profile_sensor_data)
    app_node.create_subscription(
        AudioChunk, "/ferox/go2_02/audio/mic_raw",
        received_mic.append, qos_profile_sensor_data)
    speaker_pub = app_node.create_publisher(
        AudioChunk, "/ferox/go2_02/audio/speaker_out", qos_profile_sensor_data)
    robot_node.create_subscription(
        AudioChunk, "/ferox/go2_02/audio/speaker_out",
        received_speaker.append, qos_profile_sensor_data)
    diagnostic_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
    diagnostic_pub = robot_node.create_publisher(
        DiagnosticArray, "/ferox/go2_02/audio/diagnostics", diagnostic_qos)
    app_node.create_subscription(
        DiagnosticArray, "/ferox/go2_02/audio/diagnostics",
        received_diagnostic.append, diagnostic_qos)
    try:
        time.sleep(1.0)
        mic_pub.publish(audio(
            robot_node, rate=16_000, stream="mic-smoke", sequence=0,
            offset=0, flags=1, samples=1_600))
        mic_pub.publish(audio(
            robot_node, rate=16_000, stream="mic-smoke", sequence=1,
            offset=1_600, flags=2, samples=1_600))
        speaker_pub.publish(audio(
            app_node, rate=22_050, stream="speaker-smoke", sequence=0,
            offset=0, flags=1, samples=2_205))
        speaker_pub.publish(audio(
            app_node, rate=22_050, stream="speaker-smoke", sequence=1,
            offset=2_205, flags=2, samples=2_205))
        diagnostic_pub.publish(diagnostic(robot_node))
        assert wait(lambda: len(received_mic) == 2), len(received_mic)
        assert wait(lambda: len(received_speaker) == 2), len(received_speaker)
        assert wait(lambda: len(received_diagnostic) == 1), len(received_diagnostic)

        # No continuation after END may cross either direction.
        mic_pub.publish(audio(
            robot_node, rate=16_000, stream="mic-smoke", sequence=2,
            offset=3_200, flags=0, samples=1_600))
        speaker_pub.publish(audio(
            app_node, rate=22_050, stream="speaker-smoke", sequence=2,
            offset=4_410, flags=0, samples=2_205))
        # Both rejection callbacks must be fully drained before endpoint
        # teardown; otherwise rmw can hand an already-destroying message to an
        # executor task and emit a nondeterministic unhandled-future warning.
        time.sleep(1.0)
        assert len(received_mic) == 2
        assert len(received_speaker) == 2
    finally:
        # Stop the gateway while the fixture endpoints still exist.  Destroying
        # those endpoints first can leave an in-flight DDS callback holding a
        # message whose underlying rclpy handle is already being destroyed,
        # producing a noisy "exception was never retrieved" after a passing
        # smoke test.
        gateway.close()
        for context, node, executor, thread in (robot, app):
            executor.shutdown()
            thread.join(timeout=2.0)
            node.destroy_node()
            context.try_shutdown()
    print("Go2 audio domain gateway smoke passed")


if __name__ == "__main__":
    main()
