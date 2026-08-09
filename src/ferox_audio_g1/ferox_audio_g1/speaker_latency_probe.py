"""Explicitly-authorized, low-volume Unitree G1 speaker latency probe."""
from __future__ import annotations

import argparse
import hashlib
import json
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from unitree_api.msg import Request, Response

from .latency_probe_core import synthesize_chirp
from .unitree_voice_contract import (
    GET_VOLUME_API_ID,
    PLAY_STREAM_API_ID,
    SET_VOLUME_API_ID,
    STOP_PLAY_API_ID,
)


class VoiceApiProbe(Node):
    def __init__(self) -> None:
        super().__init__("g1_speaker_latency_probe")
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
        self._identity = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        self._waiting_for: int | None = None
        self._response: Response | None = None
        self._publisher = self.create_publisher(Request, "/api/voice/request", qos)
        self._subscription = self.create_subscription(
            Response, "/api/voice/response", self._on_response, qos)

    def _on_response(self, message: Response) -> None:
        if self._waiting_for is not None and int(message.header.identity.id) == self._waiting_for:
            self._response = message

    def wait_for_service(self, timeout_s: float = 3.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._publisher.get_subscription_count() == 1:
                return
            rclpy.spin_once(self, timeout_sec=0.05)
        raise TimeoutError("Unitree Voice request subscriber was not uniquely discovered")

    def call(
        self,
        *,
        api_id: int,
        parameter: dict[str, object],
        binary: bytes = b"",
        timeout_s: float = 2.0,
    ) -> tuple[Response, int, float]:
        if self._waiting_for is not None:
            raise RuntimeError("another Voice request is already in flight")
        self._identity += 1
        request = Request()
        request.header.identity.id = self._identity
        request.header.identity.api_id = api_id
        request.parameter = json.dumps(parameter, separators=(",", ":"))
        request.binary = list(binary)
        self._waiting_for = self._identity
        self._response = None
        publish_ns = time.monotonic_ns()
        self._publisher.publish(request)
        deadline = time.monotonic() + timeout_s
        try:
            while self._response is None and time.monotonic() < deadline:
                rclpy.spin_once(self, timeout_sec=0.02)
            if self._response is None:
                raise TimeoutError(f"Voice API {api_id} timed out")
            latency_ms = (time.monotonic_ns() - publish_ns) / 1_000_000.0
            if int(self._response.header.status.code) != 0:
                raise RuntimeError(
                    f"Voice API {api_id} returned status "
                    f"{int(self._response.header.status.code)}")
            return self._response, publish_ns, latency_ms
        finally:
            self._waiting_for = None

    def get_volume(self) -> tuple[int, float]:
        response, _, latency_ms = self.call(api_id=GET_VOLUME_API_ID, parameter={})
        try:
            volume = int(json.loads(response.data)["volume"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("GetVolume returned an invalid payload") from exc
        if not 0 <= volume <= 100:
            raise RuntimeError(f"GetVolume returned out-of-range volume {volume}")
        return volume, latency_ms

    def set_volume(self, volume: int) -> float:
        _, _, latency_ms = self.call(
            api_id=SET_VOLUME_API_ID, parameter={"volume": volume})
        return latency_ms


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorize-speaker", action="store_true")
    parser.add_argument("--target-volume", type=int, default=15)
    parser.add_argument("--tone-ms", type=int, default=400)
    parser.add_argument("--amplitude", type=float, default=0.08)
    parsed = parser.parse_args(args)
    if not parsed.authorize_speaker:
        parser.error("--authorize-speaker is required for physical audio output")
    if not 1 <= parsed.target_volume <= 25:
        parser.error("--target-volume must be in [1, 25]")

    pcm = synthesize_chirp(duration_ms=parsed.tone_ms, amplitude=parsed.amplitude)
    rclpy.init()
    node = VoiceApiProbe()
    original_volume: int | None = None
    volume_changed = False
    played = False
    evidence: dict[str, object] = {
        "authorized": True,
        "target_volume": parsed.target_volume,
        "tone_duration_ms": parsed.tone_ms,
        "pcm_bytes": len(pcm),
        "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
    }
    try:
        node.wait_for_service()
        original_volume, get_latency = node.get_volume()
        evidence["original_volume"] = original_volume
        evidence["get_volume_latency_ms"] = round(get_latency, 3)

        set_latency = node.set_volume(parsed.target_volume)
        volume_changed = True
        confirmed_volume, confirm_latency = node.get_volume()
        if confirmed_volume != parsed.target_volume:
            raise RuntimeError(
                f"volume confirmation failed: expected {parsed.target_volume}, "
                f"got {confirmed_volume}")
        evidence["set_volume_latency_ms"] = round(set_latency, 3)
        evidence["confirm_volume_latency_ms"] = round(confirm_latency, 3)

        app_name = "ferox_latency_probe"
        stream_id = f"kevin-{time.clock_gettime_ns(time.CLOCK_BOOTTIME)}"
        _, publish_ns, play_latency = node.call(
            api_id=PLAY_STREAM_API_ID,
            parameter={"app_name": app_name, "stream_id": stream_id},
            binary=pcm,
        )
        played = True
        evidence["play_publish_monotonic_ns"] = publish_ns
        evidence["play_response_latency_ms"] = round(play_latency, 3)
        time.sleep(parsed.tone_ms / 1_000.0 + 0.20)
        _, _, stop_latency = node.call(
            api_id=STOP_PLAY_API_ID, parameter={"app_name": app_name})
        played = False
        evidence["stop_response_latency_ms"] = round(stop_latency, 3)
    finally:
        restoration_errors: list[str] = []
        if played:
            try:
                node.call(
                    api_id=STOP_PLAY_API_ID,
                    parameter={"app_name": "ferox_latency_probe"},
                )
            except Exception as exc:  # noqa: BLE001
                restoration_errors.append(f"stop:{exc}")
        if volume_changed and original_volume is not None:
            try:
                evidence["restore_volume_latency_ms"] = round(
                    node.set_volume(original_volume), 3)
                restored_volume, restore_check_latency = node.get_volume()
                evidence["restore_check_latency_ms"] = round(
                    restore_check_latency, 3)
                evidence["restored_volume"] = restored_volume
                if restored_volume != original_volume:
                    restoration_errors.append(
                        f"restore-confirm:{restored_volume}!={original_volume}")
            except Exception as exc:  # noqa: BLE001
                restoration_errors.append(f"restore:{exc}")
        evidence["restoration_errors"] = restoration_errors
        node.destroy_node()
        rclpy.shutdown()

    if evidence["restoration_errors"]:
        raise RuntimeError("; ".join(evidence["restoration_errors"]))
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
