"""Low-privilege intake storage: invitations, applications, receipts (§5.3).

This store belongs to the unprivileged intake service. It never touches the
broker's member/allocation/GPU databases; the broker only ever READS approved
frozen snapshots from here when an administrator approves (§9.2: the broker
pulls, external callers cannot push into privileged paths).

Secrets: invitation codes and receipt secrets are stored as SHA-256 hashes
only. The plaintext is shown once to the invitee/applicant and never logged.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..membership.models import parse_public_key

REQUEST_LIMITS = {
    "max_pending_requests": 200,
    "max_display_name": 80,
    "max_username": 32,
    "max_affiliation": 160,
    "max_purpose": 600,
    "max_contact": 160,
    "max_nickname": 60,
    "max_keys": 3,
}


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


SCHEMA = """
CREATE TABLE IF NOT EXISTS invitations (
    invite_id TEXT PRIMARY KEY,
    secret_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_by_request TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    receipt_hash TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (status IN ('pending_review', 'needs_info', 'approved', 'rejected', 'cancelled')),
    public_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init_intake_db(path: str | Path) -> sqlite3.Connection:
    connection = connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_invitation(
    connection: sqlite3.Connection,
    *,
    created_by: str,
    validity_hours: int = 168,
) -> dict[str, str]:
    """Create one invitation; the plaintext secret is returned ONCE."""
    invite_id = uuid.uuid4().hex
    secret = secrets.token_urlsafe(24)
    timestamp = _now()
    expires = (datetime.now(UTC) + timedelta(hours=validity_hours)).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """
        INSERT INTO invitations(
            invite_id, secret_hash, created_by, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (invite_id, _hash(secret), created_by, timestamp, expires),
    )
    connection.commit()
    return {"invite_id": invite_id, "invite_secret": secret, "expires_at": expires}


def revoke_invitation(connection: sqlite3.Connection, invite_id: str) -> None:
    connection.execute("UPDATE invitations SET revoked = 1 WHERE invite_id = ?", (invite_id,))
    connection.commit()


def validate_invitation(
    connection: sqlite3.Connection,
    *,
    invite_id: str,
    invite_secret: str,
) -> bool:
    row = connection.execute(
        "SELECT * FROM invitations WHERE invite_id = ?", (invite_id,)
    ).fetchone()
    if row is None:
        return False
    if int(row["revoked"]) == 1:
        return False
    if str(row["secret_hash"]) != _hash(invite_secret):
        return False
    if _now() >= str(row["expires_at"]):
        return False
    if row["used_by_request"]:
        # Single-use invitations (§5.3); a used invitation is spent.
        return False
    return True


def normalize_application(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one application submission (§5.2, AC-10).

    Raises ValueError with a safe message; the raw input is never echoed in
    full (logs must not record submitted keys or secrets).
    """
    if not isinstance(raw, dict):
        raise ValueError("申请内容格式不正确")
    limits = REQUEST_LIMITS

    def field(name: str, limit: int, *, required: bool = True) -> str:
        value = raw.get(name, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"字段 {name} 格式不正确")
        value = value.strip()
        if required and not value:
            raise ValueError(f"缺少必填字段：{name}")
        if len(value) > limit:
            raise ValueError(f"字段 {name} 超长")
        return value

    display_name = field("display_name", limits["max_display_name"])
    username = field("username", limits["max_username"])
    affiliation = field("affiliation", limits["max_affiliation"])
    purpose = field("purpose", limits["max_purpose"])
    contact = field("contact", limits["max_contact"])
    nickname = field("nickname", limits["max_nickname"], required=False)
    public_contact = raw.get("public_contact", "")
    public_contact = "1" if public_contact in (True, "1", "true", "on") else ""
    agreed = raw.get("agree_terms", "")
    if agreed not in (True, "1", "true", "on"):
        raise ValueError("必须确认使用约定")
    keys_raw = raw.get("public_keys", [])
    if not isinstance(keys_raw, list) or not keys_raw:
        raise ValueError("至少提交一把 SSH 公钥")
    if len(keys_raw) > limits["max_keys"]:
        raise ValueError("公钥数量过多")
    keys = []
    for key_raw in keys_raw:
        if not isinstance(key_raw, str):
            raise ValueError("公钥格式不正确")
        try:
            normalized, fingerprint = parse_public_key(key_raw)
        except ValueError:
            raise ValueError(
                "公钥校验失败：请确认提交的是 .pub 公钥；"
                "若误提交私钥，请立即重新生成并撤销受影响凭据"
            ) from None
        keys.append({"public_key": normalized, "fingerprint": fingerprint})
    import re

    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
        raise ValueError("期望用户名格式不正确（小写字母开头，a-z0-9_-）")
    return {
        "display_name": display_name,
        "username": username,
        "affiliation": affiliation,
        "purpose": purpose,
        "contact": contact,
        "nickname": nickname,
        "public_contact": public_contact,
        "public_keys": keys,
    }


def submit_application(
    connection: sqlite3.Connection,
    *,
    invite_id: str,
    invite_secret: str,
    application: dict[str, Any],
) -> dict[str, str]:
    """Submit one application against a valid, unused invitation.

    Returns the request id and the PRIVATE receipt secret (shown once).
    Idempotent by invitation: one invitation yields exactly one request
    (§5.3); a retry with the same invitation returns the original request.
    """
    if not validate_invitation(connection, invite_id=invite_id, invite_secret=invite_secret):
        raise ValueError("邀请无效、已使用或已过期")
    normalized = normalize_application(application)
    existing = connection.execute(
        "SELECT request_id, receipt_hash FROM invitations "
        "LEFT JOIN requests ON requests.request_id = invitations.used_by_request "
        "WHERE invitations.invite_id = ?",
        (invite_id,),
    ).fetchone()
    if existing is not None and existing["request_id"]:
        return {"request_id": str(existing["request_id"]), "receipt_secret": ""}
    request_id = uuid.uuid4().hex
    receipt_secret = secrets.token_urlsafe(24)
    version = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    timestamp = _now()
    connection.execute("BEGIN IMMEDIATE")
    pending = connection.execute(
        "SELECT COUNT(*) FROM requests WHERE status IN ('pending_review', 'needs_info')"
    ).fetchone()[0]
    if int(pending) >= REQUEST_LIMITS["max_pending_requests"]:
        connection.rollback()
        raise ValueError("当前待审核申请过多，请稍后再提交")
    connection.execute(
        """
        INSERT INTO requests(
            request_id, receipt_hash, version, snapshot, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'pending_review', ?, ?)
        """,
        (
            request_id,
            _hash(receipt_secret),
            version,
            json.dumps(normalized, sort_keys=True, ensure_ascii=False),
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        "UPDATE invitations SET used_by_request = ? WHERE invite_id = ?",
        (request_id, invite_id),
    )
    connection.commit()
    return {"request_id": request_id, "receipt_secret": receipt_secret}


def request_status(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    receipt_secret: str,
) -> dict[str, Any]:
    """Private receipt lookup (AC-09): guessing ids yields nothing."""
    row = connection.execute(
        "SELECT * FROM requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None or str(row["receipt_hash"]) != _hash(receipt_secret):
        raise ValueError("查询失败：申请编号或回执不正确")
    return {
        "request_id": request_id,
        "status": str(row["status"]),
        "public_note": str(row["public_note"]),
        "version": str(row["version"]),
        "updated_at": str(row["updated_at"]),
    }


def cancel_request(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    receipt_secret: str,
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None or str(row["receipt_hash"]) != _hash(receipt_secret):
        raise ValueError("查询失败：申请编号或回执不正确")
    if str(row["status"]) not in ("pending_review", "needs_info"):
        raise ValueError("当前状态不可取消")
    timestamp = _now()
    connection.execute(
        "UPDATE requests SET status = 'cancelled', updated_at = ? WHERE request_id = ?",
        (timestamp, request_id),
    )
    connection.commit()
    return {"request_id": request_id, "status": "cancelled"}


def set_needs_info(
    connection: sqlite3.Connection,
    request_id: str,
    *,
    public_note: str,
) -> None:
    timestamp = _now()
    connection.execute(
        "UPDATE requests SET status = 'needs_info', public_note = ?, updated_at = ? "
        "WHERE request_id = ? AND status IN ('pending_review', 'needs_info')",
        (public_note, timestamp, request_id),
    )
    connection.commit()


def set_decision(
    connection: sqlite3.Connection,
    request_id: str,
    *,
    status: str,
    public_note: str = "",
) -> None:
    """Broker-side decision projection (approved/rejected); pull-only."""
    if status not in ("approved", "rejected"):
        raise ValueError("unsupported decision")
    timestamp = _now()
    connection.execute(
        "UPDATE requests SET status = ?, public_note = ?, updated_at = ? WHERE request_id = ?",
        (status, public_note, timestamp, request_id),
    )
    connection.commit()


def list_requests(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    if status:
        rows = connection.execute(
            "SELECT * FROM requests WHERE status = ? ORDER BY created_at ASC",
            (status,),
        ).fetchall()
    else:
        rows = connection.execute("SELECT * FROM requests ORDER BY created_at ASC").fetchall()
    result = []
    for row in rows:
        snapshot = json.loads(str(row["snapshot"]))
        result.append(
            {
                "request_id": str(row["request_id"]),
                "status": str(row["status"]),
                "version": str(row["version"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                # Review display fields (no contact secrets beyond what the
                # applicant explicitly provided for verification).
                "display_name": snapshot.get("display_name", ""),
                "username": snapshot.get("username", ""),
                "affiliation": snapshot.get("affiliation", ""),
                "purpose": snapshot.get("purpose", ""),
                "contact": snapshot.get("contact", ""),
                "nickname": snapshot.get("nickname", ""),
                "public_contact": snapshot.get("public_contact", ""),
                "keys": [
                    {"fingerprint": key["fingerprint"], "public_key": key["public_key"]}
                    for key in snapshot.get("public_keys", [])
                ],
            }
        )
    return result


def get_request(
    connection: sqlite3.Connection,
    request_id: str,
) -> dict[str, Any] | None:
    for request in list_requests(connection):
        if request["request_id"] == request_id:
            return request
    return None
