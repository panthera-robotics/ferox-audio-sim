#!/usr/bin/env bash
#
# ferox-audio-sim DDS entrypoint — renders the cyclone template using
# the two optional FEROX_DDS_* env vars and chains to the next entrypoint.
#
# Defaults are robot-LAN safe (no env vars => auto-detect + multicast).
# CYCLONEDDS_URI is set via image ENV in the Dockerfile, not re-exported here.

set -e

# Interface block: pin if FEROX_DDS_INTERFACE set, else auto-detect.
if [[ -n "${FEROX_DDS_INTERFACE}" ]]; then
  export CYCLONE_INTERFACE_BLOCK="<NetworkInterface name=\"${FEROX_DDS_INTERFACE}\" presence_required=\"true\" />"
else
  export CYCLONE_INTERFACE_BLOCK="<NetworkInterface autodetermine=\"true\" />"
fi

# Peers block: empty unless FEROX_DDS_PEERS set (space-separated IPs).
export CYCLONE_PEERS_BLOCK=""
for peer in ${FEROX_DDS_PEERS}; do
  CYCLONE_PEERS_BLOCK+="<Peer Address=\"${peer}\"/>"$'\n        '
done

TEMPLATE="${CYCLONEDDS_TEMPLATE:-/etc/cyclonedds.xml.template}"
envsubst < "$TEMPLATE" > /tmp/cyclonedds.xml

echo "[dds] interface: ${FEROX_DDS_INTERFACE:-<auto>}"
echo "[dds] peers:     ${FEROX_DDS_PEERS:-<none, multicast only>}"
echo "[dds] config:    ${CYCLONEDDS_URI}"

exec "$@"
