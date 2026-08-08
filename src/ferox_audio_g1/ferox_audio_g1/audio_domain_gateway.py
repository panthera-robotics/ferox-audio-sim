"""Narrow G1 audio gateway: AudioChunk 42->0, diagnostics 0->42 only."""
from __future__ import annotations

import argparse
import math
import queue
import re
import threading
import time

import rclpy
from diagnostic_msgs.msg import DiagnosticArray
from ferox_msgs.msg import AudioChunk
from rclpy.context import Context
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data

from .diagnostic_contract import validate_audio_diagnostic
from .pcm_gate import PcmContractError, PcmGate


class AudioDomainGateway:
    def __init__(self, *, robot_domain: int, application_domain: int,
                 robot_id: str) -> None:
        if not 0 <= int(robot_domain) <= 232 or not 0 <= int(application_domain) <= 232:
            raise ValueError("DDS domains must be in [0, 232]")
        if int(robot_domain) == int(application_domain):
            raise ValueError("robot and application domains must differ")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(robot_id)):
            raise ValueError("robot_id is invalid")
        self._robot_id = robot_id
        self._audio_topic = f"/ferox/{robot_id}/audio/speaker_out"
        self._diagnostic_topic = f"/ferox/{robot_id}/audio/diagnostics"
        self._audio_gate = PcmGate()
        self._last_source_stamp_s: float | None = None
        self._audio_queue: queue.Queue = queue.Queue(maxsize=4)
        self._diagnostic_queue: queue.Queue = queue.Queue(maxsize=1)

        self._robot_context = Context()
        self._app_context = Context()
        self._robot_context.init(
            domain_id=int(robot_domain), initialize_logging=True)
        self._app_context.init(
            domain_id=int(application_domain), initialize_logging=False)
        self._robot_node = Node(
            "ferox_audio_robot_gateway", context=self._robot_context)
        self._app_node = Node(
            "ferox_audio_application_gateway", context=self._app_context)
        self._app_audio_sub = self._app_node.create_subscription(
            AudioChunk, self._audio_topic, self._on_audio, qos_profile_sensor_data)
        self._robot_audio_pub = self._robot_node.create_publisher(
            AudioChunk, self._audio_topic, qos_profile_sensor_data)
        diagnostics_qos = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.RELIABLE)
        self._robot_diagnostic_sub = self._robot_node.create_subscription(
            DiagnosticArray, self._diagnostic_topic,
            self._on_diagnostic, diagnostics_qos)
        self._app_diagnostic_pub = self._app_node.create_publisher(
            DiagnosticArray, self._diagnostic_topic, diagnostics_qos)
        self._robot_audio_timer = self._robot_node.create_timer(
            0.01, self._flush_audio)
        self._app_diagnostic_timer = self._app_node.create_timer(
            0.1, self._flush_diagnostic)
        self._robot_executor = MultiThreadedExecutor(
            num_threads=2, context=self._robot_context)
        self._app_executor = MultiThreadedExecutor(
            num_threads=2, context=self._app_context)
        self._robot_executor.add_node(self._robot_node)
        self._app_executor.add_node(self._app_node)
        self._threads = [
            threading.Thread(target=self._robot_executor.spin, daemon=True),
            threading.Thread(target=self._app_executor.spin, daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self._app_node.get_logger().info(
            f"narrow audio gateway ready: AudioChunk {application_domain}->{robot_domain}, "
            f"diagnostics {robot_domain}->{application_domain}")

    @staticmethod
    def _source_stamp_s(message: AudioChunk) -> float:
        stamp = message.header.stamp
        value = float(stamp.sec) + float(stamp.nanosec) * 1e-9
        if not math.isfinite(value) or value <= 0.0:
            raise PcmContractError("AudioChunk source timestamp is invalid")
        return value

    def _on_audio(self, message: AudioChunk) -> None:
        try:
            stamp_s = self._source_stamp_s(message)
            if (self._last_source_stamp_s is not None
                    and stamp_s <= self._last_source_stamp_s):
                raise PcmContractError("AudioChunk source timestamp regressed")
            self._audio_gate.accept(
                data=bytes(message.data),
                sample_rate=int(message.sample_rate),
                channels=int(message.channels),
                sample_width=int(message.sample_width),
                contract_version=int(message.contract_version),
                encoding=int(message.encoding),
                stream_id=str(message.stream_id),
                sequence=int(message.sequence),
                sample_offset=int(message.sample_offset),
                flags=int(message.flags),
                receive_steady_s=time.monotonic(),
            )
            self._last_source_stamp_s = stamp_s
            self._audio_gate.discard_buffer()
            self._audio_queue.put_nowait(message)
        except (PcmContractError, queue.Full) as exc:
            self._audio_gate.clear()
            self._last_source_stamp_s = None
            self._app_node.get_logger().error(
                f"AudioChunk rejected at domain boundary: {exc}")

    def _flush_audio(self) -> None:
        try:
            message = self._audio_queue.get_nowait()
        except queue.Empty:
            return
        self._robot_audio_pub.publish(message)

    def _on_diagnostic(self, message: DiagnosticArray) -> None:
        error = validate_audio_diagnostic(message, robot_id=self._robot_id)
        if error:
            self._robot_node.get_logger().error(
                f"audio diagnostic rejected at domain boundary: {error}")
            return
        try:
            self._diagnostic_queue.put_nowait(message)
        except queue.Full:
            try:
                self._diagnostic_queue.get_nowait()
            except queue.Empty:
                pass
            self._diagnostic_queue.put_nowait(message)

    def _flush_diagnostic(self) -> None:
        try:
            message = self._diagnostic_queue.get_nowait()
        except queue.Empty:
            return
        self._app_diagnostic_pub.publish(message)

    def close(self) -> None:
        self._robot_executor.shutdown()
        self._app_executor.shutdown()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._robot_node.destroy_node()
        self._app_node.destroy_node()
        self._robot_context.try_shutdown()
        self._app_context.try_shutdown()


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--robot-domain", type=int, default=0)
    parser.add_argument("--application-domain", type=int, default=42)
    parser.add_argument("--robot-id", default="g1_01")
    options = parser.parse_args(args)
    gateway = AudioDomainGateway(
        robot_domain=options.robot_domain,
        application_domain=options.application_domain,
        robot_id=options.robot_id)
    try:
        while gateway._robot_context.ok() and gateway._app_context.ok():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
