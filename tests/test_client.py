import socket

import pytest

from udc_resource.client import BrokerClient
from udc_resource.errors import BrokerUnavailable


def test_stream_read_does_not_consume_payload_bytes():
    left, right = socket.socketpair()
    try:
        right.sendall(b'{"ok":true}\nSSH-2.0-test\r\n')
        client = BrokerClient("unused", timeout=1.0)
        assert client._read_line_stream(left) == {"ok": True}
        assert left.recv(64) == b"SSH-2.0-test\r\n"
    finally:
        left.close()
        right.close()


def test_missing_socket_is_reported_as_unavailable(tmp_path):
    client = BrokerClient(str(tmp_path / "missing.sock"), timeout=0.1)
    with pytest.raises(BrokerUnavailable):
        client.request("ping")
