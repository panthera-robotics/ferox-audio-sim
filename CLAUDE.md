# ferox-audio-sim — repo guide

## Repo role

Host-side audio bridge between `sounddevice` (PortAudio mic/speaker) and
ROS 2 topics. **Standalone — not part of Ferox.** It runs on a host
Ubuntu laptop, in a Docker container, and lets `ferox-speech` do speech
I/O over DDS without ever touching `/dev/snd` or PulseAudio.

Two packages, one repo:
- `ferox_audio_msgs` — the `AudioChunk` message only (ament_cmake/rosidl)
- `ferox_audio_sim` — the `audio_bridge` rclpy node (ament_python)

## Topic contract

Every audio backend implements this — `ferox_audio_sim` here, and the
future `ferox_audio_go2` / `ferox_audio_g1` hardware drivers identically:

- publishes  `/ferox/<robot_id>/audio/mic_raw`     — captured mic PCM
- subscribes `/ferox/<robot_id>/audio/speaker_out` — PCM to play

Message: `ferox_audio_msgs/msg/AudioChunk` (sample_rate, channels,
sample_width, data). QoS: BEST_EFFORT, depth 10. Topic names are RELATIVE
in the node; the `/ferox/<robot_id>/` namespace is applied by the launch
file's `PushRosNamespace`, never hardcoded.

## Standing tech conventions

ROS 2 Humble + Cyclone DDS + `ROS_DOMAIN_ID=42` + `--network host` —
everything must match the rest of the Panthera stack or DDS discovery
silently fails. Every runtime dependency goes in `docker/Dockerfile`;
nothing is installed live in a running container.

## Boundaries

This repo does **NOT** depend on Ferox or ferox-speech. Downstream repos
(ferox-speech, ferox_audio_go2/_g1) depend on `ferox_audio_msgs` as a
small external package — never the other way around.

## Commit style

No `feat:` / `fix:` prefixes. Present tense. One-line subject + a short
body explaining what changed and why. Lowercase subject.
