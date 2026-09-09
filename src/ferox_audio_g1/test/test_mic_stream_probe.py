import pytest

from ferox_audio_g1 import mic_stream_probe
from ferox_audio_g1.mic_stream_probe import _ipv4, _open_multicast_socket


def test_probe_endpoint_contract_distinguishes_group_and_unicast_addresses():
    assert _ipv4(
        "239.168.123.161", label="group", multicast=True
    ) == "239.168.123.161"
    assert _ipv4(
        "192.168.123.164", label="interface", multicast=False
    ) == "192.168.123.164"

    with pytest.raises(ValueError, match="multicast"):
        _ipv4("192.168.123.161", label="group", multicast=True)
    with pytest.raises(ValueError, match="unicast"):
        _ipv4("239.168.123.161", label="sender", multicast=False)
    with pytest.raises(ValueError, match="dotted IPv4"):
        _ipv4("eth0", label="interface", multicast=False)
    with pytest.raises(ValueError, match="unspecified"):
        _ipv4("0.0.0.0", label="interface", multicast=False)


def test_socket_is_closed_when_multicast_setup_fails(monkeypatch):
    class FakeSocket:
        closed = False

        def setsockopt(self, *args):
            if args[1] == mic_stream_probe.socket.IP_ADD_MEMBERSHIP:
                raise OSError("join failed")

        def bind(self, address):
            assert address == ("", 5555)

        def settimeout(self, timeout):
            raise AssertionError("must not continue after failed join")

        def close(self):
            self.closed = True

    fake = FakeSocket()
    monkeypatch.setattr(mic_stream_probe.socket, "socket", lambda *args: fake)

    with pytest.raises(OSError, match="join failed"):
        _open_multicast_socket(
            group="239.168.123.161",
            port=5555,
            interface="192.168.123.164",
        )
    assert fake.closed
