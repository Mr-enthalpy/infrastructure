from citespec_resource import MIN_CLIENT_PROTOCOL, PROTOCOL_VERSION, __version__
from citespec_resource.compat import check_server_handshake


def test_handshake_ok():
    result = check_server_handshake(
        {
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "min_client_protocol": MIN_CLIENT_PROTOCOL,
        }
    )
    assert result.status == "ok"
    assert result.ok is True
    assert result.server_protocol == PROTOCOL_VERSION


def test_handshake_old_server_is_unknown():
    result = check_server_handshake({"allocations": []})
    assert result.status == "unknown"
    assert result.ok is True
    assert "protocol" in result.message.lower()


def test_handshake_requires_client_update_for_newer_server():
    result = check_server_handshake(
        {
            "version": "9.0.0",
            "protocol": PROTOCOL_VERSION + 1,
            "min_client_protocol": PROTOCOL_VERSION + 1,
        }
    )
    assert result.status == "client_update_required"
    assert result.ok is False
    assert "update" in result.message.lower()


def test_handshake_warns_when_server_is_older():
    result = check_server_handshake(
        {
            "version": "0.0.1",
            "protocol": 0,
            "min_client_protocol": 1,
        }
    )
    assert result.status == "server_update_required"
    assert result.ok is False
