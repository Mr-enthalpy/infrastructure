import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from citespec_resource import store
from citespec_resource.db import connect, init_db
from citespec_resource.errors import GpuBusy, InsufficientResources, NotFound
from citespec_resource.models import GPU

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
    max_cpus_per_user: float | None = None,
    max_memory_gb_per_user: int | None = None,
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
        image="citespec-base:2026.10",
        workspace=f"/data1/citespec-users/{owner_name}",
        note="test",
        starts_at=NOW,
        expires_at=NOW + timedelta(hours=12),
        max_cpus=max_cpus,
        max_memory_gb=max_memory_gb,
        max_cpus_per_user=max_cpus_per_user,
        max_memory_gb_per_user=max_memory_gb_per_user,
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
        container_name="citespec-alice-1",
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
    assert failed["remaining_seconds"] is None
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 0
    connection.close()


def test_events_are_recorded(tmp_path):
    connection = _connection(tmp_path)
    allocation_id = _create(connection, gpus=("GPU-0",))
    store.activate_allocation(
        connection,
        allocation_id,
        container_id="container-1",
        container_name="citespec-alice-1",
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


def test_per_user_cpu_limit_is_enforced(tmp_path):
    connection = _connection(tmp_path)
    _create(connection, gpus=("GPU-0",), cpus=8, memory_gb=8)
    with pytest.raises(InsufficientResources, match="per-user CPU limit"):
        _create(
            connection,
            gpus=("GPU-1",),
            cpus=8,
            memory_gb=8,
            max_cpus_per_user=12,
        )
    connection.close()


def test_per_user_memory_limit_is_enforced(tmp_path):
    connection = _connection(tmp_path)
    _create(connection, gpus=("GPU-0",), cpus=4, memory_gb=32)
    with pytest.raises(InsufficientResources, match="per-user memory limit"):
        _create(
            connection,
            gpus=("GPU-1",),
            cpus=4,
            memory_gb=32,
            max_memory_gb_per_user=50,
        )
    connection.close()


def _queue(
    connection: sqlite3.Connection,
    *,
    gpus: tuple[int, ...] = (3,),
    owner_uid: int = 1000,
    owner_name: str = "alice",
    now: datetime = NOW,
):
    return store.create_queue_request(
        connection,
        owner_uid=owner_uid,
        owner_name=owner_name,
        requested_gpus=gpus,
        cpus=8,
        memory_gb=32,
        hours=12,
        image="citespec-base:2026.10",
        workspace=f"/data1/citespec-users/{owner_name}",
        note="queued test",
        max_queue_length=10,
        max_queued_allocations_per_user=4,
        max_queue_hours=24,
        now=now,
    )


def test_queue_request_position_and_cancel(tmp_path):
    connection = _connection(tmp_path)
    first = _queue(connection, gpus=(3,), now=NOW)
    second = _queue(connection, gpus=(4,), now=NOW + timedelta(seconds=1))
    listed = store.list_queue_requests(connection, owner_uid=1000, now=NOW)
    assert [item["queue_id"] for item in listed] == [first, second]
    assert listed[0]["position"] == 1
    assert listed[1]["position"] == 2
    store.cancel_queue_request(connection, first, actor_uid=1000, reason="test", now=NOW)
    assert [item["queue_id"] for item in store.list_queue_requests(connection)] == [second]
    history = store.list_queue_requests(connection, include_finished=True)
    assert history[0]["status"] == "cancelled" or history[1]["status"] == "cancelled"
    connection.close()


def test_queue_positions_are_unique_with_identical_timestamps(tmp_path):
    connection = _connection(tmp_path)
    _queue(connection, gpus=(3,), now=NOW)
    _queue(connection, gpus=(4,), now=NOW)
    listed = store.list_queue_requests(connection, owner_uid=1000, now=NOW)
    assert sorted(item["position"] for item in listed) == [1, 2]
    connection.close()


def test_expired_queue_request_is_not_admitted(tmp_path):
    connection = _connection(tmp_path)
    queue_id = _queue(connection, gpus=(3,), now=NOW)
    limits = {
        "max_cpus": 32.0,
        "max_memory_gb": 128,
        "max_cpus_per_user": 32.0,
        "max_memory_gb_per_user": 128,
        "max_gpus_per_user": 4,
        "max_active_allocations_per_user": 4,
    }
    assert (
        store.admit_next_queued(
            connection,
            available_gpu_uuids={"GPU-3"},
            now=NOW + timedelta(hours=25),
            **limits,
        )
        is None
    )
    assert store.get_queue_request(connection, queue_id)["status"] == "queued"
    connection.close()


def test_queue_admission_waits_for_available_gpu(tmp_path):
    connection = _connection(tmp_path)
    queue_id = _queue(connection, gpus=(3,))
    limits = {
        "max_cpus": 32.0,
        "max_memory_gb": 128,
        "max_cpus_per_user": 32.0,
        "max_memory_gb_per_user": 128,
        "max_gpus_per_user": 4,
        "max_active_allocations_per_user": 4,
    }
    assert (
        store.admit_next_queued(
            connection,
            available_gpu_uuids={"GPU-0"},
            now=NOW,
            **limits,
        )
        is None
    )
    assert store.get_queue_request(connection, queue_id)["status"] == "queued"
    allocation_id = store.admit_next_queued(
        connection,
        available_gpu_uuids={"GPU-3"},
        now=NOW,
        **limits,
    )
    assert allocation_id is not None
    allocation = store.get_allocation(connection, allocation_id, now=NOW)
    assert allocation["status"] == "pending"
    assert allocation["gpus"] == [3]
    assert store.get_queue_request(connection, queue_id)["status"] == "admitted"
    connection.close()


def test_queue_expiry(tmp_path):
    connection = _connection(tmp_path)
    queue_id = _queue(connection, gpus=(5,), now=NOW)
    expired = store.expire_queue_requests(connection, now=NOW + timedelta(hours=25))
    assert expired == [queue_id]
    assert store.get_queue_request(connection, queue_id)["status"] == "expired"
    connection.close()


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


def test_create_allocation_cannot_bypass_pending_queue(tmp_path):
    connection = _connection(tmp_path)
    queue_id = _queue(connection, gpus=(3,), now=NOW)
    with pytest.raises(GpuBusy, match="queued requests are pending"):
        _create(connection, gpus=("GPU-3",))
    store.cancel_queue_request(connection, queue_id, actor_uid=1000, reason="done", now=NOW)
    allocation_id = _create(connection, gpus=("GPU-3",))
    assert allocation_id
    connection.close()


def test_cpu_only_blocked_head_stops_backfill(tmp_path):
    """A CPU-only head blocked on the CPU budget must block later requests.

    The blocked head claims no GPUs, so the old blocked_gpu_uuids-only check
    let later CPU-only requests through even with backfill disabled.
    """
    connection = _connection(tmp_path)
    store.ensure_user(connection, uid=1001, username="bob", role="member", now=NOW)
    store.ensure_user(connection, uid=1002, username="carol", role="member", now=NOW)
    _create(connection, gpus=(), cpus=84.0, memory_gb=64, max_cpus=100.0, max_memory_gb=219)
    head = store.create_queue_request(
        connection,
        owner_uid=1001,
        owner_name="bob",
        requested_gpus=[],
        cpus=32.0,
        memory_gb=32,
        hours=12,
        image="citespec-base:2026.10",
        workspace="/data1/citespec-users/bob",
        note="",
        max_queue_length=10,
        max_queued_allocations_per_user=4,
        max_queue_hours=24,
        now=NOW,
    )
    tail = store.create_queue_request(
        connection,
        owner_uid=1002,
        owner_name="carol",
        requested_gpus=[],
        cpus=8.0,
        memory_gb=8,
        hours=12,
        image="citespec-base:2026.10",
        workspace="/data1/citespec-users/carol",
        note="",
        max_queue_length=10,
        max_queued_allocations_per_user=4,
        max_queue_hours=24,
        now=NOW,
    )
    # With backfill disabled, the CPU-only blocked head must block later
    # requests too: the blocked set is not GPU-specific.
    admitted = store.admit_next_queued(
        connection,
        max_cpus=100.0,
        max_memory_gb=219,
        max_cpus_per_user=64.0,
        max_memory_gb_per_user=192,
        max_gpus_per_user=4,
        max_active_allocations_per_user=4,
        backfill_enabled=False,
        now=NOW,
    )
    assert admitted is None
    states = {
        str(row["id"]): str(row["state"])
        for row in connection.execute("SELECT id, state FROM queue_requests").fetchall()
    }
    assert states[head] == "queued"
    assert states[tail] == "queued"
    # Within the reserve window and with backfill enabled, the small tail may
    # run past the blocked head (documented backfill behavior).
    admitted = store.admit_next_queued(
        connection,
        max_cpus=100.0,
        max_memory_gb=219,
        max_cpus_per_user=64.0,
        max_memory_gb_per_user=192,
        max_gpus_per_user=4,
        max_active_allocations_per_user=4,
        backfill_enabled=True,
        now=NOW,
    )
    assert admitted is not None
    connection.close()


def test_cpu_only_head_starvation_guard_blocks_later_requests(tmp_path):
    connection = _connection(tmp_path)
    store.ensure_user(connection, uid=1001, username="bob", role="member", now=NOW)
    store.ensure_user(connection, uid=1002, username="carol", role="member", now=NOW)
    _create(connection, gpus=(), cpus=84.0, memory_gb=64, max_cpus=100.0, max_memory_gb=219)
    store.create_queue_request(
        connection,
        owner_uid=1001,
        owner_name="bob",
        requested_gpus=[],
        cpus=32.0,
        memory_gb=32,
        hours=12,
        image="citespec-base:2026.10",
        workspace="/data1/citespec-users/bob",
        note="",
        max_queue_length=10,
        max_queued_allocations_per_user=4,
        max_queue_hours=24,
        now=NOW,
    )
    tail = store.create_queue_request(
        connection,
        owner_uid=1002,
        owner_name="carol",
        requested_gpus=[],
        cpus=8.0,
        memory_gb=8,
        hours=12,
        image="citespec-base:2026.10",
        workspace="/data1/citespec-users/carol",
        note="",
        max_queue_length=10,
        max_queued_allocations_per_user=4,
        max_queue_hours=24,
        now=NOW,
    )
    admitted = store.admit_next_queued(
        connection,
        max_cpus=100.0,
        max_memory_gb=219,
        max_cpus_per_user=64.0,
        max_memory_gb_per_user=192,
        max_gpus_per_user=4,
        max_active_allocations_per_user=4,
        backfill_reserve_minutes=30,
        now=NOW + timedelta(minutes=45),
    )
    assert admitted is None
    state = str(
        connection.execute(
            "SELECT state FROM queue_requests WHERE id = ?",
            (tail,),
        ).fetchone()[0]
    )
    assert state == "queued"
    connection.close()
