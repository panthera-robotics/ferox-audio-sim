#!/usr/bin/env python3
"""Real two-domain ROS smoke for the narrow G1 audio gateway."""
from __future__ import annotations

import threading
import time

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from ferox_audio_g1.audio_domain_gateway import AudioDomainGateway
from ferox_audio_g1.diagnostic_contract import BOOLEANS, COUNTERS, TIMINGS
from ferox_msgs.msg import AudioChunk
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data


def _context_node(domain, name):
    context = Context()
    context.init(domain_id=domain, initialize_logging=False)
    node = Node(name, context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    return context, node, executor, thread


def _audio(node, sequence, offset, flags):
    message = AudioChunk()
    message.header.stamp = node.get_clock().now().to_msg()
    message.contract_version = 1
    message.encoding = 1
    message.stream_id = "gateway-smoke"
    message.sequence = sequence
    message.sample_offset = offset
    message.flags = flags
    message.sample_rate = 16000
    message.channels = 1
    message.sample_width = 2
    message.data = bytes(3200)
    return message


def _diagnostic(node):
    values = {key: "0" for key in COUNTERS}
    values.update({key: "false" for key in BOOLEANS})
    values.update({
        "schema_version": "2",
        "ready": "true",
        "speaker_enabled": "true",
        "volume_confirmed": "true",
        "microphone_available": "false",
        "last_fault": "",
    })
    values.update({key: ("0.0" if key == "buffered_audio_ms" else "-1.0")
                   for key in TIMINGS})
    status = DiagnosticStatus()
    status.level = DiagnosticStatus.OK
    status.name = "ferox/g1_01/audio"
    status.message = "G1 voice adapter healthy"
    status.hardware_id = "g1_01"
    status.values = [KeyValue(key=key, value=value)
                     for key, value in values.items()]
    message = DiagnosticArray()
    message.header.stamp = node.get_clock().now().to_msg()
    message.status = [status]
    return message


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def main():
    gateway = AudioDomainGateway(
        robot_domain=17, application_domain=18, robot_id="g1_01")
    app = _context_node(18, "audio_gateway_smoke_app")
    robot = _context_node(17, "audio_gateway_smoke_robot")
    app_context, app_node, app_executor, app_thread = app
    robot_context, robot_node, robot_executor, robot_thread = robot
    received_audio = []
    received_diagnostics = []
    audio_pub = app_node.create_publisher(
        AudioChunk, "/ferox/g1_01/audio/speaker_out", qos_profile_sensor_data)
    robot_node.create_subscription(
        AudioChunk, "/ferox/g1_01/audio/speaker_out",
        received_audio.append, qos_profile_sensor_data)
    diagnostics_qos = QoSProfile(
        depth=1, reliability=ReliabilityPolicy.RELIABLE)
    diagnostic_pub = robot_node.create_publisher(
        DiagnosticArray, "/ferox/g1_01/audio/diagnostics", diagnostics_qos)
    app_node.create_subscription(
        DiagnosticArray, "/ferox/g1_01/audio/diagnostics",
        received_diagnostics.append, diagnostics_qos)
    try:
        time.sleep(1.0)
        audio_pub.publish(_audio(app_node, 0, 0, 1))
        time.sleep(0.10)
        audio_pub.publish(_audio(app_node, 1, 1600, 0))
        time.sleep(0.10)
        audio_pub.publish(_audio(app_node, 2, 3200, 2))
        assert _wait(lambda: len(received_audio) == 3), len(received_audio)

        diagnostic_pub.publish(_diagnostic(robot_node))
        assert _wait(lambda: len(received_diagnostics) == 1), len(received_diagnostics)

        # Continuation without a new START is rejected after END and never
        # appears on the robot domain.
        audio_pub.publish(_audio(app_node, 3, 4800, 0))
        time.sleep(0.3)
        assert len(received_audio) == 3
    finally:
        for context, node, executor, thread in (app, robot):
            executor.shutdown()
            thread.join(timeout=2.0)
            node.destroy_node()
            context.try_shutdown()
        gateway.close()
    print("audio domain gateway smoke passed")


if __name__ == "__main__":
    main()
