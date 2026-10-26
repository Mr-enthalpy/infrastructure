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

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

# The base schema below is version 2. Membership and lifecycle additions are
# applied as ordered migrations (3, 4), never by editing only PRAGMA
# user_version. Fresh installs therefore run the same migration path as
# existing deployments.
BASE_SCHEMA_VERSION: Final = 2
SCHEMA_VERSION: Final = 4

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
# Migration statements are executed one by one inside the caller's transaction
# (sqlite3.Connection.executescript would implicitly commit and break the
# all-or-nothing migration guarantee). IF NOT EXISTS makes an interrupted or
# previously half-applied migration safe to retry.
MEMBERSHIP_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS members (
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS member_keys (
        key_id TEXT PRIMARY KEY,
        member_id TEXT NOT NULL REFERENCES members(member_id),
        public_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'revoked')),
        created_at TEXT NOT NULL,
        revoked_at TEXT NOT NULL DEFAULT '',
        UNIQUE (member_id, fingerprint)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_member_keys_active_fingerprint
        ON member_keys(fingerprint) WHERE status = 'active'
    """,
    """
    CREATE TABLE IF NOT EXISTS member_operations (
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS membership_applications (
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
    )
    """,
)


def _apply_membership_migration(connection: sqlite3.Connection) -> None:
    for statement in MEMBERSHIP_STATEMENTS:
        connection.execute(statement)


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        str(row["name"]) == column
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _apply_lifecycle_migration(connection: sqlite3.Connection) -> None:
    """Lifecycle action + queued GPU-count migration (schema version 4).

    SQLite cannot alter a CHECK constraint, so member_operations is rebuilt
    with the same rows and a widened action whitelist. queue_requests only
    needs an additive column; existing rows are backfilled from their explicit
    GPU index list.
    """
    if not _column_exists(connection, "queue_requests", "requested_gpu_count"):
        connection.execute(
            "ALTER TABLE queue_requests ADD COLUMN requested_gpu_count INTEGER NOT NULL DEFAULT 0"
        )
    if not _column_exists(connection, "queue_requests", "idempotency_key"):
        connection.execute(
            "ALTER TABLE queue_requests ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''"
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_owner_idempotency "
        "ON queue_requests(owner_uid, idempotency_key) "
        "WHERE idempotency_key != ''"
    )
    # One approved applicant per Linux username. This is the database-level
    # race guard; two concurrent approvals cannot both provision "alice".
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_membership_approved_username "
        "ON membership_applications(requested_username) "
        "WHERE status = 'approved'"
    )
    for row in connection.execute(
        "SELECT id, requested_gpus FROM queue_requests WHERE requested_gpu_count = 0"
    ).fetchall():
        try:
            import json as _json

            decoded = _json.loads(str(row["requested_gpus"]))
            count = len(decoded) if isinstance(decoded, list) else 0
        except (TypeError, ValueError):
            count = 0
        if count:
            connection.execute(
                "UPDATE queue_requests SET requested_gpu_count = ? WHERE id = ?",
                (count, str(row["id"])),
            )

    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'member_operations'"
    ).fetchone()
    if row is not None and "enable_member" in str(row["sql"]):
        return
    connection.execute("DROP TABLE IF EXISTS member_operations_v4")
    connection.execute(
        """
        CREATE TABLE member_operations_v4 (
            operation_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            member_id TEXT,
            request_id TEXT,
            action TEXT NOT NULL
                CHECK (action IN (
                    'provision_member', 'set_keys', 'disable_member', 'enable_member'
                )),
            frozen_input TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    if row is not None:
        connection.execute(
            """
            INSERT INTO member_operations_v4(
                operation_id, idempotency_key, member_id, request_id, action,
                frozen_input, stage, status, attempts, last_error, created_at,
                updated_at
            )
            SELECT operation_id, idempotency_key, member_id, request_id, action,
                   frozen_input, stage, status, attempts, last_error, created_at,
                   updated_at
            FROM member_operations
            """
        )
    connection.execute("DROP TABLE member_operations")
    connection.execute("ALTER TABLE member_operations_v4 RENAME TO member_operations")


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
    3: _apply_membership_migration,
    4: _apply_lifecycle_migration,
}


def _backup_before_migration(connection: sqlite3.Connection, path: Path, version: int) -> Path:
    """Take a consistent SQLite backup (not a byte copy of a live WAL file)."""
    backup = path.with_name(f"{path.name}.pre-migration-{version}.bak")
    destination = sqlite3.connect(backup)
    try:
        connection.backup(destination)
    finally:
        destination.close()
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
            # The base schema is version 2. Do NOT jump to SCHEMA_VERSION: the
            # ordered migrations still have to add membership tables and
            # lifecycle columns. Otherwise a fresh install and an upgraded
            # install would have different schemas.
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version={BASE_SCHEMA_VERSION}")
            connection.commit()
            version = BASE_SCHEMA_VERSION
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
            backup = _backup_before_migration(connection, db_path, migration_version)
            try:
                connection.execute("BEGIN IMMEDIATE")
                MIGRATIONS[migration_version](connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration_version, datetime.now(UTC).isoformat()),
                )
                connection.execute(f"PRAGMA user_version={migration_version}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise RuntimeError(
                    f"migration {migration_version} failed; transaction rolled back, "
                    f"pre-migration backup at {backup}"
                ) from None
        # Existing version-2 databases have no schema_migrations rows, and a
        # database that already has every row may still have a stale
        # user_version. Converge both representations after the loop.
        if version < SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
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
