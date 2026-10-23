"""SQLite schema, connections, and small settings helpers for the broker."""

from __future__ import annotations

import sqlite3
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
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'active', 'released', 'expired', 'failed')),
    image TEXT NOT NULL,
    cpus REAL NOT NULL CHECK (cpus > 0),
    memory_gb INTEGER NOT NULL CHECK (memory_gb > 0),
    workspace TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    container_id TEXT NOT NULL DEFAULT '',
    container_name TEXT NOT NULL DEFAULT ''
);

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

CREATE INDEX IF NOT EXISTS idx_allocations_expiry
    ON allocations(status, expires_at);

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


def init_db(path: str | Path) -> sqlite3.Connection:
    connection = connect(path)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        connection.close()
        raise RuntimeError(
            f"database schema version {version} is newer than supported {SCHEMA_VERSION}"
        )
    connection.executescript(SCHEMA)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    connection.commit()
    return connection


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
