"""Unified member authorization (goal member-onboarding-and-ux.md §6).

One rule, applied everywhere a member-initiated privileged operation enters
the broker: normal RPC commands, the shell/file/tunnel special entries, and
queue admission. Admin identities keep the trusted admin-group path and are
exempt (server-side verification, §6.1).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import store as member_store

# Commands that require an active member record when
# policy.membership_required is on. Self-status commands (me, status of own
# allocations, queue listing) stay reachable so a blocked member can see why
# and what to do next (§6.2).
MEMBER_COMMANDS = frozenset(
    {
        "apply",
        "queue_cancel",
        "release",
        "logs",
        "job_start",
        "job_list",
        "job_logs",
        "job_stop",
    }
)

# Streaming/special entries checked at their own handlers (same rule).
SPECIAL_ENTRIES = frozenset({"shell", "file_put", "file_get", "tunnel"})

# Maintenance/monitor are not an authorization bypass.  Members may inspect
# state and clean up their own work, but they may not start new allocations,
# queue entries, jobs, or interactive/file/tunnel sessions.  The cleanup set is
# deliberately explicit; unknown commands continue to the normal handler so
# they fail as bad requests instead of being misreported as mode denials.
NON_ACTIVE_MEMBER_ALLOWED = frozenset(
    {
        "logs",
        "job_files",
        "job_list",
        "job_logs",
        "job_stop",
        "queue_cancel",
        "release",
    }
)


def check_for_mode(
    *,
    mode: str,
    command: str,
    is_admin: bool,
) -> dict[str, Any] | None:
    """Return a mode denial for a member command, or None when allowed.

    Active mode is unrestricted.  In maintenance/monitor, read-only and
    cleanup commands stay available so members can inspect and release their
    own resources, while every path that can create or extend work is denied.
    """
    if is_admin or mode == "active":
        return None
    if command not in MEMBER_COMMANDS and command not in SPECIAL_ENTRIES:
        return None
    if command in NON_ACTIVE_MEMBER_ALLOWED:
        return None
    if mode == "maintenance":
        message = "broker is in maintenance mode; new work and interactive sessions are disabled"
    else:
        message = "broker is in monitor-only mode; new work and interactive sessions are disabled"
    return {
        "code": "monitor_only",
        "message": message,
        "next_actions": ["contact_admin", "retry_later"],
    }


def check_for_command(
    connection: sqlite3.Connection,
    *,
    command: str,
    is_admin: bool,
    uid: int,
    membership_required: bool,
    now: Any = None,
) -> dict[str, Any] | None:
    """Return a structured denial for this command, or None when allowed.

    Structured denial shape (§11.4): code/message/next_actions so the CLI and
    GUI can render the situation without parsing strings.
    """
    if is_admin or not membership_required:
        return None
    is_member_command = command in MEMBER_COMMANDS or command in SPECIAL_ENTRIES
    if not is_member_command:
        return None
    decision = member_store.check_access(connection, uid, now=now)
    if decision["allowed"]:
        return None
    return denial_from_decision(decision)


def denial_from_decision(decision: dict[str, Any]) -> dict[str, Any]:
    reason = str(decision.get("reason", ""))
    if reason == "no_member_record":
        return {
            "code": "membership_required",
            "message": "还没有你的成员记录；普通资源操作需要管理员开通后才能使用。",
            "next_actions": ["join", "contact_admin"],
        }
    if reason == "expired":
        return {
            "code": "membership_expired",
            "message": "成员资格已到期；新工作环境与排队已停止。",
            "next_actions": ["contact_admin_renew"],
        }
    status = str(decision.get("status", ""))
    if status == "suspended":
        return {
            "code": "membership_suspended",
            "message": "你的交互访问已暂停；后台训练按管理员选择的策略处理。",
            "next_actions": ["contact_admin_resume"],
        }
    if status == "revoked":
        return {
            "code": "membership_revoked",
            "message": "成员资格已撤销；如需恢复请联系管理员。",
            "next_actions": ["contact_admin"],
        }
    if status == "provisioning":
        return {
            "code": "membership_provisioning",
            "message": "申请已批准，正在开户；开通完成后即可使用资源。",
            "next_actions": ["check_status"],
        }
    if status == "provision_failed":
        return {
            "code": "membership_provision_failed",
            "message": "开户未完成，尚未授予资源使用资格；管理员可重试开户操作。",
            "next_actions": ["contact_admin"],
        }
    return {
        "code": "membership_inactive",
        "message": "当前成员状态不允许进行资源操作。",
        "next_actions": ["contact_admin"],
    }
