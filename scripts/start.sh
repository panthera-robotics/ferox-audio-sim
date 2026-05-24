#!/usr/bin/env bash
# ============================================================================
# start.sh — host-side runner for the ferox-audio-sim audio bridge.
#
# Brings up the ferox/audio_sim:humble container on --network host so it joins
# the same Cyclone DDS mesh (ROS_DOMAIN_ID 42) as ferox-speech.
#
# Usage:
#   ./scripts/start.sh                 # defaults from config/audio_bridge.yaml
#   ./scripts/start.sh robot_id:=g1_01 # pass-through launch args
#
# Fail-loud preflight, container guards, fail-loud wait on a readiness marker.
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CONTAINER="ferox_audio_sim"
IMAGE="ferox/audio_sim:humble"
READY_MARKER="audio_bridge ready"
READY_TIMEOUT=30

# ---- env: source a repo-root .env if present, then fall back to defaults. ----
if [ -f "${REPO_ROOT}/.env" ]; then
    # shellcheck disable=SC1091
    set -a; . "${REPO_ROOT}/.env"; set +a
fi
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}"

# Cross-machine Cyclone DDS — both optional. Defaults to multicast on
# auto-detected interface (works host-on-LAN). Set both in .env or
# the shell when crossing a tunnel/VPN that drops multicast.
# See .env.example for setup scenarios.
FEROX_DDS_INTERFACE="${FEROX_DDS_INTERFACE:-}"
FEROX_DDS_PEERS="${FEROX_DDS_PEERS:-}"
UID_N="$(id -u)"
GID_N="$(id -g)"
PULSE_DIR="/run/user/${UID_N}/pulse"

fail() { echo "ERROR: $*" >&2; exit 1; }

# ---- preflight ------------------------------------------------------------
echo "== ferox-audio-sim preflight =="

command -v docker >/dev/null 2>&1 || fail "docker not found on PATH."

[ -d /dev/snd ] || fail "/dev/snd does not exist — this host has no audio \
hardware the container can attach to. Cannot run the audio bridge here."

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    fail "image ${IMAGE} not found locally. Build it first:
    ./scripts/build.sh"
fi

if [ ! -S "${PULSE_DIR}/native" ]; then
    echo "WARNING: no PulseAudio socket at ${PULSE_DIR}/native."
    echo "         host_mic capture needs it; silence/file mode still works."
fi

echo "  docker image : ${IMAGE} OK"
echo "  /dev/snd     : present"
echo "  ROS_DOMAIN_ID: ${ROS_DOMAIN_ID}"
echo "  dds iface    : ${FEROX_DDS_INTERFACE:-<auto>}"
echo "  dds peers    : ${FEROX_DDS_PEERS:-<none, multicast only>}"
echo "  run as       : ${UID_N}:${GID_N}"

# ---- clean any stale container -------------------------------------------
if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
    echo "Removing existing ${CONTAINER} container..."
    docker rm -f "${CONTAINER}" >/dev/null
fi

# ---- run ------------------------------------------------------------------
echo "Starting ${CONTAINER}..."
docker run -d \
    --name "${CONTAINER}" \
    --network host \
    --device /dev/snd \
    --user "${UID_N}:${GID_N}" \
    -v "${PULSE_DIR}:${PULSE_DIR}" \
    -e PULSE_SERVER="unix:${PULSE_DIR}/native" \
    -e HOME=/tmp \
    -e ROS_DOMAIN_ID="${ROS_DOMAIN_ID}" \
    -e RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    -e FEROX_DDS_INTERFACE="${FEROX_DDS_INTERFACE}" \
    -e FEROX_DDS_PEERS="${FEROX_DDS_PEERS}" \
    "${IMAGE}" "$@" >/dev/null

# ---- fail-loud wait on the readiness marker ------------------------------
echo -n "Waiting for '${READY_MARKER}' (timeout ${READY_TIMEOUT}s)"
deadline=$(( $(date +%s) + READY_TIMEOUT ))
while true; do
    if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
        echo ""
        echo "---- ${CONTAINER} logs ----" >&2
        docker logs "${CONTAINER}" 2>&1 | tail -40 >&2
        fail "${CONTAINER} exited before becoming ready."
    fi
    if docker logs "${CONTAINER}" 2>&1 | grep -q "${READY_MARKER}"; then
        echo " — up."
        break
    fi
    if [ "$(date +%s)" -ge "${deadline}" ]; then
        echo ""
        echo "---- ${CONTAINER} logs ----" >&2
        docker logs "${CONTAINER}" 2>&1 | tail -40 >&2
        fail "${CONTAINER} did not report ready within ${READY_TIMEOUT}s."
    fi
    echo -n "."
    sleep 1
done

# ---- surface success ------------------------------------------------------
CPID="$(docker inspect -f '{{.State.Pid}}' "${CONTAINER}")"
echo ""
echo "${CONTAINER} is up."
echo "  host PID  : ${CPID}"
echo "  logs      : docker logs -f ${CONTAINER}"
echo "  topics    : docker exec ${CONTAINER} ros2 topic list | grep audio"
echo "  stop      : docker rm -f ${CONTAINER}"
