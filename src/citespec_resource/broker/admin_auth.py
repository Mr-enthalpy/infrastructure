"""Broker-owned administrator credentials and one-use attestations.

The portal database is writable by the portal service account, so its role,
password, and TOTP records cannot authorize a broker mutation.  This module
keeps a separate credential record in the root-owned broker database and issues
short-lived capabilities (attestations) only after a fresh password + TOTP
check.  An attestation is bound to the broker administrator, the portal session
that requested it, the exact action, the exact target, and a nonce.  Consumption
is a single ``BEGIN IMMEDIATE`` transaction, so a token cannot be replayed or
used concurrently by two requests.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..portal import security as portal_security

ATTESTABLE_ACTIONS = frozenset(
    {
        "portal_requests_approve",
        "portal_requests_reject",
        "portal_members_suspend",
        "portal_members_resume",
        "portal_members_revoke",
    }
)
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class AdminAuthDenied(Exception):
    """Credential or attestation verification failed; callers must fail closed."""


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _validate_action(action: str) -> str:
    if action not in ATTESTABLE_ACTIONS:
        raise ValueError("action is not eligible for administrator attestation")
    return action


def _validate_target(target: str) -> str:
    if not isinstance(target, str) or not target:
        raise ValueError("attestation target must be a non-empty string")
    if len(target) > 256 or target.strip() != target:
        raise ValueError("attestation target is not valid")
    if any(ord(character) < 0x20 for character in target):
        raise ValueError("attestation target is not valid")
    return target


def _load_key(path: str) -> bytes:
    """Load the root-only key without ever creating or chmod-ing host paths.

    The installer provisions the key before the Broker starts.  A missing key is
    therefore a fail-closed condition, not a reason to create a file or change
    the permissions of ``/etc/citespec-resource`` while the Broker is running.
    """

    key_path = Path(path)
    if os.name == "posix":
        try:
            metadata = key_path.stat()
        except OSError as exc:
            raise AdminAuthDenied("administrator authentication key is unavailable") from exc
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise AdminAuthDenied(
                "administrator authentication key must be owned by the broker user with mode 0600"
            )
    try:
        return portal_security.load_secret(key_path, create=False)
    except portal_security.PortalSecurityError as exc:
        raise AdminAuthDenied("administrator authentication key is unavailable") from exc


def enroll_admin(
    connection: sqlite3.Connection,
    *,
    username: str,
    password: str,
    key_path: str,
    now: datetime,
) -> dict[str, Any]:
    """Create or replace the independent broker credential for an admin.

    The TOTP secret is returned exactly once to the root CLI.  It is stored
    encrypted with the broker-only key; the portal service cannot read it.
    """

    normalized = portal_security.validate_username(username)
    password_hash = portal_security.hash_password(password, username=normalized)
    totp_secret = portal_security.new_totp_secret()
    sealed_secret = portal_security.seal(_load_key(key_path), totp_secret)
    timestamp = _timestamp(now)
    with connection:
        connection.execute(
            """
            INSERT INTO portal_admin_credentials(
                username, password_hash, totp_secret_enc, enabled,
                totp_last_counter, failed_attempts, locked_until,
                created_at, updated_at, last_used_at
            ) VALUES (?, ?, ?, 1, 0, 0, '', ?, ?, '')
            ON CONFLICT(username) DO UPDATE SET
                password_hash = excluded.password_hash,
                totp_secret_enc = excluded.totp_secret_enc,
                enabled = 1,
                totp_last_counter = 0,
                failed_attempts = 0,
                locked_until = '',
                updated_at = excluded.updated_at,
                last_used_at = ''
            """,
            (normalized, password_hash, sealed_secret, timestamp, timestamp),
        )
        connection.execute(
            "DELETE FROM portal_admin_attestations WHERE admin_username = ?",
            (normalized,),
        )
    return {
        "username": normalized,
        "totp_secret": totp_secret,
        "otp_uri": portal_security.totp_uri(
            totp_secret,
            username=normalized,
            issuer="Citespec Broker Admin",
        ),
        "created_at": timestamp,
    }


def list_admins(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT username, enabled, created_at, updated_at, last_used_at,
               failed_attempts, locked_until
        FROM portal_admin_credentials
        ORDER BY username
        """
    ).fetchall()
    return [
        {
            "username": str(row["username"]),
            "enabled": bool(row["enabled"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "last_used_at": str(row["last_used_at"]),
            "failed_attempts": int(row["failed_attempts"]),
            "locked_until": str(row["locked_until"]),
        }
        for row in rows
    ]


def remove_admin(connection: sqlite3.Connection, *, username: str) -> bool:
    normalized = username.strip()
    with connection:
        connection.execute(
            "DELETE FROM portal_admin_attestations WHERE admin_username = ? COLLATE NOCASE",
            (normalized,),
        )
        cursor = connection.execute(
            "DELETE FROM portal_admin_credentials WHERE username = ? COLLATE NOCASE",
            (normalized,),
        )
    return cursor.rowcount > 0


def _record_failure(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    now: datetime,
) -> None:
    attempts = int(row["failed_attempts"]) + 1
    locked_until = ""
    if attempts >= MAX_FAILED_ATTEMPTS:
        locked_until = _timestamp(now + timedelta(seconds=LOCKOUT_SECONDS))
    connection.execute(
        """
        UPDATE portal_admin_credentials
        SET failed_attempts = ?, locked_until = ?, updated_at = ?
        WHERE username = ?
        """,
        (attempts, locked_until, _timestamp(now), str(row["username"])),
    )
    connection.commit()


def issue_attestation(
    connection: sqlite3.Connection,
    *,
    username: str,
    password: str,
    otp: str,
    action: str,
    target: str,
    portal_user_id: str,
    portal_username: str,
    key_path: str,
    ttl_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    """Verify fresh credentials and issue a short-lived one-use attestation."""

    _validate_action(action)
    _validate_target(target)
    if not portal_user_id or not portal_username:
        raise ValueError("portal actor identity is required")
    if not 30 <= ttl_seconds <= 600:
        raise ValueError("attestation lifetime must be between 30 and 600 seconds")
    if not isinstance(password, str) or not password or not isinstance(otp, str):
        raise AdminAuthDenied("administrator credentials are invalid")

    started = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        started = True
        row = connection.execute(
            """
            SELECT * FROM portal_admin_credentials
            WHERE username = ? COLLATE NOCASE AND enabled = 1
            """,
            (username.strip(),),
        ).fetchone()
        if row is None:
            connection.rollback()
            started = False
            raise AdminAuthDenied("administrator credentials are invalid")
        locked_until = str(row["locked_until"] or "")
        if locked_until and locked_until > _timestamp(now):
            connection.rollback()
            started = False
            raise AdminAuthDenied("administrator credentials are temporarily locked")
        if not portal_security.verify_password(str(row["password_hash"]), password):
            _record_failure(connection, row, now=now)
            started = False
            raise AdminAuthDenied("administrator credentials are invalid")
        secret = portal_security.unseal(
            _load_key(key_path),
            str(row["totp_secret_enc"]),
        )
        counter = portal_security.verify_totp(
            secret,
            otp,
            last_counter=int(row["totp_last_counter"]),
            now=now.timestamp(),
        )
        if counter is None:
            _record_failure(connection, row, now=now)
            started = False
            raise AdminAuthDenied("administrator credentials are invalid")

        timestamp = _timestamp(now)
        expires_at = now + timedelta(seconds=ttl_seconds)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        nonce = secrets.token_hex(16)
        connection.execute(
            """
            UPDATE portal_admin_credentials
            SET totp_last_counter = ?, failed_attempts = 0, locked_until = '',
                last_used_at = ?, updated_at = ?
            WHERE username = ?
            """,
            (counter, timestamp, timestamp, str(row["username"])),
        )
        connection.execute(
            """
            INSERT INTO portal_admin_attestations(
                token_hash, nonce, admin_username, portal_user_id,
                portal_username, action, target, issued_at, expires_at,
                consumed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (
                token_hash,
                nonce,
                str(row["username"]),
                portal_user_id,
                portal_username,
                action,
                target,
                timestamp,
                _timestamp(expires_at),
            ),
        )
        # Expired capabilities have no forensic value once the broker event log
        # and the mutation audit have recorded the decision.
        connection.execute(
            "DELETE FROM portal_admin_attestations WHERE expires_at < ?",
            (_timestamp(now - timedelta(days=7)),),
        )
        connection.commit()
        started = False
        return {
            "attestation": token,
            "nonce": nonce,
            "admin": str(row["username"]),
            "action": action,
            "target": target,
            "expires_at": _timestamp(expires_at),
        }
    except Exception:
        if started:
            connection.rollback()
        raise


def consume_attestation(
    connection: sqlite3.Connection,
    *,
    token: str,
    action: str,
    target: str,
    portal_user_id: str,
    portal_username: str,
    now: datetime,
) -> dict[str, Any]:
    """Atomically consume a token after checking every binding."""

    _validate_action(action)
    _validate_target(target)
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise AdminAuthDenied("administrator attestation is invalid")
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    started = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        started = True
        row = connection.execute(
            "SELECT * FROM portal_admin_attestations WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        timestamp = _timestamp(now)
        if (
            row is None
            or str(row["action"]) != action
            or str(row["target"]) != target
            or str(row["portal_user_id"]) != portal_user_id
            or str(row["portal_username"]) != portal_username
            or str(row["consumed_at"])
            or str(row["expires_at"]) <= timestamp
        ):
            connection.rollback()
            started = False
            raise AdminAuthDenied("administrator attestation is invalid or expired")
        cursor = connection.execute(
            """
            UPDATE portal_admin_attestations
            SET consumed_at = ?
            WHERE token_hash = ? AND consumed_at = '' AND expires_at > ?
            """,
            (timestamp, token_hash, timestamp),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            started = False
            raise AdminAuthDenied("administrator attestation was already used")
        connection.commit()
        started = False
        return {
            "admin": str(row["admin_username"]),
            "nonce": str(row["nonce"]),
            "action": action,
            "target": target,
            "expires_at": str(row["expires_at"]),
        }
    except Exception:
        if started:
            connection.rollback()
        raise
