# ============================================================================
# ferox-audio-sim — host-side audio bridge.
#
# Runs on a host Ubuntu laptop (NOT the compute box). Captures the host mic
# into /ferox/<robot_id>/audio/mic_raw and plays /ferox/<robot_id>/audio/
# speaker_out through the host speaker, over Cyclone DDS on ROS_DOMAIN_ID 42.
#
# Multi-stage: stage 1 builds ferox_audio_msgs + ferox_audio_sim, stage 2 is a
# slim runtime that only carries the install/ tree plus the audio + DDS libs.
#
# Build from the repo root:
#   docker build -f docker/Dockerfile -t ferox/audio_sim:humble .
# ============================================================================

ARG ROS_DISTRO=humble

# ---------------------------------------------------------------------------
# Stage 1 — builder: colcon build the two packages into a merged install tree.
# ---------------------------------------------------------------------------
FROM ros:${ROS_DISTRO}-ros-base AS builder

ARG ROS_DISTRO=humble
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      python3-colcon-common-extensions \
      ros-${ROS_DISTRO}-ament-cmake \
      ros-${ROS_DISTRO}-rosidl-default-generators \
      ros-${ROS_DISTRO}-rosidl-default-runtime \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ws
COPY src/ferox_audio_msgs  ./src/ferox_audio_msgs
COPY src/ferox_audio_sim   ./src/ferox_audio_sim

RUN . /opt/ros/${ROS_DISTRO}/setup.sh \
 && colcon build --merge-install \
      --packages-select ferox_audio_msgs ferox_audio_sim

# ---------------------------------------------------------------------------
# Stage 2 — runtime: slim image, install tree copied in from the builder.
# ---------------------------------------------------------------------------
FROM ros:${ROS_DISTRO}-ros-base

ARG ROS_DISTRO=humble
ENV DEBIAN_FRONTEND=noninteractive
ENV ROS_DISTRO=${ROS_DISTRO}
ENV PYTHONDONTWRITEBYTECODE=1
ENV RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
ENV ROS_DOMAIN_ID=42

# ---- audio + DDS runtime libraries ----
#   libasound2-dev / libportaudio2 : PortAudio backend for sounddevice
#   libasound2-plugins             : ALSA<->PulseAudio bridge (the 'pulse' PCM)
#   libpulse0 / pulseaudio-utils   : Pulse client lib + pactl for debugging
#   rmw-cyclonedds-cpp             : DDS implementation — must match the stack
#   launch / launch-ros            : included defensively (already in ros-base)
RUN apt-get update && apt-get install -y --no-install-recommends \
      libasound2-dev \
      libasound2-plugins \
      libportaudio2 \
      libpulse0 \
      pulseaudio-utils \
      python3-pip \
      python3-numpy \
      python3-scipy \
      ros-${ROS_DISTRO}-rmw-cyclonedds-cpp \
      ros-${ROS_DISTRO}-launch \
      ros-${ROS_DISTRO}-launch-ros \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir sounddevice soundfile

# Route ALSA's default PCM to the PulseAudio/PipeWire server. Ubuntu desktops
# own the mic through Pulse; without this the container can see /dev/snd but
# only as raw hw: devices it cannot open at an arbitrary sample rate.
RUN printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' \
      > /etc/asound.conf

# Carry only the built workspace — no source, no build tools.
COPY --from=builder /ws/install /workspace/install

# docker exec does NOT run the entrypoint, so bake the ROS + workspace
# environment onto the image. Without this, `docker exec ... ros2 ...` and
# the python3 validation snippets cannot resolve ros2 / ferox_audio_msgs.
ENV AMENT_PREFIX_PATH=/workspace/install:/opt/ros/humble
ENV LD_LIBRARY_PATH=/workspace/install/lib:/opt/ros/humble/lib/x86_64-linux-gnu:/opt/ros/humble/lib
ENV PYTHONPATH=/workspace/install/local/lib/python3.10/dist-packages:/workspace/install/lib/python3.10/site-packages:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages
ENV PATH=/workspace/install/bin:/opt/ros/humble/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Entrypoint: source ROS + workspace, then launch the bridge. HOME is forced
# to a writable path because the container runs as a host UID with no
# /etc/passwd entry (ros2 wants somewhere to put its logs).
RUN printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -e' \
  'source /opt/ros/humble/setup.bash' \
  'source /workspace/install/setup.bash' \
  'export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp' \
  'export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-42}' \
  'export HOME=${HOME:-/tmp}' \
  'exec ros2 launch ferox_audio_sim audio_bridge.launch.py "$@"' \
  > /entrypoint.sh \
 && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
