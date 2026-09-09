"""Receive-only continuity probe for the G1 microphone multicast stream.

Joins the official Unitree G1 audio multicast group, records packet arrival
times and sizes, and hands them to `mic_stream_core` for the verdict.  Audio is
never written to disk, never played, and never forwarded onto ROS: aggregate
statistics only, same custody discipline as the original 20 s probe.

Source of the stream shape:
`unitree_sdk2/example/g1/audio/g1_audio_client_example.cpp` — 16 kHz mono
signed 16-bit PCM on UDP multicast 239.168.123.161:5555.
"""
from __future__ import annotations

import argparse
import array
import ipaddress
import json
import math
import socket
import sys
import time

from .mic_stream_core import (
    MicStreamContract,
    MicStreamError,
    analyze_mic_stream,
    required_jitter_buffer_ms,
)

DEFAULT_GROUP = "239.168.123.161"
DEFAULT_PORT = 5555
MAX_TRACKED_SENDERS = 16


def _ipv4(value: str, *, label: str, multicast: bool) -> str:
    try:
        address = ipaddress.IPv4Address(str(value).strip())
    except ipaddress.AddressValueError as exc:
        raise ValueError(f"{label} must be a dotted IPv4 address") from exc
    if multicast != address.is_multicast:
        kind = "multicast" if multicast else "unicast"
        raise ValueError(f"{label} must be a {kind} IPv4 address")
    if address.is_unspecified:
        raise ValueError(f"{label} cannot be unspecified")
    return str(address)


def _open_multicast_socket(
    *, group: str, port: int, interface: str
) -> tuple[socket.socket, bytes]:
    """Open the receive socket without leaking it on partial setup failure."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    membership = socket.inet_aton(group) + socket.inet_aton(interface)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.settimeout(0.5)
    except BaseException:
        sock.close()
        raise
    return sock, membership


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interface", required=True, help="G1 robot-LAN IPv4 address")
    parser.add_argument(
        "--expected-sender-ip", required=True,
        help="expected vendor microphone source IPv4 address")
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--window-s", type=float, default=10.0)
    parser.add_argument(
        "--packet-bytes", type=int, default=5_120,
        help="expected fixed payload size of the multicast stream")
    parsed = parser.parse_args(argv)
    if not 1.0 <= parsed.duration <= 3_600.0:
        parser.error("--duration must be in [1, 3600] seconds")
    if not 1.0 <= parsed.window_s <= parsed.duration:
        parser.error("--window-s must be in [1, duration]")
    if not 1 <= parsed.port <= 65_535:
        parser.error("--port must be in [1, 65535]")
    try:
        group = _ipv4(parsed.group, label="--group", multicast=True)
        interface = _ipv4(
            parsed.interface, label="--interface", multicast=False)
        expected_sender_ip = _ipv4(
            parsed.expected_sender_ip,
            label="--expected-sender-ip",
            multicast=False,
        )
    except ValueError as exc:
        parser.error(str(exc))

    try:
        contract = MicStreamContract(packet_bytes=parsed.packet_bytes)
    except MicStreamError as exc:
        parser.error(str(exc))

    sock, membership = _open_multicast_socket(
        group=group, port=parsed.port, interface=interface)

    arrivals: list[float] = []
    payload_sizes: list[int] = []
    sample_count = 0
    nonzero_count = 0
    clipped_count = 0
    sum_squares = 0
    peak = 0
    sender_counts: dict[tuple[str, int], int] = {}
    untracked_sender_packets = 0
    unexpected_sender_packets = 0

    started = time.monotonic()
    deadline = started + parsed.duration
    try:
        while time.monotonic() < deadline:
            try:
                payload, sender = sock.recvfrom(65535)
            except socket.timeout:
                continue
            endpoint = (str(sender[0]), int(sender[1]))
            if endpoint in sender_counts or len(sender_counts) < MAX_TRACKED_SENDERS:
                sender_counts[endpoint] = sender_counts.get(endpoint, 0) + 1
            else:
                untracked_sender_packets += 1
            if endpoint[0] != expected_sender_ip:
                unexpected_sender_packets += 1
                continue
            arrivals.append(time.monotonic())
            payload_sizes.append(len(payload))
            usable = len(payload) - (len(payload) % 2)
            samples = array.array("h")
            samples.frombytes(payload[:usable])
            if sys.byteorder != "little":
                samples.byteswap()
            sample_count += len(samples)
            for sample in samples:
                magnitude = abs(sample)
                peak = max(peak, magnitude)
                nonzero_count += sample != 0
                clipped_count += magnitude >= 32_767
                sum_squares += sample * sample
    finally:
        try:
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, membership)
            except OSError:
                # The kernel may already have torn down membership after an
                # interface transition. Closing the descriptor is sufficient.
                pass
        finally:
            sock.close()

    elapsed = time.monotonic() - started
    report = analyze_mic_stream(
        arrivals_s=arrivals,
        payload_sizes=payload_sizes,
        elapsed_s=elapsed,
        contract=contract,
        window_s=parsed.window_s,
    )
    result = {
        "source": (
            "unitree_sdk2/example/g1/audio/g1_audio_client_example.cpp"),
        "source_identity_boundary": (
            "sender IPv4 is a topology check, not cryptographic authentication"),
        "mode": "receive_only_no_audio_saved",
        "group": group,
        "port": parsed.port,
        "interface": interface,
        "expected_sender_ip": expected_sender_ip,
        "sender_endpoints": [
            {"ip": ip, "port": port, "packets": count}
            for (ip, port), count in sorted(sender_counts.items())
        ],
        "unexpected_sender_packets": unexpected_sender_packets,
        "untracked_sender_packets": untracked_sender_packets,
        "stream": report.as_dict(),
        "required_jitter_buffer_ms": (
            round(value, 6)
            if (value := required_jitter_buffer_ms(report)) is not None
            else None),
        "pcm": {
            "format": "s16le",
            "sample_rate_hz": contract.sample_rate,
            "channels": contract.channels,
            "samples": sample_count,
            "duration_s": sample_count / contract.sample_rate,
            "rms": (
                math.sqrt(sum_squares / sample_count) if sample_count else 0.0),
            "peak": peak,
            "nonzero_fraction": (
                nonzero_count / sample_count if sample_count else 0.0),
            "clipped_samples": clipped_count,
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if not report.packets:
        return 2
    return 0 if report.continuous and not unexpected_sender_packets else 1


if __name__ == "__main__":
    raise SystemExit(main())
