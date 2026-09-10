#!/usr/bin/env python3
"""Real Humble wire test; synthetic PCM, no hardware or speaker interfaces.

Run only in Docker --network none: the test refuses active non-loopback interfaces.
Only hardware evidence loading is mocked in the child, not ROS, graph GIDs,
the production adapter, codec, chunking, or continuity guard. The evidence
fixture is NOT a hardware qualification. --negative-control disables only
subscriber resync to reproduce the original late-join failure on real DDS.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch


def isolated():
    # Docker Desktop may expose dormant kernel tunnel devices in network none.
    active = {p.name for p in Path("/sys/class/net").iterdir()
              if (p / "flags").is_file() and int((p / "flags").read_text(), 16) & 1}
    if active != {"lo"}:
        raise RuntimeError("requires Docker --network none (only loopback active)")


def worker(negative):
    from ferox_audio_go2 import bridge_node
    from ferox_audio_go2.mic_bridge import Go2MicBridgeCore
    with patch.object(bridge_node, "load_profile_evidence",
                      return_value=SimpleNamespace(speaker_confirmed=False)):
        if negative:
            Go2MicBridgeCore.observe_subscribers = lambda self, identities: None
        bridge_node.main([
            "--ros-args", "-r", "__ns:=/ferox/go2_99",
            "-p", "robot_id:=go2_99", "-p", "mic_enabled:=true",
            "-p", "speaker_enabled:=false",
            "-p", "hardware_profile:=go2_opus48_audiohub_v1",
            "-p", "runtime_firmware:=synthetic-offline-fixture"])


def opus_silence():
    """Encode and pad one real 20ms Opus packet to the profile's 160 bytes."""
    lib = ctypes.CDLL(ctypes.util.find_library("opus"))
    lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int,
                                       ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_encoder_create.restype = ctypes.c_void_p
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16),
                               ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32]
    lib.opus_encode.restype = ctypes.c_int32
    lib.opus_packet_pad.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
                                   ctypes.c_int32]
    error = ctypes.c_int(-1)
    encoder = lib.opus_encoder_create(48000, 1, 2048, ctypes.byref(error))
    assert encoder and error.value == 0
    try:
        packet = (ctypes.c_ubyte * 160)()
        size = lib.opus_encode(encoder, (ctypes.c_int16 * 960)(), 960, packet, 160)
        assert 0 < size <= 160
        assert lib.opus_packet_pad(packet, size, 160) == 0
        return bytes(packet)
    finally:
        lib.opus_encoder_destroy(encoder)


def run(negative):
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
    from ferox_msgs.msg import AudioChunk
    from unitree_go.msg import AudioData
    from ferox_audio_go2.stream_contract import AudioStreamGuard, AudioStreamError
    from ferox_audio_go2 import bridge_node, mic_bridge

    rclpy.init()
    node = Node("offline_lifecycle_source")
    topic = "/ferox/go2_99/audio/mic_raw"
    source = node.create_publisher(AudioData, "/audiosender", QoSProfile(
        depth=32, reliability=ReliabilityPolicy.RELIABLE))
    payload = opus_silence()
    frames = [0]
    listeners = {}
    failures = []

    def publish():
        frames[0] += 1
        message = AudioData()
        message.time_frame = frames[0] * 200000
        message.data = payload
        source.publish(message)

    timer = node.create_timer(.02, publish)
    child = subprocess.Popen([sys.executable, __file__, "--worker"] +
                             (["--negative-control"] if negative else []),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def pump(predicate, label, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise AssertionError("adapter exited during " + label)
            rclpy.spin_once(node, timeout_sec=.01)
            if failures:
                raise AssertionError(str(failures))
            if predicate():
                return
        raise AssertionError("deadline waiting for " + label)

    def listen(name):
        record = {"accepted": 0, "leading_rejections": 0, "starts": [], "stream": None}
        guard = AudioStreamGuard()

        def receive(message):
            try:
                accepted = guard.accept(message)
            except AudioStreamError as exc:
                if record["accepted"]:
                    failures.append(name + ": " + str(exc))
                else:
                    record["leading_rejections"] += 1
                return
            if accepted.started:
                assert message.flags & 5 == 5
                assert message.sequence == message.sample_offset == 0
                record["starts"].append(message.stream_id)
            record["stream"] = message.stream_id
            record["accepted"] += 1

        subscription = node.create_subscription(AudioChunk, topic, receive, qos_profile_sensor_data)
        listeners[name] = (record, subscription)
        return record

    def gids():
        return {bytes(endpoint.endpoint_gid).hex()
                for endpoint in node.get_subscriptions_info_by_topic(topic)}

    try:
        pump(lambda: source.get_subscription_count() == 1, "source discovery")
        # Ensure the adapter's original START is gone before anyone subscribes.
        baseline = frames[0]
        pump(lambda: frames[0] >= baseline + 25, "unobserved running source")
        a = listen("a")
        pump(lambda: a["accepted"] >= 3, "first late subscriber START")
        b = listen("b")
        pump(lambda: b["accepted"] >= 3 and len(a["starts"]) >= 2, "second subscriber START")
        pump(lambda: len(gids()) == 2, "two endpoints")
        before = gids()
        node.destroy_subscription(listeners["b"][1])
        c = listen("c")
        pump(lambda: len(gids()) == 2 and gids() != before, "same-count GID replacement")
        after = gids()
        pump(lambda: c["accepted"] >= 3 and a["stream"] == c["stream"],
             "replacement START accepted by old and new listeners")
        assert len(before & after) == 1
        assert c["starts"][0] != b["starts"][0]
        assert len(a["starts"]) >= 3
        node.destroy_subscription(listeners["a"][1])
        node.destroy_subscription(listeners["c"][1])
        pump(lambda: not gids(), "complete disconnect")
        baseline = frames[0]
        pump(lambda: frames[0] >= baseline + 15, "disconnected source continues")
        d = listen("d")
        pump(lambda: d["accepted"] >= 3, "rejoin START")
        child.send_signal(signal.SIGTERM)
        log, _ = child.communicate(timeout=8)
        assert child.returncode == 0, log
        assert "Traceback" not in log, log
        assert not any("audiohub" in name for name, _ in node.get_topic_names_and_types())
        return {"passed": True, "mode": "network_none_synthetic_opus_real_ros",
                "revision": os.environ.get("OFFLINE_TEST_REVISION"),
                "image": os.environ.get("OFFLINE_TEST_IMAGE"),
                "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "source_sha256": {Path(module.__file__).name:
                    hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                    for module in (bridge_node, mic_bridge)},
                "gid_count_before": len(before), "gid_count_after": len(after),
                "retained_gids": len(before & after), "shutdown_exit_code": child.returncode,
                "listeners": {name: entry[0] for name, entry in listeners.items()},
                "hardware_qualified": False, "speech_quality_qualified": False}
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=2)
        node.destroy_timer(timer)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--negative-control", action="store_true")
    args = parser.parse_args()
    isolated()
    os.environ["ROS_DOMAIN_ID"] = "191"
    os.environ["ROS_LOCALHOST_ONLY"] = "1"
    if args.worker:
        worker(args.negative_control)
    else:
        print(json.dumps(run(args.negative_control), indent=2))
