"""Trusted client-address helpers shared by the intake and portal services.

Both HTTP services bind loopback and are reached through the host nginx.  The
nginx vhost overwrites ``X-Citespec-Client-IP`` with the real peer address; the
application may only trust that header when the immediate peer is inside one of
the configured proxy networks.  Never trust ``X-Forwarded-For`` here: it can be
supplied by the client and would make rate limits and audit logs spoofable.

Known boundary (P2): a loopback peer is not proof of a proxy.  Another local
host user can connect directly to ``127.0.0.1:8770``/``8765`` and supply the
header themselves, so the header only separates remote clients, not local ones.
Before the browser entry is exposed, the listener should move to a
permission-restricted Unix socket (or carry an equivalent verifiable proxy
identity) so the application can authenticate its reverse proxy.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

DEFAULT_TRUSTED_PROXIES = ("127.0.0.1/32", "::1/128")


def _networks(values: Iterable[str]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    result: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        result.append(ipaddress.ip_network(value, strict=False))
    return tuple(result)


def _inside(
    address: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def trusted_client_ip(
    remote: str | None,
    forwarded: str | None,
    *,
    trusted_proxies: Iterable[str] = DEFAULT_TRUSTED_PROXIES,
) -> str:
    """Return a stable, non-spoofable client address for limits and audit."""

    remote_value = (remote or "").strip() or "unknown"
    header_value = (forwarded or "").strip()
    if not header_value:
        return remote_value
    networks = _networks(trusted_proxies)
    if not _inside(remote_value, networks):
        return remote_value
    try:
        candidate = ipaddress.ip_address(header_value)
    except ValueError:
        return remote_value
    return str(candidate)
