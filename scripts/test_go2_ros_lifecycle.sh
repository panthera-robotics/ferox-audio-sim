#!/usr/bin/env bash
set -euo pipefail
# Local synthetic test only: never use host networking or the image entrypoint.
task_audio_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
task_audio_image="sha256:3fd34f3220bebe737e6a14d3f3bc4b4193c3dba325383e3ae33046c241092a25"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--negative-control" ) ]]; then
  echo 'usage: bash scripts/test_go2_ros_lifecycle.sh [--negative-control]' >&2
  exit 2
fi
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 128 --memory 1g --cpus 2 \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m \
  -e CYCLONEDDS_URI= -e PYTHONDONTWRITEBYTECODE=1 \
  -e OFFLINE_TEST_IMAGE="$task_audio_image" \
  -e OFFLINE_TEST_REVISION="$(git -C "$task_audio_root" rev-parse HEAD)" \
  -v "$task_audio_root:/candidate:ro" -w /candidate \
  --entrypoint /bin/bash "$task_audio_image" -c '
    source /opt/ros/humble/setup.bash &&
    source /opt/ferox_msgs_ws/install/setup.bash &&
    source /unitree_ws/install/setup.bash &&
    export PYTHONPATH=/candidate/src/ferox_audio_go2:$PYTHONPATH &&
    timeout --kill-after=5s 45s python3 src/ferox_audio_go2/test/ros_subscriber_lifecycle.py "$@"
  ' bash "$@"
