"""Optional public collaborator directory.

The file is intentionally small and administrator-maintained.  Only fields
that are safe to show to every member are returned by :func:`load_members`.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True)
class MemberProfile:
    display_name: str = ""
    nickname: str = ""
    public_contact: str = ""


def _text(value: Any, field: str, username: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"members.{username}.{field} must be a string")
    return value.strip()


def load_members(path: str | Path) -> dict[str, MemberProfile]:
    """Load the public member directory.

    A missing file is normal and means no extra identity information is
    available.  Invalid usernames or field types are configuration errors.
    Private contact fields are deliberately ignored; only ``public_contact``
    is exposed.
    """

    member_path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    # The directory is administrator-maintained.  Never follow a symlink if
    # the platform can enforce that; on other platforms fall back to a regular
    # file check so a directory/device cannot be parsed as TOML.
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(member_path, flags)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise OSError(f"cannot open member directory {member_path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"member directory is not a regular file: {member_path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = tomllib.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)
    members_raw = raw.get("members", {})
    if not isinstance(members_raw, dict):
        raise ValueError("members must be a TOML table")
    members: dict[str, MemberProfile] = {}
    for username, value in members_raw.items():
        name = str(username)
        if not USERNAME_RE.fullmatch(name):
            raise ValueError(f"invalid member username: {name}")
        if not isinstance(value, dict):
            raise ValueError(f"members.{name} must be a table")
        members[name] = MemberProfile(
            display_name=_text(value.get("display_name"), "display_name", name),
            nickname=_text(value.get("nickname"), "nickname", name),
            public_contact=_text(value.get("public_contact"), "public_contact", name),
        )
    return members


def public_member_data(
    members: dict[str, MemberProfile],
    username: str,
) -> dict[str, str]:
    profile = members.get(username)
    if profile is None:
        return {}
    data: dict[str, str] = {}
    if profile.display_name:
        data["display_name"] = profile.display_name
    if profile.nickname:
        data["nickname"] = profile.nickname
    if profile.public_contact:
        data["public_contact"] = profile.public_contact
    return data
