import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = ROOT / "docker/entrypoint-g1-dds.sh"


def _write_fake_ip(tmp_path: Path) -> Path:
    executable = tmp_path / "ip"
    executable.write_text(
        """#!/usr/bin/env bash
set -eu
if [[ \"$1 $2 $3 $4\" == \"-o link show dev\" ]]; then
  case \"${FAKE_LINK_MODE:-up}\" in
    absent) exit 1 ;;
    down) echo \"4: $5: <BROADCAST,MULTICAST> mtu 1500 state DOWN\" ;;
    no-carrier) echo \"4: $5: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500 state DOWN\" ;;
    up) echo \"4: $5: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\" ;;
    *) exit 2 ;;
  esac
  exit 0
fi
if [[ \"$*\" == \"-o -4 address show dev eth1 scope global\" ]]; then
  if [[ -n \"${FAKE_IPV4_CIDR:-}\" ]]; then
    echo \"4: eth1 inet ${FAKE_IPV4_CIDR} scope global eth1\"
  fi
  exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _run(tmp_path: Path, **changes: str) -> subprocess.CompletedProcess[str]:
    _write_fake_ip(tmp_path)
    template = tmp_path / "cyclonedds.xml.template"
    template.write_text(
        "<Interfaces>${CYCLONE_INTERFACE_BLOCK}</Interfaces>"
        "<Peers>${CYCLONE_PEERS_BLOCK}</Peers>",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "FEROX_DDS_INTERFACE": "eth1",
            "FEROX_DDS_IPV4_CIDR": "192.168.123.18/24",
            "FEROX_DDS_PEERS": "",
            "FAKE_LINK_MODE": "up",
            "FAKE_IPV4_CIDR": "192.168.123.18/24",
            "CYCLONEDDS_TEMPLATE": str(template),
            "CYCLONEDDS_URI": "file:///tmp/cyclonedds.xml",
        }
    )
    env.update(changes)
    return subprocess.run(
        ["bash", str(ENTRYPOINT), "/usr/bin/true"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def test_dds_entrypoint_accepts_exact_live_ipv4_binding(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "interface: eth1 (192.168.123.18/24)" in result.stdout


def test_dds_entrypoint_rejects_absent_interface(tmp_path):
    result = _run(tmp_path, FAKE_LINK_MODE="absent")
    assert result.returncode == 2
    assert "is not present" in result.stdout


def test_dds_entrypoint_rejects_no_carrier(tmp_path):
    result = _run(tmp_path, FAKE_LINK_MODE="no-carrier")
    assert result.returncode == 2
    assert "is not carrier-ready" in result.stdout


def test_dds_entrypoint_rejects_missing_or_wrong_ipv4(tmp_path):
    missing = _run(tmp_path, FAKE_IPV4_CIDR="")
    wrong = _run(tmp_path, FAKE_IPV4_CIDR="192.168.123.19/24")
    assert missing.returncode == 2
    assert wrong.returncode == 2
    assert "missing approved IPv4 CIDR" in missing.stdout
    assert "missing approved IPv4 CIDR" in wrong.stdout
