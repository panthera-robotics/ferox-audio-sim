#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${FEROX_DDS_INTERFACE:-}" ]]; then
  export CYCLONE_INTERFACE_BLOCK="<NetworkInterface name=\"${FEROX_DDS_INTERFACE}\" presence_required=\"true\" />"
else
  export CYCLONE_INTERFACE_BLOCK="<NetworkInterface autodetermine=\"true\" />"
fi

export CYCLONE_PEERS_BLOCK=""
for peer in ${FEROX_DDS_PEERS:-}; do
  CYCLONE_PEERS_BLOCK+="<Peer Address=\"${peer}\"/>"$'\n        '
done

TEMPLATE="${CYCLONEDDS_TEMPLATE:-/etc/cyclonedds.xml.template}"
python3 - "$TEMPLATE" /tmp/cyclonedds.xml <<'PY'
import os
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
for name in ("CYCLONE_INTERFACE_BLOCK", "CYCLONE_PEERS_BLOCK"):
    source = source.replace("${" + name + "}", os.environ[name])
if "${" in source:
    raise SystemExit("unexpanded placeholder remains in Cyclone DDS config")
pathlib.Path(sys.argv[2]).write_text(source, encoding="utf-8")
PY

echo "[dds] interface: ${FEROX_DDS_INTERFACE:-<auto>}"
echo "[dds] peers:     ${FEROX_DDS_PEERS:-<none, multicast only>}"
echo "[dds] config:    ${CYCLONEDDS_URI}"
exec "$@"
