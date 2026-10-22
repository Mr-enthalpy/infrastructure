import sqlite3
from datetime import UTC, datetime

import pytest

from udc_resource.db import (
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
            "udc-test",
            status,
            "udc-base:2026.10",
            8.0,
            32,
            "/data1/udc-users/udc-test",
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
