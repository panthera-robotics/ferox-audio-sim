"""Bounded Ferox AudioChunk to Unitree G1 Voice API speaker adapter."""
from __future__ import annotations

import json
import time

import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from ferox_msgs.msg import AudioChunk
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from unitree_api.msg import Request, Response

from .pcm_gate import PcmContract, PcmContractError, PcmGate
from .playback_telemetry import PlaybackTelemetry
from .health import voice_health_report
from .unitree_voice_contract import (
    GET_VOLUME_API_ID,
    PLAY_STREAM_API_ID,
    STOP_PLAY_API_ID,
)


class G1VoiceBridge(Node):
    """Speaker-only adapter; no microphone is advertised without real evidence."""

    def __init__(self) -> None:
        super().__init__("g1_voice_bridge")
        self.declare_parameter("speaker_enabled", False)
        self.declare_parameter("query_volume_on_start", True)
        self.declare_parameter("app_name", "ferox_speech")
        self.declare_parameter("request_timeout_s", 2.0)
        self.declare_parameter("target_request_ms", 1000)
        self.declare_parameter("max_buffer_ms", 3000)
        self.declare_parameter("max_interarrival_ms", 500)
        self.declare_parameter("idle_flush_ms", 150)

        self._speaker_enabled = bool(self.get_parameter("speaker_enabled").value)
        self._query_volume = bool(self.get_parameter("query_volume_on_start").value)
        self._app_name = str(self.get_parameter("app_name").value).strip()
        self._request_timeout_s = float(self.get_parameter("request_timeout_s").value)
        if not self._app_name or not 0.1 <= self._request_timeout_s <= 10.0:
            raise RuntimeError("invalid app_name or request_timeout_s")

        bytes_per_ms = 16_000 * 1 * 2 // 1000
        self._gate = PcmGate(PcmContract(
            target_request_bytes=(
                int(self.get_parameter("target_request_ms").value) * bytes_per_ms),
            max_buffer_bytes=(
                int(self.get_parameter("max_buffer_ms").value) * bytes_per_ms),
            max_interarrival_s=(
                float(self.get_parameter("max_interarrival_ms").value) / 1000.0),
            idle_flush_s=float(self.get_parameter("idle_flush_ms").value) / 1000.0,
        ))
        self._playback_telemetry = PlaybackTelemetry()
        self._unitree_stream_id = f"{self._app_name}-{time.clock_gettime_ns(time.CLOCK_BOOTTIME)}"
        self._identity_counter = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        self._inflight_id: int | None = None
        self._inflight_kind: str | None = None
        self._inflight_since_s: float | None = None
        self._latched_fault: str | None = None
        self._accepted_chunks = 0
        self._rejected_chunks = 0
        self._requests_ok = 0
        self._play_requests_ok = 0
        self._request_timeout_total = 0
        self._unitree_error_total = 0
        self._volume_confirmed = False
        namespace_parts = [part for part in self.get_namespace().split("/") if part]
        if len(namespace_parts) < 2 or namespace_parts[-2] != "ferox":
            raise RuntimeError("G1 voice bridge must run in /ferox/<robot_id>")
        self._robot_id = namespace_parts[-1]

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
        self._diagnostic_pub = self.create_publisher(
            DiagnosticArray, "audio/diagnostics", qos)
        self._timer = self.create_timer(0.02, self._tick)
        self._diagnostic_timer = self.create_timer(1.0, self._publish_diagnostics)

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
        now_steady = time.monotonic()
        try:
            self._gate.accept(
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
                receive_steady_s=now_steady,
            )
        except PcmContractError as exc:
            self._rejected_chunks += 1
            self.get_logger().error(f"speaker chunk rejected: {exc}")
            return
        self._accepted_chunks += 1
        if not self._speaker_enabled:
            self._gate.discard_buffer()

    def _tick(self) -> None:
        now = time.monotonic()
        if self._inflight_id is not None:
            assert self._inflight_since_s is not None
            if now - self._inflight_since_s > self._request_timeout_s:
                self._request_timeout_total += 1
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
            evidence = self._gate.last_pop_evidence
            if evidence is None:
                self._latch_fault("PCM gate omitted request evidence")
                return
            self._playback_telemetry.record_dispatch(evidence)
            self._publish_request(
                PLAY_STREAM_API_ID,
                {"app_name": self._app_name, "stream_id": self._unitree_stream_id},
                payload,
                "play_stream",
            )

    def _on_response(self, message: Response) -> None:
        if self._inflight_id is None:
            return
        if int(message.header.identity.id) != self._inflight_id:
            return
        kind = self._inflight_kind
        response_latency_ms = (
            (time.monotonic() - self._inflight_since_s) * 1000.0
            if self._inflight_since_s is not None else None)
        status = int(message.header.status.code)
        self._inflight_id = None
        self._inflight_kind = None
        self._inflight_since_s = None
        if status != 0:
            self._unitree_error_total += 1
            self._latch_fault(f"{kind} returned Unitree status {status}")
            return
        self._requests_ok += 1
        if kind == "play_stream":
            assert response_latency_ms is not None
            self._playback_telemetry.record_play_response(response_latency_ms)
            self._play_requests_ok += 1
        if kind == "get_volume":
            try:
                volume = int(json.loads(message.data)["volume"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._unitree_error_total += 1
                self._latch_fault("get_volume returned an invalid payload")
                return
            if not 0 <= volume <= 100:
                self._unitree_error_total += 1
                self._latch_fault("get_volume returned an out-of-range value")
                return
            self._volume_confirmed = True
            self.get_logger().info(f"G1 voice API ready; reported volume={volume}")

    def _publish_diagnostics(self) -> None:
        inflight_age_ms = -1.0
        if self._inflight_since_s is not None:
            inflight_age_ms = max(
                0.0, (time.monotonic() - self._inflight_since_s) * 1000.0)
        report = voice_health_report(
            speaker_enabled=self._speaker_enabled,
            volume_confirmed=self._volume_confirmed,
            latched_fault=self._latched_fault,
            accepted_chunks=self._accepted_chunks,
            rejected_chunks=self._rejected_chunks,
            requests_ok=self._requests_ok,
            play_requests_ok=self._play_requests_ok,
            request_timeout_total=self._request_timeout_total,
            unitree_error_total=self._unitree_error_total,
            buffered_bytes=self._gate.buffered_bytes,
            buffered_audio_ms=self._gate.buffered_audio_ms,
            inflight_age_ms=inflight_age_ms,
            playback=self._playback_telemetry.snapshot(),
        )
        status = DiagnosticStatus()
        status.level = (
            DiagnosticStatus.OK,
            DiagnosticStatus.WARN,
            DiagnosticStatus.ERROR,
            DiagnosticStatus.STALE,
        )[report.level]
        status.name = f"ferox/{self._robot_id}/audio"
        status.message = report.message
        status.hardware_id = self._robot_id
        status.values = [KeyValue(key=key, value=value)
                         for key, value in report.values]
        message = DiagnosticArray()
        message.header.stamp = self.get_clock().now().to_msg()
        message.status = [status]
        self._diagnostic_pub.publish(message)

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
