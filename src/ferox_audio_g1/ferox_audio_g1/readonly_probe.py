"""Repeated read-only G1 Voice API health and latency probe."""
from __future__ import annotations

import argparse
import json
import statistics
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from unitree_api.msg import Request, Response

from .voice_bridge_node import GET_VOLUME_API_ID


class VolumeProbe(Node):
    def __init__(self) -> None:
        super().__init__("g1_voice_readonly_probe")
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
        self._identity = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        self.response: Response | None = None
        self.publisher = self.create_publisher(Request, "/api/voice/request", qos)
        self.subscription = self.create_subscription(
            Response, "/api/voice/response", self._on_response, qos)

    def _on_response(self, message: Response) -> None:
        if int(message.header.identity.id) == self._identity:
            self.response = message

    def query_once(self, timeout_s: float) -> tuple[int, float]:
        self._identity += 1
        self.response = None
        request = Request()
        request.header.identity.id = self._identity
        request.header.identity.api_id = GET_VOLUME_API_ID
        request.parameter = "{}"
        started = time.monotonic()
        self.publisher.publish(request)
        deadline = started + timeout_s
        while self.response is None and time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            rclpy.spin_once(self, timeout_sec=min(0.05, remaining))
        latency_s = time.monotonic() - started
        if self.response is None:
            raise TimeoutError("GetVolume timed out")
        if int(self.response.header.status.code) != 0:
            raise RuntimeError(
                f"GetVolume returned status {int(self.response.header.status.code)}")
        try:
            volume = int(json.loads(self.response.data)["volume"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("GetVolume returned an invalid payload") from exc
        if not 0 <= volume <= 100:
            raise RuntimeError(f"GetVolume returned out-of-range volume {volume}")
        return volume, latency_s


def main(args=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--interval-s", type=float, default=0.2)
    parsed = parser.parse_args(args)
    if not 1 <= parsed.samples <= 100:
        parser.error("--samples must be in [1, 100]")
    if not 0.1 <= parsed.timeout_s <= 10.0:
        parser.error("--timeout-s must be in [0.1, 10]")
    if not 0.0 <= parsed.interval_s <= 5.0:
        parser.error("--interval-s must be in [0, 5]")

    rclpy.init()
    node = VolumeProbe()
    try:
        discovery_deadline = time.monotonic() + 2.0
        while time.monotonic() < discovery_deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
        volumes = []
        latencies_ms = []
        for index in range(parsed.samples):
            volume, latency_s = node.query_once(parsed.timeout_s)
            volumes.append(volume)
            latencies_ms.append(latency_s * 1000.0)
            if index + 1 < parsed.samples and parsed.interval_s:
                time.sleep(parsed.interval_s)
        ordered = sorted(latencies_ms)
        p95_index = min(len(ordered) - 1, int(0.95 * len(ordered)))
        print(json.dumps({
            "api_id": GET_VOLUME_API_ID,
            "samples": len(volumes),
            "volume_min": min(volumes),
            "volume_max": max(volumes),
            "latency_ms_median": round(statistics.median(latencies_ms), 3),
            "latency_ms_p95": round(ordered[p95_index], 3),
            "timeouts": 0,
            "status_errors": 0,
            "read_only": True,
        }, sort_keys=True))
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
