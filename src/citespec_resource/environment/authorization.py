"""Access checks for projects, environment versions, and legacy claims.

Environment memory reuses the existing member record: a member is identified by
``member_id`` (stable across UID or workspace changes), and every write path
requires an active membership at the moment of the request. Administrators keep
the trusted admin path for review actions, but creating an environment version
still records a real member as its creator.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from ..membership import store as member_store
from . import models
from . import store as env_store


def member_id_for(connection: sqlite3.Connection, uid: int) -> str:
    member = member_store.get_member_by_uid(connection, uid)
    if member is None:
        raise models.EnvironmentForbidden(
            "还没有你的成员记录；环境记忆需要管理员开通账号后才能使用。"
        )
    return member.member_id


def require_active_member(
    connection: sqlite3.Connection,
    *,
    uid: int,
    now: datetime | None = None,
) -> str:
    """Return the member_id for an active member or raise EnvironmentForbidden."""

    member_id = member_id_for(connection, uid)
    decision = member_store.check_access(connection, uid, now=now)
    if decision["allowed"]:
        return member_id
    reason = str(decision.get("reason", ""))
    if reason == "expired":
        raise models.EnvironmentForbidden(
            "成员资格已到期，环境记忆与构建已停止；请联系管理员续期。"
        )
    if reason == "status:suspended":
        raise models.EnvironmentForbidden("你的成员资格已暂停，暂时不能保存、构建或认领环境。")
    if reason == "status:revoked":
        raise models.EnvironmentForbidden("成员资格已撤销，不能使用环境记忆。")
    raise models.EnvironmentForbidden("当前成员状态不允许使用环境记忆，请联系管理员。")


def can_view_version(
    context: dict[str, Any],
    *,
    member_id: str,
    is_admin: bool,
) -> bool:
    """Visibility rules for one version.

    ``project`` visibility currently means the owning project only: the first
    release has one owner per project, so it is deliberately not wider than
    ``private``. ``admin_shared`` was explicitly approved for lab-wide use.
    """

    if is_admin:
        return True
    if context["owner_member_id"] == member_id:
        return True
    return context["visibility"] == models.VISIBILITY_ADMIN_SHARED


def require_project(
    connection: sqlite3.Connection,
    *,
    uid: int,
    is_admin: bool,
    project_id: str | None = None,
    name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve a project the caller may read or write."""

    if name is not None:
        member_id = require_active_member(connection, uid=uid, now=now)
        return env_store.get_project_by_name(connection, owner_member_id=member_id, name=name)
    if project_id is None:
        raise models.EnvironmentError("必须指定项目")
    project = env_store.get_project(connection, project_id)
    if is_admin:
        return project
    member_id = require_active_member(connection, uid=uid, now=now)
    if project["owner_member_id"] != member_id:
        raise models.EnvironmentForbidden("无权访问该项目")
    return project


def require_version(
    connection: sqlite3.Connection,
    *,
    uid: int,
    is_admin: bool,
    version_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    context = env_store.version_context(connection, version_id)
    if is_admin:
        return context
    member_id = require_active_member(connection, uid=uid, now=now)
    if not can_view_version(context, member_id=member_id, is_admin=False):
        raise models.EnvironmentForbidden("无权访问该环境版本")
    return context


def require_claim_owner(
    claim: dict[str, Any],
    *,
    member_id: str,
    is_admin: bool,
) -> None:
    if is_admin:
        return
    if claim["member_id"] != member_id:
        raise models.EnvironmentForbidden("无权查看该认领申请")
