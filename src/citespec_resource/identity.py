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


# The portal account is a machine identity that the broker trusts to submit
# administrator actors.  Reusing an interactive or privileged account with the
# same name would hand that capability to a human, so resolution is fail closed
# unless the account still matches the service-account shape created by the
# installer.
PORTAL_NON_INTERACTIVE_SHELLS = frozenset(
    {
        "/bin/false",
        "/usr/bin/false",
        "/sbin/false",
        "/usr/sbin/false",
        "/bin/nologin",
        "/usr/bin/nologin",
        "/sbin/nologin",
        "/usr/sbin/nologin",
    }
)
PORTAL_ALLOWED_SUPPLEMENTARY_GROUPS = frozenset({"citespec-cli"})
PORTAL_FORBIDDEN_GROUPS = frozenset(
    {
        "root",
        "sudo",
        "wheel",
        "admin",
        "adm",
        "docker",
        "lxd",
        "disk",
        "kvm",
        "libvirt",
        "shadow",
        "video",
        "render",
        "citespec-admins",
        "citespec-members",
        "citespec-gpu",
    }
)
PORTAL_MAX_SYSTEM_UID = 999


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


def portal_account_issues(service_account: str) -> tuple[str, ...]:
    """Return why ``service_account`` is not a safe portal machine identity.

    An empty tuple means the account is acceptable.  Callers must treat a
    non-empty result as "portal capability disabled" (fail closed).
    """

    if pwd is None or grp is None or not service_account:
        return ("account lookup is unavailable",)
    try:
        account = pwd.getpwnam(service_account)
    except KeyError:
        return (f"account {service_account!r} does not exist",)

    issues: list[str] = []
    uid = int(account.pw_uid)
    if uid == 0:
        issues.append("account is root")
    elif uid > PORTAL_MAX_SYSTEM_UID:
        issues.append(f"UID {uid} is outside the system range (1-{PORTAL_MAX_SYSTEM_UID})")
    try:
        primary = grp.getgrgid(account.pw_gid)
    except KeyError:
        issues.append("primary group does not exist")
    else:
        if primary.gr_name != service_account:
            issues.append(f"primary group is {primary.gr_name!r}, expected {service_account!r}")
    shell = str(getattr(account, "pw_shell", ""))
    if shell not in PORTAL_NON_INTERACTIVE_SHELLS:
        issues.append(f"login shell {shell!r} is interactive")
    try:
        group_ids = set(os.getgrouplist(account.pw_name, account.pw_gid))
    except OSError:
        issues.append("supplementary groups could not be read")
    else:
        for group_id in sorted(group_ids):
            try:
                group = grp.getgrgid(group_id)
            except KeyError:
                continue
            if group.gr_name == service_account:
                continue
            if group.gr_name in PORTAL_FORBIDDEN_GROUPS:
                issues.append(f"member of privileged group {group.gr_name!r}")
            elif group.gr_name not in PORTAL_ALLOWED_SUPPLEMENTARY_GROUPS:
                issues.append(f"unexpected supplementary group {group.gr_name!r}")
    return tuple(issues)


def resolve_portal_account(service_account: str) -> tuple[int, str] | None:
    """Resolve the dedicated portal service account to ``(uid, name)``.

    Returns ``None`` when the account cannot be resolved or no longer matches
    the service-account policy.  Callers must treat that as "portal capability
    disabled" (fail closed), never as "allow".
    """

    if portal_account_issues(service_account):
        return None
    assert pwd is not None
    account = pwd.getpwnam(service_account)
    return int(account.pw_uid), account.pw_name


def portal_group_extra_members(group: str, service_account: str) -> tuple[str, ...]:
    """Return supplementary members of the portal group other than the service.

    The portal account's primary group is the portal group, so it is not listed
    in ``gr_mem``.  Any other entry is a configuration drift that the caller
    should warn about; authorization itself is bound to the account UID.
    """

    if grp is None or not group:
        return ()
    try:
        info = grp.getgrnam(group)
    except KeyError:
        return ()
    return tuple(sorted(name for name in info.gr_mem if name != service_account))


class IdentityResolver:
    """Resolve a peer UID to a Linux account and the admin role.

    Portal capability is bound to the dedicated service account's UID *and*
    account name.  It is deliberately not derived from membership in the
    ``citespec-portal`` group: a human account added to that group by mistake
    must not be able to submit portal administrator actors to the broker.
    """

    def __init__(
        self,
        admin_group: str,
        portal_group: str = "citespec-portal",
        *,
        portal_uid: int | None = None,
        portal_username: str | None = None,
    ) -> None:
        self.admin_group = admin_group
        self.portal_group = portal_group
        self.portal_uid = portal_uid
        self.portal_username = portal_username

    def resolve(self, uid: int, gid: int) -> Identity:
        if pwd is None or grp is None:
            raise RuntimeError("peer identity resolution requires Linux pwd/grp support")
        account = pwd.getpwuid(uid)
        group_ids = set(os.getgrouplist(account.pw_name, account.pw_gid))
        group_ids.add(gid)
        is_admin = False
        for group_id in group_ids:
            try:
                group = grp.getgrgid(group_id)
            except KeyError:
                continue
            if group.gr_name == self.admin_group:
                is_admin = True
        is_portal = (
            self.portal_uid is not None
            and int(uid) == self.portal_uid
            and account.pw_name == self.portal_username
        )
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
