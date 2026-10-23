import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from udc_resource import store
from udc_resource.db import connect, init_db
from udc_resource.errors import GpuBusy, InsufficientResources, NotFound
from udc_resource.models import GPU

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def _connection(tmp_path) -> sqlite3.Connection:
    connection = init_db(tmp_path / "state.db")
    store.upsert_gpus(
        connection,
        [
            GPU(index=index, uuid=f"GPU-{index}", name="RTX 4090", memory_mib=49140)
            for index in range(8)
        ],
    )
    store.ensure_user(connection, uid=1000, username="alice", role="member", now=NOW)
    return connection


def _create(
    connection: sqlite3.Connection,
    *,
    gpus: tuple[str, ...] = ("GPU-0",),
    cpus: float = 8.0,
    memory_gb: int = 32,
    max_cpus: float = 32.0,
    max_memory_gb: int = 128,
    max_active_allocations_per_user: int = 4,
    owner_uid: int = 1000,
    owner_name: str = "alice",
) -> str:
    return store.create_allocation(
        connection,
        owner_uid=owner_uid,
        owner_name=owner_name,
        gpu_uuids=gpus,
        cpus=cpus,
        memory_gb=memory_gb,
        image="udc-base:2026.10",
        workspace=f"/data1/udc-users/{owner_name}",
        note="test",
        starts_at=NOW,
        expires_at=NOW + timedelta(hours=12),
        max_cpus=max_cpus,
        max_memory_gb=max_memory_gb,
        max_active_allocations_per_user=max_active_allocations_per_user,
        now=NOW,
    )


def test_create_allocation_claims_multiple_gpus(tmp_path):
    connection = _connection(tmp_path)
    allocation_id = _create(connection, gpus=("GPU-3", "GPU-5"))
    allocation = store.get_allocation(connection, allocation_id, now=NOW)
    assert allocation["gpus"] == [3, 5]
    assert allocation["status"] == "pending"
    claims = connection.execute(
        "SELECT gpu_uuid FROM active_gpu_claims WHERE allocation_id = ? ORDER BY gpu_uuid",
        (allocation_id,),
    ).fetchall()
    assert [row[0] for row in claims] == ["GPU-3", "GPU-5"]
    connection.close()


def test_busy_gpu_is_rejected(tmp_path):
    connection = _connection(tmp_path)
    _create(connection, gpus=("GPU-3",))
    with pytest.raises(GpuBusy):
        _create(connection, gpus=("GPU-3",))
    count = connection.execute("SELECT COUNT(*) FROM allocations").fetchone()[0]
    assert count == 1
    connection.close()


def test_cpu_budget_is_enforced(tmp_path):
    connection = _connection(tmp_path)
    _create(connection, gpus=("GPU-0",), cpus=24)
    with pytest.raises(InsufficientResources):
        _create(connection, gpus=("GPU-1",), cpus=16)
    connection.close()


def test_memory_budget_is_enforced(tmp_path):
    connection = _connection(tmp_path)
    _create(connection, gpus=("GPU-0",), memory_gb=100)
    with pytest.raises(InsufficientResources):
        _create(connection, gpus=("GPU-1",), memory_gb=32)
    connection.close()


def test_activate_and_fail_release_claims(tmp_path):
    connection = _connection(tmp_path)
    allocation_id = _create(connection, gpus=("GPU-2",))
    store.activate_allocation(
        connection,
        allocation_id,
        container_id="container-1",
        container_name="udc-alice-1",
        now=NOW,
    )
    active = store.get_allocation(connection, allocation_id, now=NOW)
    assert active["status"] == "active"
    assert active["container_id"] == "container-1"
    gpu = next(row for row in store.list_gpus(connection, now=NOW) if row["index"] == 2)
    assert gpu["allocation_id"] == allocation_id
    assert gpu["remaining_seconds"] == 12 * 3600

    store.fail_allocation(connection, allocation_id, reason="test failure", now=NOW)
    failed = store.get_allocation(connection, allocation_id, now=NOW)
    assert failed["status"] == "failed"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 0
    connection.close()


def test_events_are_recorded(tmp_path):
    connection = _connection(tmp_path)
    allocation_id = _create(connection, gpus=("GPU-0",))
    store.activate_allocation(
        connection,
        allocation_id,
        container_id="container-1",
        container_name="udc-alice-1",
        now=NOW,
    )
    events = store.list_allocation_events(connection, allocation_id)
    assert [event["action"] for event in events] == [
        "allocation_active",
        "allocation_pending",
    ]
    connection.close()


def test_list_allocations_filters_by_owner(tmp_path):
    connection = _connection(tmp_path)
    _create(connection, gpus=("GPU-0",))
    assert len(store.list_allocations(connection, owner_uid=1000, now=NOW)) == 1
    assert store.list_allocations(connection, owner_uid=2000, now=NOW) == []
    connection.close()


def test_unknown_gpu_index_is_rejected(tmp_path):
    connection = _connection(tmp_path)
    with pytest.raises(NotFound):
        store.gpus_for_indices(connection, [99])
    connection.close()


def test_mode_setting_roundtrip(tmp_path):
    connection = _connection(tmp_path)
    assert store.get_mode(connection, "monitor") == "monitor"
    store.set_mode(connection, "active", actor_uid=0, now=NOW)
    assert store.get_mode(connection, "monitor") == "active"
    store.set_mode(connection, "maintenance", actor_uid=0, now=NOW)
    assert store.get_mode(connection, "monitor") == "maintenance"
    events = store.list_events(connection)
    assert events[0]["action"] == "mode_changed"
    with pytest.raises(ValueError):
        store.set_mode(connection, "automatic", actor_uid=0, now=NOW)
    connection.close()


def test_concurrent_claims_only_one_wins(tmp_path):
    connection = _connection(tmp_path)
    connection.close()

    def attempt(uid: int) -> int | None:
        candidate = connect(tmp_path / "state.db")
        try:
            _create(
                candidate,
                gpus=("GPU-6",),
                owner_uid=uid,
                owner_name=f"user{uid}",
            )
            return uid
        except GpuBusy:
            return None
        finally:
            candidate.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1000, 1001]))
    assert sum(result is not None for result in results) == 1


def test_per_user_allocation_limit_is_enforced(tmp_path):
    connection = _connection(tmp_path)
    for _ in range(4):
        _create(connection, gpus=(), cpus=1, memory_gb=1)
    with pytest.raises(InsufficientResources, match="per-user allocation limit"):
        _create(connection, gpus=(), cpus=1, memory_gb=1)
    connection.close()


def test_per_user_gpu_limit_is_enforced(tmp_path):
    connection = _connection(tmp_path)
    for index in range(4):
        _create(
            connection,
            gpus=(f"GPU-{index}",),
            cpus=1,
            memory_gb=1,
            max_active_allocations_per_user=10,
        )
    with pytest.raises(InsufficientResources, match="per-user GPU limit"):
        _create(
            connection,
            gpus=("GPU-4",),
            cpus=1,
            memory_gb=1,
            max_active_allocations_per_user=10,
        )
    connection.close()
