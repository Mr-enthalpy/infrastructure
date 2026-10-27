"""Portal-owned SQLite state: local accounts, sessions, registration links, audit.

The portal database is intentionally separate from the broker state database and
the low-privilege intake database.  It never stores a plaintext password, TOTP
secret, activation code, or session token; only Argon2id hashes and SHA-256
digests of the one-time secrets are persisted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PORTAL_ROLES = ("member", "admin")
PORTAL_STATUSES = ("pending_activation", "active", "suspended", "revoked")
REGISTRATION_STATUSES = ("pending", "approved", "rejected")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


SCHEMA = """
CREATE TABLE IF NOT EXISTS portal_users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL CHECK (role IN ('member', 'admin')),
    status TEXT NOT NULL CHECK (
        status IN ('pending_activation', 'active', 'suspended', 'revoked')
    ),
    password_hash TEXT NOT NULL DEFAULT '',
    totp_secret_enc TEXT NOT NULL DEFAULT '',
    pending_totp_enc TEXT NOT NULL DEFAULT '',
    totp_last_counter INTEGER NOT NULL DEFAULT 0,
    member_id TEXT NOT NULL DEFAULT '',
    linux_username TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    activated_at TEXT NOT NULL DEFAULT '',
    last_login_at TEXT NOT NULL DEFAULT '',
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_portal_users_member
    ON portal_users(member_id) WHERE member_id != '';

CREATE TABLE IF NOT EXISTS portal_activation_codes (
    code_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES portal_users(user_id) ON DELETE CASCADE,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_portal_activation_user
    ON portal_activation_codes(user_id, used_at);

CREATE TABLE IF NOT EXISTS portal_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES portal_users(user_id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_portal_sessions_user ON portal_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_portal_sessions_expiry ON portal_sessions(expires_at);

CREATE TABLE IF NOT EXISTS portal_registrations (
    registration_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    affiliation TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_by TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    member_id TEXT NOT NULL DEFAULT '',
    portal_user_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_portal_registrations_username
    ON portal_registrations(username, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_portal_registrations_pending_username
    ON portal_registrations(username COLLATE NOCASE) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_portal_registrations_request
    ON portal_registrations(request_id);

CREATE TABLE IF NOT EXISTS portal_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_user_id TEXT NOT NULL DEFAULT '',
    actor_username TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_portal_audit_ts ON portal_audit(id DESC);
"""


def init_portal_db(path: str | Path) -> sqlite3.Connection:
    connection = connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def utcnow() -> datetime:
    return datetime.now(UTC)


def _now(now: datetime | None = None) -> str:
    return (now or utcnow()).isoformat()


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _user_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "user_id": str(row["user_id"]),
        "username": str(row["username"]),
        "role": str(row["role"]),
        "status": str(row["status"]),
        "password_hash": str(row["password_hash"]),
        "totp_secret_enc": str(row["totp_secret_enc"]),
        "pending_totp_enc": str(row["pending_totp_enc"]),
        "totp_last_counter": int(row["totp_last_counter"]),
        "member_id": str(row["member_id"]),
        "linux_username": str(row["linux_username"]),
        "display_name": str(row["display_name"]),
        "contact": str(row["contact"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "activated_at": str(row["activated_at"]),
        "last_login_at": str(row["last_login_at"]),
        "failed_login_count": int(row["failed_login_count"]),
        "locked_until": str(row["locked_until"]),
        "version": int(row["version"]),
    }


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in user.items()
        if key
        not in {
            "password_hash",
            "totp_secret_enc",
            "pending_totp_enc",
            "totp_last_counter",
        }
    }


def get_user_by_username(connection: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM portal_users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()
    return _user_from_row(row) if row is not None else None


def get_user(connection: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM portal_users WHERE user_id = ?", (user_id,)).fetchone()
    return _user_from_row(row) if row is not None else None


def get_user_by_member_id(connection: sqlite3.Connection, member_id: str) -> dict[str, Any] | None:
    if not member_id:
        return None
    row = connection.execute(
        "SELECT * FROM portal_users WHERE member_id = ?", (member_id,)
    ).fetchone()
    return _user_from_row(row) if row is not None else None


def list_users(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM portal_users ORDER BY created_at ASC, user_id"
    ).fetchall()
    return [_public_user(_user_from_row(row)) for row in rows]


def create_user(
    connection: sqlite3.Connection,
    *,
    username: str,
    role: str = "member",
    status: str = "pending_activation",
    password_hash: str = "",
    totp_secret_enc: str = "",
    member_id: str = "",
    linux_username: str = "",
    display_name: str = "",
    contact: str = "",
    now: datetime | None = None,
) -> str:
    if role not in PORTAL_ROLES:
        raise ValueError(f"unknown portal role: {role}")
    if status not in PORTAL_STATUSES:
        raise ValueError(f"unknown portal status: {status}")
    user_id = uuid.uuid4().hex
    timestamp = _now(now)
    connection.execute(
        """
        INSERT INTO portal_users(
            user_id, username, role, status, password_hash, totp_secret_enc,
            member_id, linux_username, display_name, contact, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            username,
            role,
            status,
            password_hash,
            totp_secret_enc,
            member_id,
            linux_username,
            display_name,
            contact,
            timestamp,
            timestamp,
        ),
    )
    connection.commit()
    return user_id


def update_user_status(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    status: str,
    now: datetime | None = None,
) -> None:
    if status not in PORTAL_STATUSES:
        raise ValueError(f"unknown portal status: {status}")
    connection.execute(
        "UPDATE portal_users SET status = ?, version = version + 1, updated_at = ? "
        "WHERE user_id = ?",
        (status, _now(now), user_id),
    )
    connection.commit()


def set_pending_totp(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    pending_totp_enc: str,
    now: datetime | None = None,
) -> None:
    connection.execute(
        "UPDATE portal_users SET pending_totp_enc = ?, updated_at = ? WHERE user_id = ?",
        (pending_totp_enc, _now(now), user_id),
    )
    connection.commit()


def activate_user(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    password_hash: str,
    totp_secret_enc: str,
    totp_counter: int = 0,
    now: datetime | None = None,
) -> None:
    timestamp = _now(now)
    cursor = connection.execute(
        """
        UPDATE portal_users
        SET password_hash = ?, totp_secret_enc = ?, pending_totp_enc = '',
            status = 'active', activated_at = ?, updated_at = ?,
            version = version + 1, failed_login_count = 0, locked_until = '',
            totp_last_counter = ?
        WHERE user_id = ? AND status = 'pending_activation'
        """,
        (
            password_hash,
            totp_secret_enc,
            timestamp,
            timestamp,
            totp_counter,
            user_id,
        ),
    )
    if cursor.rowcount != 1:
        connection.rollback()
        raise ValueError("account is not awaiting activation")
    connection.commit()


def set_totp_counter(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    counter: int,
    now: datetime | None = None,
) -> bool:
    """Consume a TOTP counter exactly once.

    The conditional UPDATE is the authorization check: two concurrent requests
    that verified the same code both try to advance the stored counter, but only
    the first can match ``totp_last_counter < counter``.  Callers must treat a
    false return as a replay and reject the request.
    """

    cursor = connection.execute(
        """
        UPDATE portal_users
        SET totp_last_counter = ?, updated_at = ?
        WHERE user_id = ? AND totp_last_counter < ?
        """,
        (counter, _now(now), user_id, counter),
    )
    connection.commit()
    return cursor.rowcount == 1


def set_password(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    password_hash: str,
    now: datetime | None = None,
) -> None:
    connection.execute(
        "UPDATE portal_users SET password_hash = ?, version = version + 1, updated_at = ? "
        "WHERE user_id = ?",
        (password_hash, _now(now), user_id),
    )
    connection.commit()


def update_user_role(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    role: str,
    now: datetime | None = None,
) -> None:
    """Change a local portal role.  Only the CLI/broker calls this path."""

    if role not in PORTAL_ROLES:
        raise ValueError(f"unknown portal role: {role}")
    cursor = connection.execute(
        "UPDATE portal_users SET role = ?, version = version + 1, updated_at = ? WHERE user_id = ?",
        (role, _now(now), user_id),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"portal user not found: {user_id}")
    connection.commit()


def record_login_success(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    now: datetime | None = None,
) -> None:
    timestamp = _now(now)
    connection.execute(
        """
        UPDATE portal_users
        SET failed_login_count = 0, locked_until = '', last_login_at = ?,
            updated_at = ?
        WHERE user_id = ?
        """,
        (timestamp, timestamp, user_id),
    )
    connection.commit()


def record_login_failure(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    lock_after: int,
    lock_seconds: int,
    now: datetime | None = None,
) -> int:
    current = get_user(connection, user_id)
    if current is None:
        return 0
    failures = current["failed_login_count"] + 1
    locked_until = ""
    if lock_after > 0 and failures >= lock_after:
        locked_until = ((now or utcnow()) + timedelta(seconds=lock_seconds)).isoformat()
    connection.execute(
        "UPDATE portal_users SET failed_login_count = ?, locked_until = ?, updated_at = ? "
        "WHERE user_id = ?",
        (failures, locked_until, _now(now), user_id),
    )
    connection.commit()
    return failures


def reset_login_lock(connection: sqlite3.Connection, user_id: str) -> None:
    connection.execute(
        "UPDATE portal_users SET failed_login_count = 0, locked_until = '' WHERE user_id = ?",
        (user_id,),
    )
    connection.commit()


def create_activation_code(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    ttl_hours: int,
    created_by: str = "",
    now: datetime | None = None,
) -> dict[str, str]:
    code = uuid.uuid4().hex + uuid.uuid4().hex
    code_hash = _hash(code)
    current = now or utcnow()
    expires = (current + timedelta(hours=ttl_hours)).isoformat()
    connection.execute(
        "DELETE FROM portal_activation_codes WHERE user_id = ? AND used_at = ''",
        (user_id,),
    )
    # A newly issued code invalidates any in-flight activation page, whose
    # short-lived signed token points at the previous pending TOTP secret.
    connection.execute(
        "UPDATE portal_users SET pending_totp_enc = '', updated_at = ? WHERE user_id = ?",
        (current.isoformat(), user_id),
    )
    connection.execute(
        "INSERT INTO portal_activation_codes("
        "code_hash, user_id, created_by, created_at, expires_at"
        ") VALUES (?, ?, ?, ?, ?)",
        (code_hash, user_id, created_by, current.isoformat(), expires),
    )
    connection.commit()
    return {"code": code, "expires_at": expires}


def get_activation(
    connection: sqlite3.Connection,
    code: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not code:
        return None
    row = connection.execute(
        "SELECT * FROM portal_activation_codes WHERE code_hash = ?",
        (_hash(code),),
    ).fetchone()
    if row is None:
        return None
    if str(row["used_at"]):
        return None
    if _now(now) >= str(row["expires_at"]):
        return None
    user = get_user(connection, str(row["user_id"]))
    if user is None:
        return None
    return {
        "code_hash": str(row["code_hash"]),
        "user": user,
        "expires_at": str(row["expires_at"]),
    }


def consume_activation(
    connection: sqlite3.Connection,
    code_hash: str,
    *,
    now: datetime | None = None,
) -> bool:
    timestamp = _now(now)
    cursor = connection.execute(
        "UPDATE portal_activation_codes SET used_at = ? "
        "WHERE code_hash = ? AND used_at = '' AND expires_at > ?",
        (timestamp, code_hash, timestamp),
    )
    connection.commit()
    return cursor.rowcount == 1


def create_session(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    ttl_hours: int,
    csrf_token: str,
    ip: str = "",
    user_agent: str = "",
    now: datetime | None = None,
) -> dict[str, str]:
    token = uuid.uuid4().hex + uuid.uuid4().hex
    current = now or utcnow()
    expires = (current + timedelta(hours=ttl_hours)).isoformat()
    connection.execute(
        """
        INSERT INTO portal_sessions(
            token_hash, user_id, csrf_token, created_at, last_seen_at,
            expires_at, ip, user_agent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _hash(token),
            user_id,
            csrf_token,
            current.isoformat(),
            current.isoformat(),
            expires,
            ip,
            user_agent[:300],
        ),
    )
    connection.commit()
    return {"token": token, "csrf_token": csrf_token, "expires_at": expires}


def get_session(
    connection: sqlite3.Connection,
    token: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not token:
        return None
    row = connection.execute(
        "SELECT * FROM portal_sessions WHERE token_hash = ?", (_hash(token),)
    ).fetchone()
    if row is None:
        return None
    if _now(now) >= str(row["expires_at"]):
        connection.execute("DELETE FROM portal_sessions WHERE token_hash = ?", (_hash(token),))
        connection.commit()
        return None
    user = get_user(connection, str(row["user_id"]))
    if user is None or user["status"] != "active":
        connection.execute("DELETE FROM portal_sessions WHERE token_hash = ?", (_hash(token),))
        connection.commit()
        return None
    return {
        "token_hash": str(row["token_hash"]),
        "user": user,
        "csrf_token": str(row["csrf_token"]),
        "created_at": str(row["created_at"]),
        "last_seen_at": str(row["last_seen_at"]),
        "expires_at": str(row["expires_at"]),
        "ip": str(row["ip"]),
        "user_agent": str(row["user_agent"]),
    }


def touch_session(
    connection: sqlite3.Connection,
    token: str,
    *,
    ttl_hours: int,
    now: datetime | None = None,
) -> None:
    current = now or utcnow()
    expires = (current + timedelta(hours=ttl_hours)).isoformat()
    connection.execute(
        "UPDATE portal_sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
        (current.isoformat(), expires, _hash(token)),
    )
    connection.commit()


def delete_session(connection: sqlite3.Connection, token: str) -> None:
    connection.execute("DELETE FROM portal_sessions WHERE token_hash = ?", (_hash(token),))
    connection.commit()


def delete_user_sessions(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    except_token: str = "",
) -> None:
    if except_token:
        connection.execute(
            "DELETE FROM portal_sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, _hash(except_token)),
        )
    else:
        connection.execute("DELETE FROM portal_sessions WHERE user_id = ?", (user_id,))
    connection.commit()


def create_registration(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    username: str,
    display_name: str,
    affiliation: str,
    contact: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    registration_id = uuid.uuid4().hex
    timestamp = _now(now)
    connection.execute(
        """
        INSERT INTO portal_registrations(
            registration_id, request_id, username, display_name, affiliation,
            contact, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            registration_id,
            request_id,
            username,
            display_name,
            affiliation,
            contact,
            timestamp,
            timestamp,
        ),
    )
    connection.commit()
    return get_registration(connection, registration_id) or {}


def get_registration(connection: sqlite3.Connection, registration_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM portal_registrations WHERE registration_id = ?",
        (registration_id,),
    ).fetchone()
    return _registration_from_row(row) if row is not None else None


def get_registration_by_request(
    connection: sqlite3.Connection, request_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM portal_registrations WHERE request_id = ?", (request_id,)
    ).fetchone()
    return _registration_from_row(row) if row is not None else None


def find_pending_registration(
    connection: sqlite3.Connection, username: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM portal_registrations "
        "WHERE username = ? COLLATE NOCASE AND status IN ('pending', 'approved') "
        "ORDER BY created_at DESC LIMIT 1",
        (username,),
    ).fetchone()
    return _registration_from_row(row) if row is not None else None


def list_registrations(
    connection: sqlite3.Connection, *, status: str | None = None
) -> list[dict[str, Any]]:
    if status:
        rows = connection.execute(
            "SELECT * FROM portal_registrations WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM portal_registrations ORDER BY created_at DESC"
        ).fetchall()
    return [_registration_from_row(row) for row in rows]


def update_registration(
    connection: sqlite3.Connection,
    registration_id: str,
    *,
    status: str | None = None,
    reviewed_by: str | None = None,
    review_note: str | None = None,
    member_id: str | None = None,
    portal_user_id: str | None = None,
    now: datetime | None = None,
) -> None:
    current = get_registration(connection, registration_id)
    if current is None:
        raise ValueError("registration not found")
    values = {
        "status": current["status"],
        "reviewed_by": current["reviewed_by"],
        "review_note": current["review_note"],
        "member_id": current["member_id"],
        "portal_user_id": current["portal_user_id"],
    }
    if status is not None:
        if status not in REGISTRATION_STATUSES:
            raise ValueError(f"unknown registration status: {status}")
        values["status"] = status
    if reviewed_by is not None:
        values["reviewed_by"] = reviewed_by
    if review_note is not None:
        values["review_note"] = review_note
    if member_id is not None:
        values["member_id"] = member_id
    if portal_user_id is not None:
        values["portal_user_id"] = portal_user_id
    connection.execute(
        """
        UPDATE portal_registrations
        SET status = ?, reviewed_by = ?, review_note = ?, member_id = ?,
            portal_user_id = ?, updated_at = ?
        WHERE registration_id = ?
        """,
        (
            values["status"],
            values["reviewed_by"],
            values["review_note"],
            values["member_id"],
            values["portal_user_id"],
            _now(now),
            registration_id,
        ),
    )
    connection.commit()


def _registration_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "registration_id": str(row["registration_id"]),
        "request_id": str(row["request_id"]),
        "username": str(row["username"]),
        "display_name": str(row["display_name"]),
        "affiliation": str(row["affiliation"]),
        "contact": str(row["contact"]),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "reviewed_by": str(row["reviewed_by"]),
        "review_note": str(row["review_note"]),
        "member_id": str(row["member_id"]),
        "portal_user_id": str(row["portal_user_id"]),
    }


def create_pending_account(
    connection: sqlite3.Connection,
    *,
    username: str,
    display_name: str,
    contact: str,
    member_id: str,
    linux_username: str,
    registration_id: str = "",
    activation_ttl_hours: int,
    role: str | None = None,
    created_by: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create (or reuse) a pending portal account and return a one-time code.

    ``role`` is only supplied by the administrator CLI.  In particular, normal
    intake approval must not demote an existing pending administrator when it
    reuses the same account.
    """

    if role is not None and role not in PORTAL_ROLES:
        raise ValueError(f"unknown portal role: {role}")
    current = get_user_by_username(connection, username)
    if current is None:
        user_id = create_user(
            connection,
            username=username,
            role=role or "member",
            status="pending_activation",
            member_id=member_id,
            linux_username=linux_username,
            display_name=display_name,
            contact=contact,
            now=now,
        )
    else:
        if current["status"] not in {"pending_activation"}:
            raise ValueError("portal account is not awaiting activation")
        user_id = current["user_id"]
        new_role = role or current["role"]
        connection.execute(
            "UPDATE portal_users SET role = ?, member_id = ?, linux_username = ?, "
            "display_name = ?, contact = ?, version = version + 1, updated_at = ? "
            "WHERE user_id = ?",
            (new_role, member_id, linux_username, display_name, contact, _now(now), user_id),
        )
        connection.commit()
    code = create_activation_code(
        connection,
        user_id,
        ttl_hours=activation_ttl_hours,
        created_by=created_by,
        now=now,
    )
    if registration_id:
        update_registration(
            connection,
            registration_id,
            status="approved",
            member_id=member_id,
            portal_user_id=user_id,
            now=now,
        )
    user = get_user(connection, user_id)
    return {
        "user_id": user_id,
        "username": username,
        "activation_code": code["code"],
        "expires_at": code["expires_at"],
        "user": _public_user(user) if user is not None else None,
    }


def record_audit(
    connection: sqlite3.Connection,
    *,
    action: str,
    actor_user_id: str = "",
    actor_username: str = "",
    target: str = "",
    detail: dict[str, Any] | None = None,
    ip: str = "",
    user_agent: str = "",
    now: datetime | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO portal_audit(
            ts, actor_user_id, actor_username, action, target, detail, ip, user_agent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _now(now),
            actor_user_id,
            actor_username,
            action,
            target,
            json.dumps(detail or {}, sort_keys=True, ensure_ascii=False),
            ip,
            user_agent[:300],
        ),
    )
    connection.commit()


def list_audit(connection: sqlite3.Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM portal_audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            detail = json.loads(str(row["detail"]))
        except json.JSONDecodeError:
            detail = {}
        result.append(
            {
                "id": int(row["id"]),
                "ts": str(row["ts"]),
                "actor_user_id": str(row["actor_user_id"]),
                "actor_username": str(row["actor_username"]),
                "action": str(row["action"]),
                "target": str(row["target"]),
                "detail": detail if isinstance(detail, dict) else {},
                "ip": str(row["ip"]),
                "user_agent": str(row["user_agent"]),
            }
        )
    return result
