"""Package metadata shared by the server, GUI, and packaged client.

``PROTOCOL_VERSION`` describes the JSON/broker contract currently implemented.
The server also advertises ``MIN_CLIENT_PROTOCOL``: a client older than that
value must be updated before it can safely use the structured GUI/API surface.
Keep both values in step with releases and never infer compatibility from a
version string alone.
"""

from __future__ import annotations

__version__ = "0.1.0"
PROTOCOL_VERSION = 1
MIN_CLIENT_PROTOCOL = 1


def version_info() -> dict[str, int | str]:
    """Small, JSON-safe handshake payload used by ``version --json``."""

    return {
        "version": __version__,
        "protocol": PROTOCOL_VERSION,
        "min_client_protocol": MIN_CLIENT_PROTOCOL,
    }
