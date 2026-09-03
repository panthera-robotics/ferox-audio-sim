#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 IMAGE EVIDENCE_ROOT DURATION_SECONDS" >&2
  exit 64
fi

image="$1"
evidence_root="$2"
duration_seconds="$3"
evidence_base="/home/unitree/ferox-evidence"

if [[ ! "${duration_seconds}" =~ ^([5-9]|[1-9][0-9]|1[01][0-9]|120)$ ]]; then
  echo "DURATION_SECONDS must be an integer in [5, 120]" >&2
  exit 64
fi
evidence_name="$(basename -- "${evidence_root}")"
if [[
  ! -d "${evidence_base}"
  || -L "${evidence_base}"
  || "${evidence_root}" != "${evidence_base}/${evidence_name}"
  || ! "${evidence_name}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$
]]; then
  echo "EVIDENCE_ROOT must be one safe direct child of ${evidence_base}" >&2
  exit 64
fi
if [[ -e "${evidence_root}" ]]; then
  echo "EVIDENCE_ROOT already exists; refusing overwrite" >&2
  exit 73
fi
if ! docker image inspect "${image}" >/dev/null 2>&1; then
  echo "image is not present: ${image}" >&2
  exit 66
fi

mkdir -m 0750 "${evidence_root}"
mkdir -m 0750 "${evidence_root}/reliable" "${evidence_root}/best_effort"
sha256sum "$0" >"${evidence_root}/runner-sha256.txt"
{
  printf 'hostname='
  hostname
  printf 'user='
  id -un
  printf 'kernel='
  uname -r
  printf 'architecture='
  uname -m
  printf 'uptime_seconds='
  cut -d' ' -f1 /proc/uptime
} >"${evidence_root}/host-preflight.txt"
host_uid="$(id -u)"
host_gid="$(id -g)"
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
reliable_name="ferox-native-timing-reliable-${run_id}"
best_effort_name="ferox-native-timing-best-effort-${run_id}"

cleanup() {
  docker stop --time 2 "${reliable_name}" "${best_effort_name}" >/dev/null 2>&1 || true
  docker rm --force "${reliable_name}" "${best_effort_name}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

common=(
  docker run --detach
  --network host
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges:true
  --pids-limit 64
  --user "${host_uid}:${host_gid}"
  --tmpfs "/tmp:rw,nosuid,nodev,noexec,uid=${host_uid},gid=${host_gid},size=16m"
  --tmpfs "/home/panthera/.ros:rw,nosuid,nodev,noexec,uid=${host_uid},gid=${host_gid},size=8m"
  --env HOME=/home/panthera
  --env RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  --env ROS_DOMAIN_ID=0
  --env FEROX_DDS_INTERFACE=eth0
  --env FEROX_DDS_PEERS=
  --env CYCLONEDDS_URI=file:///tmp/cyclonedds.xml
)

probe_command=(
  /bin/bash -lc
  'source /opt/ros/humble/setup.bash; source /opt/ferox_msgs_ws/install/setup.bash; source /unitree_ws/install/setup.bash; source /workspace/install/setup.bash; exec /workspace/install/lib/ferox_audio_go2_native/go2_audio_native_timing_probe "$@"'
  ferox-native-probe
)

"${common[@]}" --name "${reliable_name}" \
  --mount "type=bind,source=${evidence_root}/reliable,target=/evidence" \
  "${image}" "${probe_command[@]}" \
  --duration-s "${duration_seconds}" \
  --qos-reliability reliable \
  --frames-output /evidence/frames.jsonl \
  --metadata-output /evidence/metadata.jsonl \
  --ros-args --remap __node:=go2_audio_native_timing_reliable \
  >"${evidence_root}/reliable.container-id"

"${common[@]}" --name "${best_effort_name}" \
  --mount "type=bind,source=${evidence_root}/best_effort,target=/evidence" \
  "${image}" "${probe_command[@]}" \
  --duration-s "${duration_seconds}" \
  --qos-reliability best_effort \
  --frames-output /evidence/frames.jsonl \
  --metadata-output /evidence/metadata.jsonl \
  --ros-args --remap __node:=go2_audio_native_timing_best_effort \
  >"${evidence_root}/best_effort.container-id"

sleep 5
docker inspect "${reliable_name}" >"${evidence_root}/reliable.container-running.json"
docker inspect "${best_effort_name}" >"${evidence_root}/best_effort.container-running.json"
docker ps --no-trunc >"${evidence_root}/docker-ps-during.txt"

reliable_exit="$(docker wait "${reliable_name}")"
best_effort_exit="$(docker wait "${best_effort_name}")"
docker logs "${reliable_name}" >"${evidence_root}/reliable.stdout.log" 2>"${evidence_root}/reliable.stderr.log"
docker logs "${best_effort_name}" >"${evidence_root}/best_effort.stdout.log" 2>"${evidence_root}/best_effort.stderr.log"
docker inspect "${reliable_name}" >"${evidence_root}/reliable.container-final.json"
docker inspect "${best_effort_name}" >"${evidence_root}/best_effort.container-final.json"
printf '%s\n' "${reliable_exit}" >"${evidence_root}/reliable.exit-code"
printf '%s\n' "${best_effort_exit}" >"${evidence_root}/best_effort.exit-code"

if [[ "${reliable_exit}" != 0 || "${best_effort_exit}" != 0 ]]; then
  echo "native probe failed: reliable=${reliable_exit} best_effort=${best_effort_exit}" >&2
  exit 1
fi
for required in \
  "${evidence_root}/reliable/frames.jsonl" \
  "${evidence_root}/reliable/metadata.jsonl" \
  "${evidence_root}/best_effort/frames.jsonl" \
  "${evidence_root}/best_effort/metadata.jsonl"; do
  if [[ ! -s "${required}" ]]; then
    echo "required evidence is empty: ${required}" >&2
    exit 1
  fi
done

docker image inspect "${image}" >"${evidence_root}/image-inspect.json"
sha256sum "${evidence_root}"/reliable/* "${evidence_root}"/best_effort/* \
  "${evidence_root}"/*.json "${evidence_root}"/*.log "${evidence_root}"/*.txt \
  >"${evidence_root}/sha256sums.txt"
chmod -R a-w "${evidence_root}"
echo "native timing A/B capture completed: ${evidence_root}"
