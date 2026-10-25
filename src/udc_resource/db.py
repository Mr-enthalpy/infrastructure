"""SQLite schema, versioned migrations, connections, and settings helpers.

Migration rules (docs/goals/member-onboarding-and-ux.md §10.2):
- every migration runs inside a transaction and is recorded in
  schema_migrations; interrupted runs are retried on the next init;
- a timestamped backup of the database file is taken before applying any
  pending migration so an interrupted upgrade can be restored manually;
- existing allocation, GPU claim, queue, and audit data is never touched by
  the membership migrations — they only add new tables and columns.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

SCHEMA_VERSION: Final = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    uid INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'member'
        CHECK (role IN ('member', 'admin')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gpus (
    uuid TEXT PRIMARY KEY,
    idx INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    memory_mib INTEGER NOT NULL CHECK (memory_mib > 0),
    state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (state IN ('unknown', 'idle', 'allocated', 'unmanaged'))
);

CREATE TABLE IF NOT EXISTS allocations (
    id TEXT PRIMARY KEY,
    owner_uid INTEGER NOT NULL,
    owner_name TEXT NOT NULL,
    cpus REAL NOT NULL CHECK (cpus > 0),
    memory_gb INTEGER NOT NULL CHECK (memory_gb > 0),
    image TEXT NOT NULL,
    workspace TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    gpus TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'released', 'expired', 'failed', 'cleanup_pending')),
    created_at TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    container_id TEXT NOT NULL DEFAULT '',
    container_name TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_allocations_owner_idem
    ON allocations(owner_uid, idempotency_key)
    WHERE idempotency_key IS NOT NULL AND idempotency_key != '';

CREATE TABLE IF NOT EXISTS allocation_gpus (
    allocation_id TEXT NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
    gpu_uuid TEXT NOT NULL REFERENCES gpus(uuid),
    PRIMARY KEY (allocation_id, gpu_uuid)
);

CREATE TABLE IF NOT EXISTS active_gpu_claims (
    gpu_uuid TEXT PRIMARY KEY REFERENCES gpus(uuid),
    allocation_id TEXT NOT NULL REFERENCES allocations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_active_gpu_claims_allocation
    ON active_gpu_claims(allocation_id);

CREATE INDEX IF NOT EXISTS idx_allocations_expiry
    ON allocations(status, expires_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_uid INTEGER,
    action TEXT NOT NULL,
    allocation_id TEXT,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_requests (
    id TEXT PRIMARY KEY,
    owner_uid INTEGER NOT NULL,
    owner_name TEXT NOT NULL,
    cpus REAL NOT NULL CHECK (cpus > 0),
    memory_gb INTEGER NOT NULL CHECK (memory_gb > 0),
    hours INTEGER NOT NULL CHECK (hours > 0),
    image TEXT NOT NULL,
    workspace TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    requested_gpus TEXT NOT NULL DEFAULT '[]',
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'admitted', 'cancelled', 'expired', 'failed')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    admitted_at TEXT NOT NULL DEFAULT '',
    allocation_id TEXT,
    reason TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_queue_requests_schedule
    ON queue_requests(state, priority DESC, created_at);
"""

# Membership tables (goal member-onboarding-and-ux.md §6/§10). Applied as a
# versioned migration so existing deployments upgrade in place.
MEMBERSHIP_SCHEMA = """
CREATE TABLE members (
    member_id TEXT PRIMARY KEY,
    linux_uid INTEGER UNIQUE,
    linux_username TEXT UNIQUE,
    status TEXT NOT NULL
        CHECK (status IN (
            'provisioning', 'active', 'suspended', 'expired', 'revoked', 'provision_failed'
        )),
    valid_until TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    nickname TEXT NOT NULL DEFAULT '',
    public_contact TEXT NOT NULL DEFAULT '',
    private_contact TEXT NOT NULL DEFAULT '',
    access_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE member_keys (
    key_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES members(member_id),
    public_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked')),
    created_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL DEFAULT '',
    UNIQUE (member_id, fingerprint)
);

CREATE UNIQUE INDEX idx_member_keys_active_fingerprint
    ON member_keys(fingerprint) WHERE status = 'active';

CREATE TABLE member_operations (
    operation_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    member_id TEXT,
    request_id TEXT,
    action TEXT NOT NULL
        CHECK (action IN ('provision_member', 'set_keys', 'disable_member')),
    frozen_input TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE membership_applications (
    request_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (status IN ('pending_review', 'needs_info', 'approved', 'rejected', 'cancelled')),
    requested_username TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    reviewer_uid INTEGER,
    review_note TEXT NOT NULL DEFAULT '',
    internal_note TEXT NOT NULL DEFAULT '',
    operation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the PRAGMAs required on every connection."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


MIGRATIONS: Final[dict[int, Callable[[sqlite3.Connection], object]]] = {
    3: lambda connection: connection.executescript(MEMBERSHIP_SCHEMA),
}


def _backup_before_migration(path: Path, version: int) -> Path:
    backup = path.with_name(f"{path.name}.pre-migration-{version}.bak")
    shutil.copy2(path, backup)
    return backup


def init_db(path: str | Path) -> sqlite3.Connection:
    connection = connect(path)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version == 0:
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
            version = SCHEMA_VERSION
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
        applied = {
            int(row["version"])
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
        db_path = Path(path)
        for migration_version in sorted(MIGRATIONS):
            if migration_version <= version or migration_version in applied:
                continue
            backup = _backup_before_migration(db_path, migration_version)
            try:
                connection.execute("BEGIN IMMEDIATE")
                MIGRATIONS[migration_version](connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration_version, datetime.now(UTC).isoformat()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise RuntimeError(
                    f"migration {migration_version} failed; database restored from "
                    f"{backup} is untouched (transaction rolled back)"
                ) from None
        return connection
    except Exception:
        connection.close()
        raise


def get_setting(
    connection: sqlite3.Connection,
    key: str,
    default: str | None = None,
) -> str | None:
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else str(row["value"])


def set_setting(
    connection: sqlite3.Connection,
    key: str,
    value: str,
    *,
    now: datetime | None = None,
) -> None:
    timestamp = (now or datetime.now(UTC)).isoformat()
    connection.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, timestamp),
    )
    connection.commit()
