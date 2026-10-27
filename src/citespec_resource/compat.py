"""Client/server protocol compatibility checks.

The CLI JSON envelope is intentionally additive, but the local GUI depends on
structured fields and must not silently talk to an incompatible server.  The
broker advertises ``version``, ``protocol`` and ``min_client_protocol`` in
``status``/``list`` responses; this module turns those values into a small,
displayable result.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from . import PROTOCOL_VERSION, __version__


@dataclass(frozen=True)
class Compatibility:
    status: str
    message: str
    client_version: str
    client_protocol: int
    server_version: str = ""
    server_protocol: int | None = None
    min_client_protocol: int | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "unknown"}

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_server_handshake(data: Mapping[str, Any] | None) -> Compatibility:
    """Compare a broker response's version fields with this client.

    An old server that predates the handshake reports ``unknown`` rather than a
    hard failure: it can still serve text CLI commands, but the GUI should show
    a warning because structured compatibility was not verified.
    """

    payload = data or {}
    server_version = str(payload.get("version") or "")
    server_protocol = _optional_int(payload.get("protocol"))
    min_client_protocol = _optional_int(payload.get("min_client_protocol"))

    if server_protocol is None or min_client_protocol is None:
        return Compatibility(
            status="unknown",
            message=(
                "Server did not advertise a protocol version; update the server "
                "before relying on the GUI."
            ),
            client_version=__version__,
            client_protocol=PROTOCOL_VERSION,
            server_version=server_version,
            server_protocol=server_protocol,
            min_client_protocol=min_client_protocol,
        )

    base = Compatibility(
        status="ok",
        message="Client and server protocol versions are compatible.",
        client_version=__version__,
        client_protocol=PROTOCOL_VERSION,
        server_version=server_version,
        server_protocol=server_protocol,
        min_client_protocol=min_client_protocol,
    )
    if min_client_protocol > PROTOCOL_VERSION or server_protocol > PROTOCOL_VERSION:
        return Compatibility(
            status="client_update_required",
            message=(
                "This client is too old for the server. Update it with "
                "`citespec-resource update`, then restart the GUI."
            ),
            client_version=base.client_version,
            client_protocol=base.client_protocol,
            server_version=server_version,
            server_protocol=server_protocol,
            min_client_protocol=min_client_protocol,
        )
    if server_protocol < PROTOCOL_VERSION:
        return Compatibility(
            status="server_update_required",
            message=(
                "The server is older than this client. Ask an administrator to "
                "update the server package; text commands may still work."
            ),
            client_version=base.client_version,
            client_protocol=base.client_protocol,
            server_version=server_version,
            server_protocol=server_protocol,
            min_client_protocol=min_client_protocol,
        )
    return base


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
