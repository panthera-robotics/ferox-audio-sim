#!/usr/bin/env bash
# ============================================================================
# test_echo.sh — acoustic loopback test for the ferox-audio-sim bridge.
#
# Republishes audio/mic_raw straight onto audio/speaker_out inside the running
# container, so you hear your own voice back through the host speaker with a
# ~180 ms delay. This is THE end-to-end check — it proves mic capture and
# speaker playback both work, with a human in the loop.
#
# Usage:
#   ./scripts/test_echo.sh        # speak into the mic, Ctrl+C to stop
#
# Container name is overridable:
#   FEROX_AUDIO_CONTAINER=other ./scripts/test_echo.sh
# ============================================================================
set -euo pipefail

CONTAINER="${FEROX_AUDIO_CONTAINER:-ferox_audio_sim}"

if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
    echo "ERROR: container '${CONTAINER}' is not running." >&2
    echo "Start it first:  ./scripts/start.sh" >&2
    exit 1
fi

echo "Acoustic loopback via '${CONTAINER}': mic_raw -> speaker_out."
echo ">>> Speak into your headset — you should hear yourself. Ctrl+C to stop. <<<"

# docker exec -it: the -t is REQUIRED so SIGINT (Ctrl+C) forwards into the
# container and reaches the python process. Without it, Ctrl+C does not
# reliably reach the inner script and the echo loop keeps running after this
# shell returns. exec hands the terminal straight to the container process.
exec docker exec -it "${CONTAINER}" python3 -c '
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data

from ferox_audio_msgs.msg import AudioChunk

rclpy.init()
node = Node("echo_test")
pub = node.create_publisher(
    AudioChunk, "/ferox/go2_01/audio/speaker_out", qos_profile_sensor_data)
node.create_subscription(
    AudioChunk, "/ferox/go2_01/audio/mic_raw",
    lambda m: pub.publish(m), qos_profile_sensor_data)
node.get_logger().info("echo_test: mic_raw -> speaker_out running")
try:
    rclpy.spin(node)
except (KeyboardInterrupt, ExternalShutdownException):
    pass  # clean Ctrl+C exit — no traceback
finally:
    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()
'
