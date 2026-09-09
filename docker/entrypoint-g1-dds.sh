#!/usr/bin/env bash
set -euo pipefail

: "${FEROX_DDS_INTERFACE:?set FEROX_DDS_INTERFACE in the deployment environment}"
: "${FEROX_DDS_IPV4_CIDR:?set FEROX_DDS_IPV4_CIDR in the deployment environment}"

if [[ ! "${FEROX_DDS_INTERFACE}" =~ ^[A-Za-z0-9_.:-]{1,15}$ ]] \
    || [[ "${FEROX_DDS_INTERFACE}" == "lo" ]]; then
  echo "invalid non-loopback FEROX_DDS_INTERFACE: ${FEROX_DDS_INTERFACE}" >&2
  exit 2
fi
if [[ ! "${FEROX_DDS_IPV4_CIDR}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]]; then
  echo "FEROX_DDS_IPV4_CIDR must be an IPv4 CIDR" >&2
  exit 2
fi
link_state="$(ip -o link show dev "${FEROX_DDS_INTERFACE}" 2>/dev/null || true)"
if [[ -z "${link_state}" ]]; then
  echo "FEROX_DDS_INTERFACE is not present: ${FEROX_DDS_INTERFACE}" >&2
  exit 2
fi
link_flags="${link_state#*<}"
link_flags="${link_flags%%>*}"
if [[ ",${link_flags}," != *",UP,"* \
    || ",${link_flags}," != *",LOWER_UP,"* \
    || ",${link_flags}," == *",NO-CARRIER,"* ]]; then
  echo "FEROX_DDS_INTERFACE is not carrier-ready: ${FEROX_DDS_INTERFACE}" >&2
  exit 2
fi
if ! ip -o -4 address show dev "${FEROX_DDS_INTERFACE}" scope global \
    | awk '{print $4}' \
    | grep -Fxq -- "${FEROX_DDS_IPV4_CIDR}"; then
  echo "FEROX_DDS_INTERFACE is missing approved IPv4 CIDR: ${FEROX_DDS_IPV4_CIDR}" >&2
  exit 2
fi

export CYCLONE_INTERFACE_BLOCK="<NetworkInterface name=\"${FEROX_DDS_INTERFACE}\" presence_required=\"true\" />"

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

echo "[dds] interface: ${FEROX_DDS_INTERFACE} (${FEROX_DDS_IPV4_CIDR})"
echo "[dds] peers:     ${FEROX_DDS_PEERS:-<none, multicast only>}"
echo "[dds] config:    ${CYCLONEDDS_URI}"
exec "$@"
