# ferox-audio-sim — repo guide

## Repo role

Audio backends for the shared Ferox ROS 2 topic contract. The simulation
backend runs on a host Ubuntu laptop; G1 and Go2 packages adapt hardware APIs
without exposing audio devices to `ferox-speech`.

Core packages, one repo:
- `ferox_msgs` — the external shared `AudioChunk` interface
- `ferox_audio_sim` — the `audio_bridge` rclpy node (ament_python)
- `ferox_audio_g1` — G1 voice adapter and evidence probes
- `ferox_audio_go2` — evidence-gated Go2 mic/speaker adapter

## Topic contract

Every audio backend implements this identically:

- publishes  `/ferox/<robot_id>/audio/mic_raw`     — captured mic PCM
- subscribes `/ferox/<robot_id>/audio/speaker_out` — PCM to play

Message: `ferox_msgs/msg/AudioChunk` (versioned stream metadata, PCM format,
and data). Topic names are RELATIVE in each bridge node; the
`/ferox/<robot_id>/` namespace is applied by the launch file's
`PushRosNamespace`, never hardcoded.

## Topic contract (QoS)

Both audio topics use `qos_profile_sensor_data` (BEST_EFFORT, KEEP_LAST,
depth 5). Any consumer that doesn't match this profile will silently
fail with a DDS "incompatible QoS" warning. Use the import path
`from rclpy.qos import qos_profile_sensor_data` — do not construct a
custom equivalent.

## Standing tech conventions

ROS 2 Humble + Cyclone DDS + `ROS_DOMAIN_ID=42` + `--network host` —
everything must match the rest of the Panthera stack or DDS discovery
silently fails. Every runtime dependency goes in `docker/Dockerfile`;
nothing is installed live in a running container.

## Cross-machine DDS env-var contract

The container expects `FEROX_DDS_PEER_HOST` and `FEROX_DDS_PEER_CLOUD`
as env at start — Tailscale IPs of the host and the compute peer. Missing
either → fail-loud (prevents accidental multicast fallback on a tailnet,
which silently produces a DDS mesh nobody else can see). The template
`config/cyclonedds.xml.template` + `docker/entrypoint-dds.sh` render
`/tmp/cyclonedds.xml` via `envsubst` and export `CYCLONEDDS_URI` before
`exec`'ing the original `/entrypoint.sh`. `scripts/start.sh` sources
`.env` (gitignored, see `.env.example`) and passes both vars through
`docker run -e`. Same two values must be set on the ferox-speech side
for the peer list to match.

## Working with the running container

Interactive scripts run via `docker exec` MUST use `-it` (or at least
`-t`) so SIGINT forwards correctly. Without it, Ctrl+C in the host
terminal does not reach the inner process and the container-side
script keeps running. See `scripts/test_echo.sh` for the canonical
pattern.

## Boundaries

This repo does **NOT** depend on the Ferox application or ferox-speech. Its
backends and downstream consumers depend on the small external `ferox_msgs`
interface package, never the other way around. Go2 hardware I/O remains
disabled until a robot/firmware-specific, SHA-pinned evidence manifest passes.

## Commit style

No `feat:` / `fix:` prefixes. Present tense. One-line subject + a short
body explaining what changed and why. Lowercase subject.
