"""Bounded Ferox AudioChunk to Unitree G1 Voice API speaker adapter."""
from __future__ import annotations

import json
import time
import uuid

import rclpy
from ferox_msgs.msg import AudioChunk
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from unitree_api.msg import Request, Response

from .pcm_gate import PcmContractError, PcmGate


PLAY_STREAM_API_ID = 1003
STOP_PLAY_API_ID = 1004
GET_VOLUME_API_ID = 1005


class G1VoiceBridge(Node):
    """Speaker-only adapter; no microphone is advertised without real evidence."""

    def __init__(self) -> None:
        super().__init__("g1_voice_bridge")
        self.declare_parameter("speaker_enabled", False)
        self.declare_parameter("query_volume_on_start", True)
        self.declare_parameter("app_name", "ferox_speech")
        self.declare_parameter("request_timeout_s", 2.0)

        self._speaker_enabled = bool(self.get_parameter("speaker_enabled").value)
        self._query_volume = bool(self.get_parameter("query_volume_on_start").value)
        self._app_name = str(self.get_parameter("app_name").value).strip()
        self._request_timeout_s = float(self.get_parameter("request_timeout_s").value)
        if not self._app_name or not 0.1 <= self._request_timeout_s <= 10.0:
            raise RuntimeError("invalid app_name or request_timeout_s")

        self._gate = PcmGate()
        self._stream_id = uuid.uuid4().hex
        self._identity_counter = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        self._inflight_id: int | None = None
        self._inflight_kind: str | None = None
        self._inflight_since_s: float | None = None
        self._latched_fault: str | None = None
        self._accepted_chunks = 0
        self._rejected_chunks = 0
        self._requests_ok = 0
        self._play_requests_ok = 0

        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
        self._request_pub = self.create_publisher(Request, "/api/voice/request", qos)
        self._response_sub = self.create_subscription(
            Response, "/api/voice/response", self._on_response, qos)
        self._speaker_sub = self.create_subscription(
            AudioChunk,
            "audio/speaker_out",
            self._on_audio,
            qos_profile_sensor_data,
        )
        self._timer = self.create_timer(0.02, self._tick)

        if not self._speaker_enabled:
            self.get_logger().warning(
                "speaker output is disabled; PCM will be validated and discarded")
        self.get_logger().error(
            "no validated G1 microphone source is available; mic_raw is intentionally absent")

    def _next_identity(self) -> int:
        self._identity_counter += 1
        return self._identity_counter

    def _publish_request(
        self, api_id: int, parameter: dict[str, object], binary: bytes, kind: str
    ) -> None:
        if self._inflight_id is not None:
            raise RuntimeError("only one Unitree voice request may be in flight")
        request = Request()
        identity = self._next_identity()
        request.header.identity.id = identity
        request.header.identity.api_id = api_id
        request.parameter = json.dumps(parameter, separators=(",", ":"))
        request.binary = list(binary)
        self._inflight_id = identity
        self._inflight_kind = kind
        self._inflight_since_s = time.monotonic()
        self._request_pub.publish(request)

    def _on_audio(self, message: AudioChunk) -> None:
        now_ros = self.get_clock().now().nanoseconds / 1e9
        now_steady = time.monotonic()
        source_stamp = float(message.header.stamp.sec) + (
            float(message.header.stamp.nanosec) / 1e9)
        try:
            self._gate.accept(
                data=bytes(message.data),
                sample_rate=int(message.sample_rate),
                channels=int(message.channels),
                sample_width=int(message.sample_width),
                source_stamp_s=source_stamp,
                receive_ros_s=now_ros,
                receive_steady_s=now_steady,
            )
        except PcmContractError as exc:
            self._rejected_chunks += 1
            self.get_logger().error(f"speaker chunk rejected: {exc}")
            return
        self._accepted_chunks += 1
        if not self._speaker_enabled:
            self._gate.clear()

    def _tick(self) -> None:
        now = time.monotonic()
        if self._inflight_id is not None:
            assert self._inflight_since_s is not None
            if now - self._inflight_since_s > self._request_timeout_s:
                self._latch_fault(f"{self._inflight_kind} request timed out")
            return
        if self._latched_fault is not None:
            return
        if self._query_volume:
            self._query_volume = False
            self._publish_request(GET_VOLUME_API_ID, {}, b"", "get_volume")
            return
        if not self._speaker_enabled:
            return
        payload = self._gate.pop_request(now)
        if payload is not None:
            self._publish_request(
                PLAY_STREAM_API_ID,
                {"app_name": self._app_name, "stream_id": self._stream_id},
                payload,
                "play_stream",
            )

    def _on_response(self, message: Response) -> None:
        if self._inflight_id is None:
            return
        if int(message.header.identity.id) != self._inflight_id:
            return
        kind = self._inflight_kind
        status = int(message.header.status.code)
        self._inflight_id = None
        self._inflight_kind = None
        self._inflight_since_s = None
        if status != 0:
            self._latch_fault(f"{kind} returned Unitree status {status}")
            return
        self._requests_ok += 1
        if kind == "play_stream":
            self._play_requests_ok += 1
        if kind == "get_volume":
            try:
                volume = int(json.loads(message.data)["volume"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._latch_fault("get_volume returned an invalid payload")
                return
            if not 0 <= volume <= 100:
                self._latch_fault("get_volume returned an out-of-range value")
                return
            self.get_logger().info(f"G1 voice API ready; reported volume={volume}")

    def _latch_fault(self, reason: str) -> None:
        if self._latched_fault is None:
            self._latched_fault = reason
            self._gate.clear()
            self.get_logger().fatal(f"G1 voice bridge latched fail-closed: {reason}")
        self._inflight_id = None
        self._inflight_kind = None
        self._inflight_since_s = None

    def shutdown(self) -> None:
        self._gate.clear()
        if self._speaker_enabled and self._play_requests_ok:
            # Never block shutdown on the optional stop acknowledgement.
            try:
                request = Request()
                request.header.identity.id = self._next_identity()
                request.header.identity.api_id = STOP_PLAY_API_ID
                request.parameter = json.dumps({"app_name": self._app_name})
                self._request_pub.publish(request)
            except Exception as exc:  # noqa: BLE001
                self.get_logger().error(f"failed to publish best-effort PlayStop: {exc}")


def main(args=None) -> None:
    rclpy.init(args=args)
    node = G1VoiceBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
