#!/bin/bash
set -e

: "${FEROX_DDS_PEER_HOST:?FEROX_DDS_PEER_HOST must be set (host tailscale IP)}"
: "${FEROX_DDS_PEER_CLOUD:?FEROX_DDS_PEER_CLOUD must be set (cloud tailscale IP)}"

TEMPLATE="${CYCLONEDDS_TEMPLATE:-/etc/cyclonedds.xml.template}"
RENDERED="/tmp/cyclonedds.xml"

envsubst < "$TEMPLATE" > "$RENDERED"
export CYCLONEDDS_URI="file://$RENDERED"

echo "[dds] Cyclone peers: $FEROX_DDS_PEER_HOST, $FEROX_DDS_PEER_CLOUD"
echo "[dds] config: $CYCLONEDDS_URI"

exec "$@"
