"""Peer identity resolution and Linux SO_PEERCRED helpers."""

from __future__ import annotations

import os
import socket
import struct
from dataclasses import dataclass
from typing import Any, Protocol

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - Windows development only
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Identity:
    uid: int
    gid: int
    username: str
    role: str
    is_admin: bool
    is_portal: bool = False


class IdentityResolverProtocol(Protocol):
    def resolve(self, uid: int, gid: int) -> Identity: ...


class IdentityResolver:
    """Resolve a peer UID to a Linux account and the admin role."""

    def __init__(self, admin_group: str, portal_group: str = "citespec-portal") -> None:
        self.admin_group = admin_group
        self.portal_group = portal_group

    def resolve(self, uid: int, gid: int) -> Identity:
        if pwd is None or grp is None:
            raise RuntimeError("peer identity resolution requires Linux pwd/grp support")
        account = pwd.getpwuid(uid)
        group_ids = set(os.getgrouplist(account.pw_name, account.pw_gid))
        group_ids.add(gid)
        is_admin = False
        is_portal = False
        for group_id in group_ids:
            try:
                group = grp.getgrgid(group_id)
            except KeyError:
                continue
            if group.gr_name == self.admin_group:
                is_admin = True
            if group.gr_name == self.portal_group:
                is_portal = True
        if is_admin:
            role = "admin"
        else:
            # The broker users table only permits member/admin.  The portal
            # service account is a machine identity; is_portal is the actual
            # capability flag and is checked before every portal command.
            role = "member"
        return Identity(
            uid=uid,
            gid=gid,
            username=account.pw_name,
            role=role,
            is_admin=is_admin,
            is_portal=is_portal,
        )


def peer_credentials(sock: Any) -> tuple[int, int, int]:
    """Return (pid, uid, gid) from a connected Linux Unix socket."""
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None:
        raise RuntimeError("SO_PEERCRED is not supported on this platform")
    raw = sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return int(pid), int(uid), int(gid)
