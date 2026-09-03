#!/usr/bin/env bash
set -euo pipefail

fail() { echo "ERROR: $*" >&2; exit 1; }

ROBOT_ID="${ROBOT_ID:-go2_02}"
FEROX_DDS_INTERFACE="${FEROX_DDS_INTERFACE:-eth0}"
DURATION_S="${GO2_AUDIO_DISCOVERY_SECONDS:-15}"
OUTPUT="${1:-}"
FRAMES_OUTPUT="${2:-}"

[[ "${ROBOT_ID}" =~ ^go2_[0-9]{2}$ ]] || fail "ROBOT_ID must look like go2_02"
[[ "${DURATION_S}" =~ ^[0-9]+$ ]] || fail "GO2_AUDIO_DISCOVERY_SECONDS must be an integer"
(( DURATION_S >= 10 && DURATION_S <= 120 )) || fail "discovery duration must be 10-120 s"
[[ -n "${OUTPUT}" && -n "${FRAMES_OUTPUT}" ]] || fail "usage: scripts/go2_audio_discover.sh /absolute/new-observation.json /absolute/new-frames.jsonl"
[[ "${OUTPUT}" = /* ]] || fail "observation output must be an absolute path"
[[ "${FRAMES_OUTPUT}" = /* ]] || fail "frames output must be an absolute path"
[[ ! -e "${OUTPUT}" ]] || fail "output already exists; refusing overwrite"
[[ ! -e "${FRAMES_OUTPUT}" ]] || fail "frames output already exists; refusing overwrite"
command -v ros2 >/dev/null 2>&1 || fail "ros2 is not on PATH"
ip link show "${FEROX_DDS_INTERFACE}" >/dev/null 2>&1 || fail "DDS interface does not exist"

export ROS_DOMAIN_ID=0
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export FEROX_DDS_INTERFACE

echo "Read-only Go2 audio discovery: robot=${ROBOT_ID} iface=${FEROX_DDS_INTERFACE} duration=${DURATION_S}s"
echo "This only subscribes to /audiosender; it does not publish audio or call robot APIs."
ros2 run ferox_audio_go2 go2_audio_readonly_discovery \
  --duration-s "${DURATION_S}" --output "${OUTPUT}" \
  --frames-output "${FRAMES_OUTPUT}"
sha256sum "${OUTPUT}"
sha256sum "${FRAMES_OUTPUT}"
