"""Authoritative member, key, application, and operation records.

All mutations are conditional (state-machine guarded) and audited through the
broker's existing events table. Idempotency keys make approvals and retries
single-shot (§7.3, §13).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from .. import store as broker_store
from ..errors import Conflict, NotFound
from .models import (
    ALLOWED_MEMBER_TRANSITIONS,
    MEMBER_STATUSES,
    MemberRecord,
    is_valid_now,
    key_fingerprint,
    utcnow,
)


def _now(now: datetime | None) -> str:
    return (now or utcnow()).isoformat()


def _member_from_row(row: sqlite3.Row) -> MemberRecord:
    return MemberRecord(
        member_id=str(row["member_id"]),
        linux_uid=int(row["linux_uid"]) if row["linux_uid"] is not None else None,
        linux_username=row["linux_username"],
        status=str(row["status"]),
        valid_until=str(row["valid_until"]),
        display_name=str(row["display_name"]),
        nickname=str(row["nickname"]),
        public_contact=str(row["public_contact"]),
        private_contact=str(row["private_contact"]),
        access_version=int(row["access_version"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def get_member_by_uid(connection: sqlite3.Connection, uid: int) -> MemberRecord | None:
    row = connection.execute("SELECT * FROM members WHERE linux_uid = ?", (uid,)).fetchone()
    return _member_from_row(row) if row is not None else None


def get_member_by_username(connection: sqlite3.Connection, username: str) -> MemberRecord | None:
    row = connection.execute(
        "SELECT * FROM members WHERE linux_username = ?", (username,)
    ).fetchone()
    return _member_from_row(row) if row is not None else None


def get_member(connection: sqlite3.Connection, member_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM members WHERE member_id = ?", (member_id,)).fetchone()
    if row is None:
        raise NotFound(f"member not found: {member_id}")
    member = _member_from_row(row)
    keys = connection.execute(
        "SELECT * FROM member_keys WHERE member_id = ? ORDER BY created_at",
        (member_id,),
    ).fetchall()
    operations = connection.execute(
        "SELECT * FROM member_operations WHERE member_id = ? ORDER BY created_at DESC",
        (member_id,),
    ).fetchall()
    return {
        "member": {
            "member_id": member.member_id,
            "linux_uid": member.linux_uid,
            "linux_username": member.linux_username,
            "status": member.status,
            "valid_until": member.valid_until,
            "display_name": member.display_name,
            "nickname": member.nickname,
            "public_contact": member.public_contact,
            "access_version": member.access_version,
            "created_at": member.created_at,
            "updated_at": member.updated_at,
        },
        "keys": [
            {
                "key_id": str(key["key_id"]),
                "fingerprint": str(key["fingerprint"]),
                "status": str(key["status"]),
                "created_at": str(key["created_at"]),
            }
            for key in keys
        ],
        "operations": [
            {
                "operation_id": str(op["operation_id"]),
                "action": str(op["action"]),
                "status": str(op["status"]),
                "stage": str(op["stage"]),
                "attempts": int(op["attempts"]),
                "last_error": str(op["last_error"]),
                "created_at": str(op["created_at"]),
            }
            for op in operations
        ],
    }


def list_members(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM members ORDER BY created_at ASC, member_id").fetchall()
    result = []
    for row in rows:
        member = _member_from_row(row)
        result.append(
            {
                "member_id": member.member_id,
                "linux_uid": member.linux_uid,
                "linux_username": member.linux_username,
                "status": member.status,
                "valid_until": member.valid_until,
                "display_name": member.display_name,
                "nickname": member.nickname,
                "public_contact": member.public_contact,
                "access_version": member.access_version,
            }
        )
    return result


def create_member(
    connection: sqlite3.Connection,
    *,
    member_id: str | None = None,
    linux_uid: int | None = None,
    linux_username: str | None = None,
    status: str = "provisioning",
    valid_until: str = "",
    display_name: str = "",
    nickname: str = "",
    public_contact: str = "",
    private_contact: str = "",
    now: datetime | None = None,
) -> str:
    if status not in MEMBER_STATUSES:
        raise Conflict(f"unknown member status: {status}")
    resolved = member_id or uuid.uuid4().hex
    timestamp = _now(now)
    own = not connection.in_transaction
    try:
        if own:
            connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO members(
                member_id, linux_uid, linux_username, status, valid_until,
                display_name, nickname, public_contact, private_contact,
                access_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                resolved,
                linux_uid,
                linux_username,
                status,
                valid_until,
                display_name,
                nickname,
                public_contact,
                private_contact,
                timestamp,
                timestamp,
            ),
        )
        broker_store.record_event(
            connection,
            actor_uid=linux_uid,
            action="member_created",
            detail={"member_id": resolved, "status": status},
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise
    return resolved


def transition_member(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    target: str,
    reason: str,
    actor_uid: int | None,
    valid_until: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Conditional state transition (§6.1, §13): monotonic, version-bumped."""
    if target not in MEMBER_STATUSES:
        raise Conflict(f"unknown member status: {target}")
    timestamp = _now(now)
    own = not connection.in_transaction
    try:
        if own:
            connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM members WHERE member_id = ?", (member_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"member not found: {member_id}")
        current_status = str(row["status"])
        if target not in ALLOWED_MEMBER_TRANSITIONS.get(current_status, ()):
            raise Conflict(f"member {member_id} cannot transition {current_status} -> {target}")
        connection.execute(
            """
            UPDATE members
            SET status = ?, access_version = access_version + 1,
                valid_until = CASE WHEN ? != '' THEN ? ELSE valid_until END,
                updated_at = ?
            WHERE member_id = ?
            """,
            (target, valid_until or "", valid_until or "", timestamp, member_id),
        )
        updated_row = connection.execute(
            "SELECT * FROM members WHERE member_id = ?", (member_id,)
        ).fetchone()
        member = _member_from_row(updated_row)
        broker_store.record_event(
            connection,
            actor_uid=actor_uid,
            action="member_status_changed",
            detail={
                "member_id": member_id,
                "from": current_status,
                "to": target,
                "reason": reason,
                "access_version": member.access_version,
            },
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise
    return {
        "member_id": member_id,
        "status": member.status,
        "access_version": member.access_version,
        "valid_until": member.valid_until,
    }


def bump_access_version(connection: sqlite3.Connection, member_id: str) -> int:
    connection.execute(
        "UPDATE members SET access_version = access_version + 1, updated_at = ? "
        "WHERE member_id = ?",
        (_now(None), member_id),
    )
    row = connection.execute(
        "SELECT access_version FROM members WHERE member_id = ?", (member_id,)
    ).fetchone()
    return int(row["access_version"]) if row else 0


def check_access(
    connection: sqlite3.Connection,
    uid: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Unified authorization decision for one UID (§6.1/§6.3)."""
    member = get_member_by_uid(connection, uid)
    if member is None:
        return {
            "allowed": False,
            "reason": "no_member_record",
            "member_id": "",
            "status": "",
            "valid_until": "",
            "access_version": 0,
        }
    if not is_valid_now(member, now=now):
        reason = "expired" if member.status == "active" else f"status:{member.status}"
        return {
            "allowed": False,
            "reason": reason,
            "member_id": member.member_id,
            "status": member.status,
            "valid_until": member.valid_until,
            "access_version": member.access_version,
        }
    return {
        "allowed": True,
        "reason": "",
        "member_id": member.member_id,
        "status": member.status,
        "valid_until": member.valid_until,
        "access_version": member.access_version,
    }


def member_valid_until(connection: sqlite3.Connection, uid: int) -> str:
    member = get_member_by_uid(connection, uid)
    return member.valid_until if member is not None else ""


# ---------------------------------------------------------------------------
# Member keys
# ---------------------------------------------------------------------------


def list_keys(connection: sqlite3.Connection, member_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM member_keys WHERE member_id = ? ORDER BY created_at",
        (member_id,),
    ).fetchall()
    return [
        {
            "key_id": str(row["key_id"]),
            "fingerprint": str(row["fingerprint"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "revoked_at": str(row["revoked_at"]),
            "public_key": str(row["public_key"]),
        }
        for row in rows
    ]


def active_keys(connection: sqlite3.Connection, uid: int) -> list[str]:
    member = get_member_by_uid(connection, uid)
    if member is None:
        return []
    rows = connection.execute(
        "SELECT public_key FROM member_keys "
        "WHERE member_id = ? AND status = 'active' ORDER BY created_at",
        (member.member_id,),
    ).fetchall()
    return [str(row["public_key"]) for row in rows]


def add_key(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    public_key: str,
    fingerprint: str,
    now: datetime | None = None,
) -> str:
    key_id = uuid.uuid4().hex
    timestamp = _now(now)
    own = not connection.in_transaction
    try:
        if own:
            connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT member_id FROM member_keys WHERE fingerprint = ? AND status = 'active'",
            (fingerprint,),
        ).fetchone()
        if existing is not None:
            raise Conflict("该公钥已登记给其他成员；如需转移请联系管理员")
        connection.execute(
            """
            INSERT INTO member_keys(
                key_id, member_id, public_key, fingerprint, status, created_at
            ) VALUES (?, ?, ?, ?, 'active', ?)
            """,
            (key_id, member_id, public_key, fingerprint, timestamp),
        )
        bump_access_version(connection, member_id)
        broker_store.record_event(
            connection,
            actor_uid=None,
            action="member_key_added",
            detail={"member_id": member_id, "fingerprint": fingerprint},
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise
    return key_id


def revoke_key(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    key_id: str,
    actor_uid: int | None,
    now: datetime | None = None,
) -> None:
    timestamp = _now(now)
    own = not connection.in_transaction
    try:
        if own:
            connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM member_keys WHERE key_id = ? AND member_id = ?",
            (key_id, member_id),
        ).fetchone()
        if row is None:
            raise NotFound(f"key not found: {key_id}")
        if str(row["status"]) != "active":
            raise Conflict(f"key {key_id} is not active")
        active_left = connection.execute(
            "SELECT COUNT(*) FROM member_keys "
            "WHERE member_id = ? AND status = 'active' AND key_id != ?",
            (member_id, key_id),
        ).fetchone()[0]
        if int(active_left) == 0:
            raise Conflict(
                "这是最后一个有效公钥；移除后你将无法登录。请先添加新密钥，或联系管理员走恢复流程"
            )
        connection.execute(
            "UPDATE member_keys SET status = 'revoked', revoked_at = ? WHERE key_id = ?",
            (timestamp, key_id),
        )
        bump_access_version(connection, member_id)
        broker_store.record_event(
            connection,
            actor_uid=actor_uid,
            action="member_key_revoked",
            detail={"member_id": member_id, "fingerprint": str(row["fingerprint"])},
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise


def revoke_all_keys(
    connection: sqlite3.Connection,
    member_id: str,
    *,
    actor_uid: int | None,
    reason: str = "",
    now: datetime | None = None,
) -> int:
    """Revoke every active key (membership revocation, §8.4).

    Unlike a member-initiated self-revoke, this intentionally bypasses the
    "last key" guard: an administrator revoking access must not be blocked by
    the member's inability to log in afterwards.
    """
    timestamp = _now(now)
    own = not connection.in_transaction
    try:
        if own:
            connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT key_id FROM member_keys WHERE member_id = ? AND status = 'active'",
            (member_id,),
        ).fetchall()
        connection.execute(
            "UPDATE member_keys SET status = 'revoked', revoked_at = ? "
            "WHERE member_id = ? AND status = 'active'",
            (timestamp, member_id),
        )
        if rows:
            bump_access_version(connection, member_id)
        broker_store.record_event(
            connection,
            actor_uid=actor_uid,
            action="member_keys_revoked_all",
            detail={
                "member_id": member_id,
                "count": len(rows),
                "reason": reason[:500],
            },
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise
    return len(rows)


def set_member_keys(
    connection: sqlite3.Connection,
    member_id: str,
    keys: list[dict[str, str]],
    *,
    now: datetime | None = None,
) -> None:
    """Replace the active key set (provisioning projection, §8.3)."""
    timestamp = _now(now)
    own = not connection.in_transaction
    try:
        if own:
            connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE member_keys SET status = 'revoked', revoked_at = ? "
            "WHERE member_id = ? AND status = 'active'",
            (timestamp, member_id),
        )
        for key in keys:
            existing = connection.execute(
                "SELECT key_id, member_id FROM member_keys WHERE fingerprint = ? "
                "AND status = 'active'",
                (key["fingerprint"],),
            ).fetchone()
            if existing is not None:
                if str(existing["member_id"]) != member_id:
                    raise Conflict("该公钥已登记给其他成员；如需转移请联系管理员")
                connection.execute(
                    "UPDATE member_keys SET status = 'active', revoked_at = '' WHERE key_id = ?",
                    (str(existing["key_id"]),),
                )
                continue
            connection.execute(
                """
                INSERT INTO member_keys(
                    key_id, member_id, public_key, fingerprint, status, created_at
                ) VALUES (?, ?, ?, ?, 'active', ?)
                """,
                (uuid.uuid4().hex, member_id, key["public_key"], key["fingerprint"], timestamp),
            )
        bump_access_version(connection, member_id)
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise


# ---------------------------------------------------------------------------
# Membership operations (idempotent provisioning records, §8.1)
# ---------------------------------------------------------------------------


def create_operation(
    connection: sqlite3.Connection,
    *,
    action: str,
    frozen_input: dict[str, Any],
    member_id: str | None = None,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> tuple[str, bool]:
    """Create an operation record; returns (operation_id, created).

    An existing operation with the same idempotency key is returned unchanged
    (§7.3: retrying an approval must not create a second provisioning chain).
    """
    if idempotency_key:
        existing = connection.execute(
            "SELECT operation_id FROM member_operations WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return str(existing["operation_id"]), False
    operation_id = uuid.uuid4().hex
    timestamp = _now(now)
    own = not connection.in_transaction
    try:
        if own:
            connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO member_operations(
                operation_id, idempotency_key, member_id, request_id, action,
                frozen_input, stage, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '', 'pending', ?, ?)
            """,
            (
                operation_id,
                idempotency_key,
                member_id,
                request_id,
                action,
                json.dumps(frozen_input, sort_keys=True, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        broker_store.record_event(
            connection,
            actor_uid=None,
            action="membership_operation_created",
            detail={
                "operation_id": operation_id,
                "action": action,
                "request_id": request_id,
            },
            now=now,
        )
        if own:
            connection.commit()
    except Exception:
        if own:
            connection.rollback()
        raise
    return operation_id, True


def get_operation(connection: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM member_operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"operation not found: {operation_id}")
    return {
        "operation_id": str(row["operation_id"]),
        "member_id": row["member_id"],
        "request_id": row["request_id"],
        "action": str(row["action"]),
        "status": str(row["status"]),
        "stage": str(row["stage"]),
        "attempts": int(row["attempts"]),
        "last_error": str(row["last_error"]),
        "frozen_input": json.loads(str(row["frozen_input"])),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def update_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    error: str = "",
    now: datetime | None = None,
) -> None:
    timestamp = _now(now)
    sets = ["updated_at = ?"]
    params: list[Any] = [timestamp]
    if status is not None:
        sets.append("status = ?")
        params.append(status)
        if status == "failed":
            sets.append("attempts = attempts + 1")
            sets.append("last_error = ?")
            params.append(error)
    if stage is not None:
        sets.append("stage = ?")
        params.append(stage)
    params.append(operation_id)
    connection.execute(
        f"UPDATE member_operations SET {', '.join(sets)} WHERE operation_id = ?",
        params,
    )


def list_operations(
    connection: sqlite3.Connection,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM member_operations ORDER BY created_at DESC, operation_id LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "operation_id": str(row["operation_id"]),
            "member_id": row["member_id"],
            "request_id": row["request_id"],
            "action": str(row["action"]),
            "status": str(row["status"]),
            "stage": str(row["stage"]),
            "attempts": int(row["attempts"]),
            "last_error": str(row["last_error"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Membership applications (frozen decision record on the broker side)
# ---------------------------------------------------------------------------


def record_application_decision(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    version: str,
    snapshot: dict[str, Any],
    status: str,
    requested_username: str,
    display_name: str,
    reviewer_uid: int,
    review_note: str,
    internal_note: str,
    operation_id: str | None,
    now: datetime | None = None,
) -> None:
    timestamp = _now(now)
    connection.execute(
        """
        INSERT INTO membership_applications(
            request_id, version, snapshot, status, requested_username,
            display_name, reviewer_uid, review_note, internal_note,
            operation_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(request_id) DO UPDATE SET
            status = excluded.status,
            reviewer_uid = excluded.reviewer_uid,
            review_note = excluded.review_note,
            internal_note = excluded.internal_note,
            operation_id = excluded.operation_id,
            updated_at = excluded.updated_at
        """,
        (
            request_id,
            version,
            json.dumps(snapshot, sort_keys=True, ensure_ascii=False),
            status,
            requested_username,
            display_name,
            reviewer_uid,
            review_note,
            internal_note,
            operation_id,
            timestamp,
            timestamp,
        ),
    )


def get_application_decision(
    connection: sqlite3.Connection,
    request_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM membership_applications WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "request_id": str(row["request_id"]),
        "version": str(row["version"]),
        "snapshot": json.loads(str(row["snapshot"])),
        "status": str(row["status"]),
        "requested_username": str(row["requested_username"]),
        "display_name": str(row["display_name"]),
        "reviewer_uid": row["reviewer_uid"],
        "review_note": str(row["review_note"]),
        "internal_note": str(row["internal_note"]),
        "operation_id": row["operation_id"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def member_key_fingerprint(public_key: str) -> str:
    return key_fingerprint(public_key)
