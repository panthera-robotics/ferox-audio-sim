# ferox-audio-sim

Host-side audio bridge. The first implementation of the Ferox **audio
topic contract** — the abstraction that lets `ferox-speech` stay completely
audio-device-agnostic (it never touches `/dev/snd`, never sees PulseAudio).

This is a **standalone repo** — it does not depend on Ferox. It runs on a
**host Ubuntu laptop**, not the compute box: it captures the laptop's
microphone and plays to its speaker, bridging both directions to ROS 2
topics so `ferox-speech` (running on Vast.ai / a Tailscale DGX) can do
speech I/O over DDS as if the laptop were a robot.

Core packages:

- **`ferox_msgs`** — the shared `AudioChunk` interface, consumed as an
  external, dependency-light ROS package.
- **`ferox_audio_sim`** — the `audio_bridge` node that does the I/O.
- **`ferox_audio_g1`** — the G1 voice adapter.
- **`ferox_audio_go2`** — the evidence-gated Go2 hardware adapter.

## The topic contract

Every audio backend — `ferox_audio_sim` today, `ferox_audio_go2` /
`ferox_audio_g1` on real hardware tomorrow — implements exactly this:

| Topic                                 | Dir        | Type                              | QoS                   |
|----------------------------------------|------------|-----------------------------------|-----------------------|
| `/ferox/<robot_id>/audio/mic_raw`      | published  | `ferox_msgs/msg/AudioChunk` | BEST_EFFORT, depth 5 |
| `/ferox/<robot_id>/audio/speaker_out`  | subscribed | `ferox_msgs/msg/AudioChunk` | BEST_EFFORT, depth 5 |

- The driver **publishes** mic frames on `audio/mic_raw`.
- The driver **subscribes** `audio/speaker_out` and plays the frames.
- `AudioChunk` carries `sample_rate`, `channels`, `sample_width`, and raw
  little-endian PCM `data`. Default stream: 100 ms int16 mono chunks at
  16 kHz → ~10 Hz on `mic_raw`, 3200 bytes/chunk.
- BEST_EFFORT QoS: audio is real-time, a dropped frame is recoverable,
  RELIABLE would only buffer-and-lag.

Topic names are relative in the node; the `/ferox/<robot_id>/` namespace is
applied by the launch file's `PushRosNamespace`.

## Run modes (`mic_mode` parameter)

- `host_mic` — capture the host's default input device (default).
- `file` — loop a WAV file (`mic_file`), resampled to `mic_sample_rate`.
- `silence` — publish zero frames; keeps the topic alive for downstream
  bring-up tests.

If the host mic cannot be opened, the node logs a loud error and falls
back to `silence` (retrying every 30 s) — it never lets `mic_raw` go dark.

## Build

```bash
git clone <this-repo> ~/panthera/ferox-audio-sim
cd ~/panthera/ferox-audio-sim
./scripts/build.sh        # docker build -> ferox/audio_sim:humble
```

## Start

```bash
./scripts/start.sh                  # defaults (config/audio_bridge.yaml)
./scripts/start.sh robot_id:=g1_01  # pass-through launch args
```

The container runs `--network host`, `--device /dev/snd`, with the host
PulseAudio socket mounted, on `ROS_DOMAIN_ID=42` with Cyclone DDS — the
same DDS mesh as the rest of the stack.

## Cross-machine setup

When `ferox-speech` runs on a remote compute box (Vast.ai, a Tailscale
DGX, etc.) instead of the same laptop, DDS multicast discovery does not
traverse WAN. This repo configures **Cyclone DDS unicast over Tailscale**
so both peers find each other explicitly.

Prereqs: both machines on the same Tailscale tailnet, each with a
`tailscale0` interface up.

1. Get each machine's Tailscale IP:

   ```bash
   tailscale ip -4
   ```

2. On **this host**, copy `.env.example` to `.env` and fill in both IPs:

   ```bash
   cp .env.example .env
   # edit .env:
   #   FEROX_DDS_PEER_HOST=100.x.x.x   # this laptop's tailscale IP
   #   FEROX_DDS_PEER_CLOUD=100.y.y.y  # remote compute's tailscale IP
   ```

   Put the **same two values** in ferox-speech's `.env` on the compute
   side — the peer list has to match on both ends.

3. Rebuild and restart:

   ```bash
   ./scripts/build.sh
   ./scripts/start.sh
   ```

4. Confirm the rendered DDS config in the container logs:

   ```bash
   docker logs ferox_audio_sim 2>&1 | head -5
   # expect:
   #   [dds] Cyclone peers: 100.x.x.x, 100.y.y.y
   #   [dds] config: file:///tmp/cyclonedds.xml
   ```

`scripts/start.sh` sources `.env` and **fails loud** if either peer var
is missing — silent multicast fallback on a tailnet would look like
working DDS that nobody else can see. `.env` is gitignored; never commit
your tailnet IPs.

## Validate

```bash
# V1 — topics exist with the right type + QoS
docker exec ferox_audio_sim ros2 topic list | grep audio
docker exec ferox_audio_sim ros2 topic info -v /ferox/go2_01/audio/mic_raw

# V2 — mic_raw publishes at ~10 Hz
docker exec ferox_audio_sim bash -c \
  'timeout 5 ros2 topic hz /ferox/go2_01/audio/mic_raw'

# V3 — speak into the host mic, expect non-zero PCM bytes
docker exec ferox_audio_sim bash -c \
  'ros2 topic echo /ferox/go2_01/audio/mic_raw --field data --truncate-length 8' \
  | head -20

# V4 — round-trip echo: republish mic_raw onto speaker_out and listen
docker exec ferox_audio_sim bash -c 'python3 -c "
import rclpy
from rclpy.node import Node
from ferox_msgs.msg import AudioChunk
rclpy.init()
n = Node(\"echo\")
p = n.create_publisher(AudioChunk, \"/ferox/go2_01/audio/speaker_out\", 10)
n.create_subscription(AudioChunk, \"/ferox/go2_01/audio/mic_raw\", lambda m: p.publish(m), 10)
rclpy.spin(n)
"'
```

## Acoustic loopback test

Verify mic and speaker work end-to-end with a human in the loop:

    ./scripts/test_echo.sh

Speak into your headset. You should hear yourself with ~180ms delay.
Press Ctrl+C to stop cleanly.

The script uses `docker exec -it` so SIGINT forwards into the container.
Without `-t`, Ctrl+C does NOT reliably reach the Python process inside —
the echo loop will keep running after your shell returns. This is a
generic docker gotcha, not specific to this repo.

## Cleaning up a hanging test

If a `docker exec` session was started without `-t` and Ctrl+C didn't
reach the inner process:

    docker exec ferox_audio_sim pgrep -af python      # confirm it's still alive
    docker exec ferox_audio_sim pkill -f echo_test    # or just pkill -f python
    docker exec ferox_audio_sim pgrep -af python || echo "clean"

For a heavier reset, restart the container:

    docker restart ferox_audio_sim

## Consuming AudioChunk from another package

The audio topics use **BEST_EFFORT reliability** — DDS will silently
refuse to match RELIABLE subscribers, and you will see warnings like
`incompatible QoS. No messages will be received` with no further hint.

Use the ROS 2 built-in `qos_profile_sensor_data` profile for both
publishing and subscribing:

```python
from rclpy.qos import qos_profile_sensor_data
from ferox_msgs.msg import AudioChunk

# Subscribing to mic_raw
self.create_subscription(
    AudioChunk,
    "/ferox/<robot_id>/audio/mic_raw",
    self._on_audio,
    qos_profile_sensor_data,
)

# Publishing to speaker_out
self.pub = self.create_publisher(
    AudioChunk,
    "/ferox/<robot_id>/audio/speaker_out",
    qos_profile_sensor_data,
)
```

This is the canonical QoS for streaming sensor data in ROS 2 —
BEST_EFFORT, KEEP_LAST, depth 5. It matches the audio_bridge node's
configuration exactly.

Build dependency: add `<depend>ferox_msgs</depend>` to your consumer's
`package.xml`. Keep the shared `ferox_msgs` interface package available in the
consumer's colcon workspace without coupling it to a hardware bridge.

## Real-hardware counterparts

`ferox_audio_go2` and `ferox_audio_g1` implement this exact topic contract in
this repository. Their hardware paths remain disabled until their respective
runtime and evidence gates pass. `ferox-speech` does not change between sim
and hardware — only the audio backend swaps.

## Go2 hardware adapter (evidence-gated)

This repository now includes `ferox_audio_go2`. It deliberately does **not**
pretend that G1's `AudioClient.PlayStream` API exists on Go2. The official
Unitree SDK exposes a Go2 `AudioData` IDL but no Go2 `AudioClient`; independent
hardware captures also disagree about whether 160-byte `/audiosender` frames
are 8 kHz G.711 u-law or 48 kHz Opus. The adapter therefore requires a named
profile plus a recent, SHA-256-pinned, operator-confirmed evidence manifest.

Supported profile contracts:

| Profile | Microphone | Speaker |
|---|---|---|
| `go2_opus48_audiohub_v1` | Opus, 48 kHz mono, 960 samples / 160 bytes / 20 ms | observed `audiohub` 4001 + 4003 WAV upload, 22050 Hz S16_LE mono |
| `go2_ulaw8_mic_only` | G.711 u-law, 8 kHz mono, 160 samples / 160 bytes / 20 ms | disabled; no matching speaker evidence |

Both profiles publish the same versioned 16 kHz mono PCM contract on
`/ferox/<robot_id>/audio/mic_raw`. The speaker path accepts complete,
continuous 22050 Hz `AudioChunk` utterances and only uploads after `FLAG_END`;
timeouts, response errors, sequence gaps, and buffer overflow latch output
fail-closed until process restart.

First run the read-only discovery on robot domain 0:

```bash
export ROBOT_ID=go2_02 FEROX_DDS_INTERFACE=eth0
scripts/go2_audio_discover.sh \
  /absolute/new/go2-02-audio-observation.json \
  /absolute/new/go2-02-audio-frames.jsonl
```

The discovery tool only subscribes to `/audiosender`. It records frame shape,
cadence, monotonicity, and a digest, and explicitly reports
`"interpretation": "none"`; it does not guess a codec. Decode a controlled
recording with both candidates, listen to the result, run the one-shot bounded
`go2_audio_speaker_probe` under direct supervision if applicable, and fill
`src/ferox_audio_go2/evidence/go2_audio_evidence.template.json`. Review the
manifest, note its SHA-256, then provide these deployment inputs:

```bash
export FEROX_AUDIO_GO2_IMAGE='registry/ferox-audio-go2@sha256:<digest>'
export GO2_AUDIO_EVIDENCE_PATH=/absolute/reviewed/go2-02-audio-evidence.json
export GO2_AUDIO_EVIDENCE_SHA256='<sha256 of the exact manifest>'
export GO2_AUDIO_RUNTIME_FIRMWARE='<firmware fingerprint copied from Unitree App>'
export GO2_AUDIO_PROFILE=go2_opus48_audiohub_v1
export GO2_AUDIO_MIC_ENABLED=true
export GO2_AUDIO_SPEAKER_ENABLED=false   # enable only after speaker probe evidence
export FEROX_DDS_INTERFACE=eth0
docker compose -f docker/docker-compose.go2.yml up -d
```

Builds also require an immutable shared-message image reference, for example
`--build-arg FEROX_MSGS_IMAGE=registry/ferox-msgs@sha256:<digest>`; a mutable
tag is intentionally not the Go2 Dockerfile default.

The container runs the hardware bridge on domain 0 and a narrow gateway to
application domain 42. Only mic and validated diagnostics cross 0→42; only the
versioned speaker `AudioChunk` crosses 42→0. No motion/control topic or generic
DDS bridge is present.

## Observed round-trip latency

V4 round-trip echo measured on the dev laptop (USB headset, host PipeWire,
sim + speech on the same machine — intra-host DDS over shared memory):

| Stage                                          | Latency      |
|-------------------------------------------------|--------------|
| Mic chunk fill (one 100 ms chunk)               | 100 ms       |
| PortAudio input buffer                          | ~35 ms       |
| DDS loop: mic_raw publish → echo → speaker_out  | ~3 ms (1–4)  |
| PortAudio output buffer                         | ~35 ms       |
| **Total mouth-to-ear**                          | **~170–190 ms** |

The 100 ms chunk duration dominates; the DDS hop is negligible on one host.
When ferox-speech runs on a remote compute box the DDS hop grows to the
network RTT — budget ~200–400 ms for that case.
