#!/usr/bin/env bash
set -eo pipefail

source /opt/ros/humble/setup.bash
source /opt/ferox_msgs_ws/install/setup.bash
source /unitree_ws/install/setup.bash
source /workspace/install/setup.bash
set -u

robot_id="go2_02"
config_file="/workspace/install/share/ferox_audio_go2/config/go2_audio_bridge.yaml"
parameter_args=()

for argument in "$@"; do
  case "${argument}" in
    robot_id:=*)
      robot_id="${argument#robot_id:=}"
      ;;
    config_file:=*)
      config_file="${argument#config_file:=}"
      ;;
    mic_enabled:=*|speaker_enabled:=*|hardware_profile:=*|runtime_firmware:=*|evidence_path:=*|evidence_sha256:=*)
      parameter_args+=(--param "${argument}")
      ;;
    *)
      echo "ERROR: unsupported Go2 audio bridge argument: ${argument}" >&2
      exit 64
      ;;
  esac
done

if [[ ! "${robot_id}" =~ ^[a-z][a-z0-9_]{0,62}$ ]]; then
  echo "ERROR: invalid robot_id: ${robot_id}" >&2
  exit 64
fi
if [[ ! -f "${config_file}" ]]; then
  echo "ERROR: config file not found: ${config_file}" >&2
  exit 66
fi

exec ros2 run ferox_audio_go2 go2_audio_bridge --ros-args \
  --remap __node:=go2_audio_bridge \
  --remap "__ns:=/ferox/${robot_id}" \
  --params-file "${config_file}" \
  --param "robot_id:=${robot_id}" \
  "${parameter_args[@]}"
