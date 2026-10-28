import sqlite3
from datetime import UTC, datetime

import pytest

from citespec_resource.db import (
    SCHEMA_VERSION,
    connect,
    get_setting,
    init_db,
    set_setting,
)


def _add_gpu(connection, idx, uuid):
    connection.execute(
        "INSERT INTO gpus(uuid, idx, name, memory_mib) VALUES (?, ?, ?, ?)",
        (uuid, idx, "NVIDIA GeForce RTX 4090", 49140),
    )


def _add_allocation(connection, allocation_id, status="pending"):
    connection.execute(
        """
        INSERT INTO allocations(
            id, owner_uid, owner_name, status, image, cpus, memory_gb,
            workspace, created_at, starts_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            allocation_id,
            1000,
            "citespec-test",
            status,
            "citespec-base:2026.10",
            8.0,
            32,
            "/data1/citespec-users/citespec-test",
            "2026-10-22T00:00:00+00:00",
            "2026-10-22T00:00:00+00:00",
            "2026-10-22T12:00:00+00:00",
        ),
    )


def test_schema_initializes(tmp_path):
    connection = init_db(tmp_path / "state.db")
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "users",
        "gpus",
        "allocations",
        "allocation_gpus",
        "active_gpu_claims",
        "events",
        "settings",
    } <= tables
    connection.close()


def test_schema_version_and_wal(tmp_path):
    connection = init_db(tmp_path / "state.db")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert version == SCHEMA_VERSION
    assert journal_mode.lower() == "wal"
    connection.close()


def test_multiple_gpus_can_be_claimed_by_one_allocation(tmp_path):
    connection = init_db(tmp_path / "state.db")
    _add_gpu(connection, 3, "GPU-3")
    _add_gpu(connection, 5, "GPU-5")
    _add_allocation(connection, "alloc-1")
    connection.execute(
        "INSERT INTO active_gpu_claims(gpu_uuid, allocation_id) VALUES (?, ?)",
        ("GPU-3", "alloc-1"),
    )
    connection.execute(
        "INSERT INTO active_gpu_claims(gpu_uuid, allocation_id) VALUES (?, ?)",
        ("GPU-5", "alloc-1"),
    )
    connection.commit()
    count = connection.execute(
        "SELECT COUNT(*) FROM active_gpu_claims WHERE allocation_id = ?",
        ("alloc-1",),
    ).fetchone()[0]
    assert count == 2
    connection.close()


def test_one_gpu_cannot_be_claimed_twice(tmp_path):
    connection = init_db(tmp_path / "state.db")
    _add_gpu(connection, 3, "GPU-3")
    _add_allocation(connection, "alloc-1")
    _add_allocation(connection, "alloc-2")
    connection.execute(
        "INSERT INTO active_gpu_claims(gpu_uuid, allocation_id) VALUES (?, ?)",
        ("GPU-3", "alloc-1"),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO active_gpu_claims(gpu_uuid, allocation_id) VALUES (?, ?)",
            ("GPU-3", "alloc-2"),
        )
    connection.rollback()
    connection.close()


def test_foreign_keys_are_enabled_on_new_connections(tmp_path):
    path = tmp_path / "state.db"
    init_db(path).close()
    connection = connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO allocation_gpus(allocation_id, gpu_uuid) VALUES (?, ?)",
            ("missing-allocation", "missing-gpu"),
        )
    connection.close()


def test_invalid_status_is_rejected(tmp_path):
    connection = init_db(tmp_path / "state.db")
    with pytest.raises(sqlite3.IntegrityError):
        _add_allocation(connection, "alloc-bad", status="running")
    connection.close()


def test_settings_roundtrip(tmp_path):
    connection = init_db(tmp_path / "state.db")
    assert get_setting(connection, "mode") is None
    assert get_setting(connection, "mode", "monitor") == "monitor"
    set_setting(
        connection,
        "mode",
        "monitor",
        now=datetime(2026, 10, 22, tzinfo=UTC),
    )
    assert get_setting(connection, "mode") == "monitor"
    set_setting(connection, "mode", "active")
    assert get_setting(connection, "mode") == "active"
    connection.close()


def test_migrations_upgrade_v2_database_to_current(tmp_path):
    """Migration 3/4 are ordered, retryable, and preserve legacy queue rows."""
    from citespec_resource.db import BASE_SCHEMA_VERSION, SCHEMA

    path = tmp_path / "legacy.db"
    connection = connect(path)
    connection.executescript(SCHEMA)
    connection.execute(f"PRAGMA user_version={BASE_SCHEMA_VERSION}")
    connection.execute(
        "INSERT INTO queue_requests("
        "id, owner_uid, owner_name, cpus, memory_gb, hours, image, workspace, "
        "requested_gpus, created_at, expires_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-queue",
            1000,
            "alice",
            4,
            8,
            2,
            "citespec-base:2026.10",
            "/data1/citespec-users/alice",
            "[0, 3]",
            "2026-10-22T00:00:00+00:00",
            "2026-10-22T02:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()

    upgraded = init_db(path)
    version = upgraded.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    queue_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(queue_requests)")}
    assert {"requested_gpu_count", "idempotency_key"} <= queue_columns
    row = upgraded.execute(
        "SELECT requested_gpu_count, idempotency_key FROM queue_requests WHERE id = ?",
        ("legacy-queue",),
    ).fetchone()
    assert row[0] == 2 and row[1] == ""
    actions = upgraded.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='member_operations'"
    ).fetchone()[0]
    assert "enable_member" in actions
    applied = {int(row[0]) for row in upgraded.execute("SELECT version FROM schema_migrations")}
    assert {3, 4} <= applied
    indexes = {
        row[0]
        for row in upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name IN ('idx_queue_owner_idempotency', 'idx_members_username_ci')"
        )
    }
    assert "idx_queue_owner_idempotency" in indexes
    upgraded.close()

    # Re-running the migration path is a no-op, not a failure.
    again = init_db(path)
    assert again.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert (
        again.execute("SELECT COUNT(*) FROM queue_requests WHERE id = 'legacy-queue'").fetchone()[0]
        == 1
    )
    again.close()


def test_refuses_newer_schema(tmp_path):
    path = tmp_path / "future.db"
    connection = connect(path)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="newer"):
        init_db(path)


def test_migration_7_adds_environment_memory_without_touching_old_rows(tmp_path):
    from citespec_resource.db import BASE_SCHEMA_VERSION, MIGRATIONS, SCHEMA

    path = tmp_path / "v6.db"
    connection = connect(path)
    connection.executescript(SCHEMA)
    connection.execute(f"PRAGMA user_version={BASE_SCHEMA_VERSION}")
    for version in sorted(MIGRATIONS):
        if version < 7:
            MIGRATIONS[version](connection)
    connection.execute("PRAGMA user_version=6")
    _add_gpu(connection, 0, "GPU-0")
    _add_allocation(connection, "legacy-alloc")
    connection.execute(
        """
        INSERT INTO queue_requests(
            id, owner_uid, owner_name, cpus, memory_gb, hours, image, workspace,
            requested_gpus, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-queue",
            1000,
            "citespec-test",
            4,
            8,
            2,
            "citespec-base:2026.10",
            "/data1/citespec-users/citespec-test",
            "[0]",
            "2026-10-22T00:00:00+00:00",
            "2026-10-22T02:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()

    upgraded = init_db(path)
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {
        row[0] for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "research_projects",
        "environment_profiles",
        "environment_versions",
        "environment_artifacts",
        "environment_builds",
        "legacy_sources",
        "legacy_claims",
    } <= tables
    triggers = {
        row[0] for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    assert {
        "trg_environment_versions_immutable",
        "trg_environment_versions_no_delete",
        "trg_environment_artifacts_immutable",
    } <= triggers

    allocation_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(allocations)")}
    assert {"env_version_id", "env_build_id", "image_id"} <= allocation_columns
    row = upgraded.execute(
        "SELECT env_version_id, env_build_id, image_id FROM allocations WHERE id = ?",
        ("legacy-alloc",),
    ).fetchone()
    assert tuple(row) == (None, None, None)
    queue_row = upgraded.execute(
        "SELECT env_version_id, env_build_id, image_id FROM queue_requests WHERE id = ?",
        ("legacy-queue",),
    ).fetchone()
    assert tuple(queue_row) == (None, None, None)
    upgraded.close()

    # The upgrade is retryable: a second init must be a no-op.
    again = init_db(path)
    assert again.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert again.execute("SELECT COUNT(*) FROM research_projects").fetchone()[0] == 0
    again.close()
