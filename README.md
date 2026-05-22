# ferox-audio-sim

Host-side audio bridge. The first implementation of the Ferox **audio
topic contract** — the abstraction that lets `ferox-speech` stay completely
audio-device-agnostic (it never touches `/dev/snd`, never sees PulseAudio).

This is a **standalone repo** — it does not depend on Ferox. It runs on a
**host Ubuntu laptop**, not the compute box: it captures the laptop's
microphone and plays to its speaker, bridging both directions to ROS 2
topics so `ferox-speech` (running on Vast.ai / a Tailscale DGX) can do
speech I/O over DDS as if the laptop were a robot.

Two packages:

- **`ferox_audio_msgs`** — the `AudioChunk` message, on its own so any
  repo can depend on just the interface.
- **`ferox_audio_sim`** — the `audio_bridge` node that does the I/O.

## The topic contract

Every audio backend — `ferox_audio_sim` today, `ferox_audio_go2` /
`ferox_audio_g1` on real hardware tomorrow — implements exactly this:

| Topic                                 | Dir        | Type                              | QoS                   |
|----------------------------------------|------------|-----------------------------------|-----------------------|
| `/ferox/<robot_id>/audio/mic_raw`      | published  | `ferox_audio_msgs/msg/AudioChunk` | BEST_EFFORT, depth 10 |
| `/ferox/<robot_id>/audio/speaker_out`  | subscribed | `ferox_audio_msgs/msg/AudioChunk` | BEST_EFFORT, depth 10 |

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
from ferox_audio_msgs.msg import AudioChunk
rclpy.init()
n = Node(\"echo\")
p = n.create_publisher(AudioChunk, \"/ferox/go2_01/audio/speaker_out\", 10)
n.create_subscription(AudioChunk, \"/ferox/go2_01/audio/mic_raw\", lambda m: p.publish(m), 10)
rclpy.spin(n)
"'
```

## Consuming the AudioChunk message from another repo

`ferox-speech` and the future `ferox_audio_go2` / `ferox_audio_g1` drivers
only need the message, not the bridge node. Add `ferox_audio_msgs` to the
consumer's colcon workspace and declare the dependency — two lines:

```xml
<!-- consumer package.xml -->
<depend>ferox_audio_msgs</depend>
```

```bash
# bring ferox_audio_msgs into the consumer's workspace, e.g.:
ln -s ~/panthera/ferox-audio-sim/src/ferox_audio_msgs  <consumer_ws>/src/
colcon build --packages-select ferox_audio_msgs <consumer_pkg>
```

```python
# consumer code
from ferox_audio_msgs.msg import AudioChunk
```

`ferox_audio_msgs` is a tiny, dependency-light rosidl package precisely so
it can be vendored this way without dragging in the bridge or Ferox.

## Real-hardware counterparts

`ferox_audio_go2` and `ferox_audio_g1` will implement this exact topic
contract against the Go2 / G1 on-robot audio devices. They will live in
the respective driver repos and ship when hardware arrives. `ferox-speech`
will not change between sim and hardware — only the audio backend swaps.

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
