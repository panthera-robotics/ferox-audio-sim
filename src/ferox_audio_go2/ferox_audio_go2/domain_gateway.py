"""Narrow Go2 gateway: mic/diagnostics 0->42, speaker AudioChunk 42->0."""
from __future__ import annotations

import argparse
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

from .health import validate_audio_diagnostic
from .stream_contract import AudioStreamError, AudioStreamGuard


class Go2AudioDomainGateway:
    def __init__(self, *, robot_domain: int, application_domain: int,
                 robot_id: str, speaker_sample_rate: int = 22_050,
                 mic_enabled: bool = False, speaker_enabled: bool = False) -> None:
        if not 0 <= int(robot_domain) <= 232 or not 0 <= int(application_domain) <= 232:
            raise ValueError("DDS domains must be in [0, 232]")
        if int(robot_domain) == int(application_domain):
            raise ValueError("robot and application DDS domains must differ")
        if not re.fullmatch(r"go2_[0-9]{2}", str(robot_id)):
            raise ValueError("Go2 robot_id is invalid")
        if int(speaker_sample_rate) != 22_050:
            raise ValueError("qualified Go2 speaker sample rate must be 22050")
        self._robot_id = robot_id
        self._mic_enabled = bool(mic_enabled)
        self._speaker_enabled = bool(speaker_enabled)
        self._mic_topic = f"/ferox/{robot_id}/audio/mic_raw"
        self._speaker_topic = f"/ferox/{robot_id}/audio/speaker_out"
        self._diagnostic_topic = f"/ferox/{robot_id}/audio/diagnostics"
        self._mic_guard = AudioStreamGuard(sample_rate=16_000)
        self._speaker_guard = AudioStreamGuard(sample_rate=int(speaker_sample_rate))
        self._mic_queue: queue.Queue = queue.Queue(maxsize=4)
        self._speaker_queue: queue.Queue = queue.Queue(maxsize=4)
        self._diagnostic_queue: queue.Queue = queue.Queue(maxsize=1)

        self._robot_context = Context()
        self._app_context = Context()
        self._robot_context.init(domain_id=int(robot_domain), initialize_logging=True)
        self._app_context.init(domain_id=int(application_domain), initialize_logging=False)
        self._robot_node = Node("go2_audio_robot_gateway", context=self._robot_context)
        self._app_node = Node("go2_audio_application_gateway", context=self._app_context)
        self._robot_mic_sub = None
        self._app_mic_pub = None
        self._app_speaker_sub = None
        self._robot_speaker_pub = None
        if self._mic_enabled:
            self._robot_mic_sub = self._robot_node.create_subscription(
                AudioChunk, self._mic_topic, self._on_mic, qos_profile_sensor_data)
            self._app_mic_pub = self._app_node.create_publisher(
                AudioChunk, self._mic_topic, qos_profile_sensor_data)
        if self._speaker_enabled:
            self._app_speaker_sub = self._app_node.create_subscription(
                AudioChunk, self._speaker_topic, self._on_speaker,
                qos_profile_sensor_data)
            self._robot_speaker_pub = self._robot_node.create_publisher(
                AudioChunk, self._speaker_topic, qos_profile_sensor_data)
        diagnostics_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
        self._robot_diagnostic_sub = self._robot_node.create_subscription(
            DiagnosticArray, self._diagnostic_topic,
            self._on_diagnostic, diagnostics_qos)
        self._app_diagnostic_pub = self._app_node.create_publisher(
            DiagnosticArray, self._diagnostic_topic, diagnostics_qos)
        self._app_mic_timer = (
            self._app_node.create_timer(0.01, self._flush_mic)
            if self._mic_enabled else None)
        self._robot_speaker_timer = (
            self._robot_node.create_timer(0.01, self._flush_speaker)
            if self._speaker_enabled else None)
        self._app_diagnostic_timer = self._app_node.create_timer(
            0.1, self._flush_diagnostic)
        self._robot_executor = MultiThreadedExecutor(
            num_threads=2, context=self._robot_context)
        self._app_executor = MultiThreadedExecutor(
            num_threads=2, context=self._app_context)
        self._robot_executor.add_node(self._robot_node)
        self._app_executor.add_node(self._app_node)
        self._stop_event = threading.Event()
        self._threads = [
            threading.Thread(
                target=self._spin_executor,
                args=(self._robot_executor, self._robot_context), daemon=True),
            threading.Thread(
                target=self._spin_executor,
                args=(self._app_executor, self._app_context), daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self._app_node.get_logger().info(
            "narrow Go2 audio gateway ready: diagnostics robot->application; "
            f"mic robot->application={self._mic_enabled}; "
            f"speaker application->robot={self._speaker_enabled}")

    def _spin_executor(self, executor, context) -> None:
        while not self._stop_event.is_set() and context.ok():
            executor.spin_once(timeout_sec=0.1)

    @staticmethod
    def _enqueue(target: queue.Queue, message) -> None:
        target.put_nowait(message)

    def _on_mic(self, message: AudioChunk) -> None:
        try:
            self._mic_guard.accept(message)
            self._enqueue(self._mic_queue, message)
        except (AudioStreamError, queue.Full) as exc:
            self._mic_guard.reset()
            self._robot_node.get_logger().error(
                f"Go2 microphone AudioChunk rejected at domain boundary: {exc}")

    def _on_speaker(self, message: AudioChunk) -> None:
        try:
            self._speaker_guard.accept(message)
            self._enqueue(self._speaker_queue, message)
        except (AudioStreamError, queue.Full) as exc:
            self._speaker_guard.reset()
            self._app_node.get_logger().error(
                f"Go2 speaker AudioChunk rejected at domain boundary: {exc}")

    def _on_diagnostic(self, message: DiagnosticArray) -> None:
        error = validate_audio_diagnostic(message, robot_id=self._robot_id)
        if error:
            self._robot_node.get_logger().error(
                f"Go2 audio diagnostic rejected at domain boundary: {error}")
            return
        try:
            self._diagnostic_queue.put_nowait(message)
        except queue.Full:
            try:
                self._diagnostic_queue.get_nowait()
            except queue.Empty:
                pass
            self._diagnostic_queue.put_nowait(message)

    def _flush_mic(self) -> None:
        try:
            message = self._mic_queue.get_nowait()
        except queue.Empty:
            return
        assert self._app_mic_pub is not None
        self._app_mic_pub.publish(message)

    def _flush_speaker(self) -> None:
        try:
            message = self._speaker_queue.get_nowait()
        except queue.Empty:
            return
        assert self._robot_speaker_pub is not None
        self._robot_speaker_pub.publish(message)

    def _flush_diagnostic(self) -> None:
        try:
            self._app_diagnostic_pub.publish(self._diagnostic_queue.get_nowait())
        except queue.Empty:
            return

    def close(self) -> None:
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._robot_executor.shutdown()
        self._app_executor.shutdown()
        self._robot_executor.remove_node(self._robot_node)
        self._app_executor.remove_node(self._app_node)
        self._robot_node.destroy_node()
        self._app_node.destroy_node()
        self._robot_context.try_shutdown()
        self._app_context.try_shutdown()


def main(args=None) -> None:
    def enabled(value: str) -> bool:
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        raise argparse.ArgumentTypeError("expected true or false")

    parser = argparse.ArgumentParser()
    parser.add_argument("--robot-domain", type=int, default=0)
    parser.add_argument("--application-domain", type=int, default=42)
    parser.add_argument("--robot-id", default="go2_02")
    parser.add_argument("--speaker-sample-rate", type=int, default=22_050)
    parser.add_argument("--mic-enabled", type=enabled, default=False)
    parser.add_argument("--speaker-enabled", type=enabled, default=False)
    options = parser.parse_args(args)
    gateway = Go2AudioDomainGateway(
        robot_domain=options.robot_domain,
        application_domain=options.application_domain,
        robot_id=options.robot_id,
        speaker_sample_rate=options.speaker_sample_rate,
        mic_enabled=options.mic_enabled,
        speaker_enabled=options.speaker_enabled,
    )
    try:
        while gateway._robot_context.ok() and gateway._app_context.ok():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
