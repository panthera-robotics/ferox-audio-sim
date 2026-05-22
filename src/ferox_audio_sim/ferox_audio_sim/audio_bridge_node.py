"""
ferox_audio_sim.audio_bridge — host <-> ROS audio bridge.

First implementation of the Ferox audio topic contract. Captures the host's
microphone and publishes it on  audio/mic_raw ; subscribes  audio/speaker_out
and plays it on the host's speaker. The launch file pushes the
/ferox/<robot_id>/ namespace so the resolved topics are:

    /ferox/<robot_id>/audio/mic_raw       (publish)   AudioChunk
    /ferox/<robot_id>/audio/speaker_out   (subscribe) AudioChunk

ferox-speech (and every other consumer) talks to these topics and never
touches /dev/snd or PulseAudio. The same node, pointed at a robot's audio
device instead of a laptop's, becomes ferox_audio_go2 / ferox_audio_g1.

Topic names here are RELATIVE on purpose — the namespace is applied by the
launch file's PushRosNamespace, never hardcoded.
"""
from __future__ import annotations

import time
from math import gcd
from typing import Optional, Tuple

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data

import sounddevice as sd
import soundfile as sf
from scipy.signal import resample_poly

from ferox_audio_msgs.msg import AudioChunk

# Audio is real-time streaming sensor data, so both topics use ROS 2's
# built-in qos_profile_sensor_data (BEST_EFFORT, KEEP_LAST, depth 5). A
# dropped frame is gone, not worth resending — RELIABLE would buffer-and-lag
# on any hiccup. Every consumer MUST use this same profile: DDS silently
# refuses to match a RELIABLE subscriber to a BEST_EFFORT publisher.

# How long to stay in silence-fallback before retrying a failed mic device.
MIC_RETRY_COOLDOWN_S = 30.0


def _device_arg(value: str):
    """Map a string parameter to a sounddevice device selector.

    Empty -> None (system default). All-digits -> int index. Otherwise the
    string is passed through as a device-name substring match.
    """
    value = (value or "").strip()
    if value == "":
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    return value


class AudioBridge(Node):
    def __init__(self):
        super().__init__("audio_bridge")

        # ---- parameters (all overridable via launch/config) ----
        self.declare_parameter("robot_id", "go2_01")
        self.declare_parameter("mic_mode", "host_mic")     # host_mic|file|silence
        self.declare_parameter("mic_file", "")
        self.declare_parameter("mic_sample_rate", 16000)
        self.declare_parameter("mic_channels", 1)
        self.declare_parameter("mic_chunk_ms", 100)
        self.declare_parameter("mic_device", "")
        self.declare_parameter("speaker_enabled", True)
        self.declare_parameter("speaker_device", "")
        self.declare_parameter("log_throughput_every", 50)

        g = self.get_parameter
        self.robot_id = g("robot_id").value
        self.mic_mode = g("mic_mode").value
        self.mic_file = g("mic_file").value
        self.mic_sample_rate = int(g("mic_sample_rate").value)
        self.mic_channels = int(g("mic_channels").value)
        self.mic_chunk_ms = int(g("mic_chunk_ms").value)
        self.mic_device = g("mic_device").value
        self.speaker_enabled = bool(g("speaker_enabled").value)
        self.speaker_device = g("speaker_device").value
        self.log_every = max(1, int(g("log_throughput_every").value))

        if self.mic_mode not in ("host_mic", "file", "silence"):
            raise RuntimeError(
                f"mic_mode must be host_mic|file|silence, got '{self.mic_mode}'")

        self.chunk_samples = int(self.mic_sample_rate * self.mic_chunk_ms / 1000)
        if self.chunk_samples <= 0:
            raise RuntimeError("mic_sample_rate * mic_chunk_ms / 1000 must be > 0")

        # ---- ROS I/O ----
        self._mic_pub = self.create_publisher(
            AudioChunk, "audio/mic_raw", qos_profile_sensor_data)
        self._spk_sub = self.create_subscription(
            AudioChunk, "audio/speaker_out", self._on_speaker,
            qos_profile_sensor_data)

        # ---- mic state ----
        self._mic_stream: Optional[sd.InputStream] = None
        self._mic_timer = None              # drives file/silence cadence
        self._mic_in_fallback = False       # host_mic failed -> publishing silence
        self._fallback_since = 0.0
        self._mic_overflows = 0
        self._mic_count = 0
        self._mic_log_t0 = time.monotonic()
        self._mic_log_n0 = 0
        self._file_pcm: Optional[np.ndarray] = None
        self._file_pos = 0
        self._mic_cb_failed = False

        # ---- speaker state ----
        self._spk_stream: Optional[sd.OutputStream] = None
        self._spk_rate = self.mic_sample_rate
        self._spk_channels = 1
        self._spk_open_failed_at = 0.0
        self._spk_reopens = 0
        self._spk_count = 0
        self._spk_log_t0 = time.monotonic()
        self._spk_log_n0 = 0

        # ---- bring up ----
        self._start_mic()
        if self.speaker_enabled:
            self._open_speaker(self._spk_rate, self._spk_channels)
        # Watchdog: retry a dead mic device, nothing else.
        self._watchdog = self.create_timer(5.0, self._tick_watchdog)

        self.get_logger().info(
            f"audio_bridge ready — robot_id={self.robot_id} "
            f"mic_mode={self.mic_mode} "
            f"{self.mic_sample_rate}Hz/{self.mic_channels}ch/"
            f"{self.mic_chunk_ms}ms ({self.chunk_samples} samples) "
            f"speaker_enabled={self.speaker_enabled}")

    # ------------------------------------------------------------------
    # Mic side
    # ------------------------------------------------------------------
    def _start_mic(self) -> None:
        if self.mic_mode == "file":
            self._load_mic_file()
            self._mic_timer = self.create_timer(
                self.mic_chunk_ms / 1000.0, self._tick_file)
            self.get_logger().info(f"mic: looping file {self.mic_file}")
        elif self.mic_mode == "silence":
            self._mic_timer = self.create_timer(
                self.mic_chunk_ms / 1000.0, self._tick_silence)
            self.get_logger().info("mic: publishing silence")
        else:  # host_mic
            if not self._open_input_stream():
                self._enter_mic_fallback("could not open host mic at startup")

    def _load_mic_file(self) -> None:
        """Load + resample the WAV for file mode. Missing file is fatal —
        that is a configuration error, not a runtime hiccup."""
        if not self.mic_file:
            raise RuntimeError("mic_mode=file requires mic_file to be set")
        import os
        if not os.path.isfile(self.mic_file):
            raise RuntimeError(f"mic_file does not exist: {self.mic_file}")

        data, sr = sf.read(self.mic_file, dtype="float32", always_2d=True)
        mono = data.mean(axis=1)  # downmix to mono
        if sr != self.mic_sample_rate:
            d = gcd(int(sr), int(self.mic_sample_rate))
            mono = resample_poly(mono, self.mic_sample_rate // d, int(sr) // d)
        pcm = np.clip(mono * 32767.0, -32768, 32767).astype("<i2")
        if pcm.size < self.chunk_samples:
            reps = self.chunk_samples // max(1, pcm.size) + 1
            pcm = np.tile(pcm, reps)
        self._file_pcm = pcm
        self._file_pos = 0

    def _open_input_stream(self) -> bool:
        try:
            stream = sd.InputStream(
                samplerate=self.mic_sample_rate,
                channels=self.mic_channels,
                dtype="int16",
                blocksize=self.chunk_samples,
                device=_device_arg(self.mic_device),
                callback=self._mic_callback,
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001 - PortAudio raises broad types
            self.get_logger().error(f"mic: InputStream open failed: {exc!r}")
            return False
        self._mic_stream = stream
        self._mic_cb_failed = False
        self._mic_log_t0 = time.monotonic()
        self._mic_log_n0 = self._mic_count
        backend, name = self._backend_of(stream, "input")
        self.get_logger().info(f"mic: capturing — {backend} | device='{name}'")
        return True

    def _mic_callback(self, indata, frames, time_info, status) -> None:
        """PortAudio thread. Keep it short; never raise out of here or the
        stream aborts."""
        try:
            if status and status.input_overflow:
                self._mic_overflows += 1
            self._publish_audio(bytes(indata), self.mic_channels, kind="mic")
        except Exception as exc:  # noqa: BLE001
            if not self._mic_cb_failed:
                self._mic_cb_failed = True
                self.get_logger().error(f"mic: callback error: {exc!r}")

    def _tick_file(self) -> None:
        buf = self._file_pcm
        n = self.chunk_samples
        end = self._file_pos + n
        if end <= buf.size:
            chunk = buf[self._file_pos:end]
        else:
            chunk = np.concatenate([buf[self._file_pos:], buf[:end - buf.size]])
        self._file_pos = end % buf.size
        self._publish_audio(chunk.tobytes(), 1, kind="mic")

    def _tick_silence(self) -> None:
        zeros = bytes(self.chunk_samples * self.mic_channels * 2)
        self._publish_audio(zeros, self.mic_channels, kind="mic")

    def _enter_mic_fallback(self, reason: str) -> None:
        """host_mic failed: keep the topic alive with silence, retry later.
        The topic must NOT go dark or downstream subscribers stall."""
        if self._mic_in_fallback:
            return
        self._mic_in_fallback = True
        self._fallback_since = time.monotonic()
        self.get_logger().error(
            f"mic: {reason} — falling back to silence, retry in "
            f"{MIC_RETRY_COOLDOWN_S:.0f}s")
        if self._mic_timer is None:
            self._mic_timer = self.create_timer(
                self.mic_chunk_ms / 1000.0, self._tick_silence)

    def _tick_watchdog(self) -> None:
        if not (self.mic_mode == "host_mic" and self._mic_in_fallback):
            return
        if time.monotonic() - self._fallback_since < MIC_RETRY_COOLDOWN_S:
            return
        self.get_logger().info("mic: retrying host mic device...")
        if self._open_input_stream():
            if self._mic_timer is not None:
                self.destroy_timer(self._mic_timer)
                self._mic_timer = None
            self._mic_in_fallback = False
            self.get_logger().info("mic: host mic recovered")
        else:
            self._fallback_since = time.monotonic()

    # ------------------------------------------------------------------
    # Speaker side
    # ------------------------------------------------------------------
    def _open_speaker(self, rate: int, channels: int) -> bool:
        if self._spk_stream is not None:
            try:
                self._spk_stream.stop()
                self._spk_stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._spk_stream = None
        try:
            stream = sd.OutputStream(
                samplerate=rate,
                channels=channels,
                dtype="int16",
                device=_device_arg(self.speaker_device),
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001
            self.get_logger().error(f"speaker: OutputStream open failed: {exc!r}")
            self._spk_open_failed_at = time.monotonic()
            return False
        self._spk_stream = stream
        self._spk_rate, self._spk_channels = rate, channels
        self._spk_log_t0 = time.monotonic()
        self._spk_log_n0 = self._spk_count
        backend, name = self._backend_of(stream, "output")
        self.get_logger().info(
            f"speaker: playing — {backend} | device='{name}' | "
            f"{rate}Hz/{channels}ch")
        return True

    def _on_speaker(self, msg: AudioChunk) -> None:
        if not self.speaker_enabled:
            return  # subscribed-and-discard: headless / file-input tests
        rate = int(msg.sample_rate)
        channels = int(msg.channels) or 1

        if self._spk_stream is None:
            # Recover from an earlier open failure, but not faster than the
            # mic retry cadence — a missing sink does not fix itself instantly.
            if time.monotonic() - self._spk_open_failed_at < MIC_RETRY_COOLDOWN_S:
                return
            if not self._open_speaker(rate, channels):
                return
        elif rate != self._spk_rate or channels != self._spk_channels:
            self.get_logger().warning(
                f"speaker: format change "
                f"{self._spk_rate}Hz/{self._spk_channels}ch -> "
                f"{rate}Hz/{channels}ch — reopening stream")
            self._spk_reopens += 1
            if not self._open_speaker(rate, channels):
                return

        raw = bytes(msg.data)
        if len(raw) % 2:
            self.get_logger().warning("speaker: odd-length PCM payload, dropping")
            return
        samples = np.frombuffer(raw, dtype="<i2")
        if channels > 1:
            samples = samples.reshape(-1, channels)
        try:
            self._spk_stream.write(samples)
        except Exception as exc:  # noqa: BLE001
            self.get_logger().error(f"speaker: write failed: {exc!r} — reopening")
            try:
                self._spk_stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._spk_stream = None
            self._spk_open_failed_at = time.monotonic()
            return

        self._spk_count += 1
        if self._spk_count % self.log_every == 0:
            now = time.monotonic()
            dt = now - self._spk_log_t0
            rate_hz = (self._spk_count - self._spk_log_n0) / dt if dt > 0 else 0.0
            backend, name = self._backend_of(self._spk_stream, "output")
            self.get_logger().info(
                f"speaker: {rate_hz:5.1f} Hz | {backend} | device='{name}' | "
                f"reopens={self._spk_reopens}")
            self._spk_log_t0, self._spk_log_n0 = now, self._spk_count

    # ------------------------------------------------------------------
    # Shared
    # ------------------------------------------------------------------
    def _publish_audio(self, data: bytes, channels: int, kind: str) -> None:
        msg = AudioChunk()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.robot_id
        msg.sample_rate = self.mic_sample_rate
        msg.channels = channels
        msg.sample_width = 2  # int16
        msg.data = data
        self._mic_pub.publish(msg)

        self._mic_count += 1
        if self._mic_count % self.log_every == 0:
            now = time.monotonic()
            dt = now - self._mic_log_t0
            rate_hz = (self._mic_count - self._mic_log_n0) / dt if dt > 0 else 0.0
            src = "silence" if self._mic_in_fallback else self.mic_mode
            if self._mic_stream is not None:
                backend, name = self._backend_of(self._mic_stream, "input")
            else:
                backend, name = "timer", src
            self.get_logger().info(
                f"mic: {rate_hz:5.1f} Hz | {backend} | device='{name}' | "
                f"overflows={self._mic_overflows}")
            self._mic_log_t0, self._mic_log_n0 = now, self._mic_count

    @staticmethod
    def _backend_of(stream, direction: str) -> Tuple[str, str]:
        """('sounddevice/<hostapi>', '<device name>') for a health line."""
        try:
            dev = stream.device
            info = sd.query_devices(dev, direction)
            hostapi = sd.query_hostapis(info["hostapi"])["name"]
            return f"sounddevice/{hostapi}", info["name"]
        except Exception:  # noqa: BLE001
            return "sounddevice/?", "?"

    def shutdown(self) -> None:
        """Explicit teardown. The audio streams and the rclpy node both own
        native resources — closing the streams BEFORE the node/context goes
        away avoids the interpreter exiting on a SIGSEGV."""
        for timer in (getattr(self, "_watchdog", None),
                      getattr(self, "_mic_timer", None)):
            if timer is not None:
                try:
                    self.destroy_timer(timer)
                except Exception:  # noqa: BLE001
                    pass
        for stream in (self._mic_stream, self._spk_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
        self._mic_stream = None
        self._spk_stream = None


def main(args=None):
    rclpy.init(args=args)
    node = AudioBridge()
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
