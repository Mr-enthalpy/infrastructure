import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from citespec_resource import store
from citespec_resource.broker.docker_driver import ContainerInfo
from citespec_resource.broker.reconcile import reconcile
from citespec_resource.db import init_db
from citespec_resource.models import GPU
from tests.fakes import FakeDocker

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
    return connection


def _allocation(
    connection: sqlite3.Connection,
    *,
    created_at: datetime,
    expires_at: datetime,
) -> str:
    return store.create_allocation(
        connection,
        owner_uid=1000,
        owner_name="alice",
        gpu_uuids=("GPU-0",),
        cpus=8.0,
        memory_gb=32,
        image="citespec-base:2026.10",
        workspace="/data1/citespec-users/alice",
        note="",
        starts_at=created_at,
        expires_at=expires_at,
        max_cpus=32.0,
        max_memory_gb=128,
        now=created_at,
    )


def _container(
    allocation_id: str,
    *,
    status: str = "running",
    managed: bool = True,
) -> ContainerInfo:
    labels = {"citespec.owner": "alice"}
    if managed:
        labels.update(
            {
                "citespec.managed": "true",
                "citespec.allocation": allocation_id,
            }
        )
    return ContainerInfo(
        id="container-1",
        name="citespec-alice-test",
        image="citespec-base:2026.10",
        status=status,
        labels=labels,
    )


def test_stale_pending_fails_and_removes_container(tmp_path):
    connection = _connection(tmp_path)
    old = NOW - timedelta(minutes=10)
    allocation_id = _allocation(connection, created_at=old, expires_at=old + timedelta(hours=12))
    assert store.get_allocation(connection, allocation_id, now=NOW)["gpus"] == [0]
    fake = FakeDocker()
    fake.add_container(_container(allocation_id))
    summary = reconcile(connection, fake, now=NOW)
    assert summary["pending_failed"] == 1
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "failed"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 0
    assert fake.removed == ["container-1"]
    connection.close()


def test_recent_pending_is_left_alone(tmp_path):
    connection = _connection(tmp_path)
    created = NOW - timedelta(minutes=1)
    allocation_id = _allocation(
        connection,
        created_at=created,
        expires_at=created + timedelta(hours=12),
    )
    fake = FakeDocker()
    summary = reconcile(connection, fake, now=NOW)
    assert summary["pending_failed"] == 0
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "pending"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 1
    connection.close()


def test_running_active_allocation_is_kept(tmp_path):
    connection = _connection(tmp_path)
    old = NOW - timedelta(hours=1)
    allocation_id = _allocation(connection, created_at=old, expires_at=NOW + timedelta(hours=1))
    store.activate_allocation(
        connection,
        allocation_id,
        container_id="container-1",
        container_name="citespec-alice-test",
        now=old,
    )
    fake = FakeDocker()
    fake.add_container(_container(allocation_id))
    summary = reconcile(connection, fake, now=NOW)
    assert summary["kept"] == 1
    assert summary["lost"] == 0
    assert fake.removed == []
    connection.close()


def test_missing_active_container_releases_claim(tmp_path):
    connection = _connection(tmp_path)
    old = NOW - timedelta(hours=1)
    allocation_id = _allocation(connection, created_at=old, expires_at=NOW + timedelta(hours=1))
    store.activate_allocation(
        connection,
        allocation_id,
        container_id="missing-container",
        container_name="citespec-alice-test",
        now=old,
    )
    fake = FakeDocker()
    summary = reconcile(connection, fake, now=NOW)
    assert summary["lost"] == 1
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "failed"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 0
    connection.close()


def test_expired_active_container_is_removed(tmp_path):
    connection = _connection(tmp_path)
    old = NOW - timedelta(hours=2)
    allocation_id = _allocation(connection, created_at=old, expires_at=NOW - timedelta(minutes=1))
    store.activate_allocation(
        connection,
        allocation_id,
        container_id="container-1",
        container_name="citespec-alice-test",
        now=old,
    )
    fake = FakeDocker()
    fake.add_container(_container(allocation_id))
    summary = reconcile(connection, fake, now=NOW)
    assert summary["expired"] == 1
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "expired"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 0
    assert fake.removed == ["container-1"]
    connection.close()


def test_stopped_active_container_is_failed(tmp_path):
    connection = _connection(tmp_path)
    old = NOW - timedelta(hours=1)
    allocation_id = _allocation(connection, created_at=old, expires_at=NOW + timedelta(hours=1))
    store.activate_allocation(
        connection,
        allocation_id,
        container_id="container-1",
        container_name="citespec-alice-test",
        now=old,
    )
    fake = FakeDocker()
    fake.add_container(_container(allocation_id, status="exited"))
    summary = reconcile(connection, fake, now=NOW)
    assert summary["lost"] == 1
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "failed"
    assert fake.removed == ["container-1"]
    connection.close()


def test_unknown_container_is_never_removed(tmp_path):
    connection = _connection(tmp_path)
    fake = FakeDocker()
    fake.add_container(
        ContainerInfo(
            id="unknown",
            name="someone-else",
            image="other:latest",
            status="running",
            labels={},
        )
    )
    summary = reconcile(connection, fake, now=NOW)
    assert summary["unmanaged"] == 1
    assert "unknown" in fake.containers
    assert fake.removed == []
    connection.close()


def test_orphaned_managed_container_is_reported_not_removed(tmp_path):
    connection = _connection(tmp_path)
    fake = FakeDocker()
    fake.add_container(
        ContainerInfo(
            id="orphan",
            name="citespec-old",
            image="citespec-base:2026.10",
            status="running",
            labels={"citespec.managed": "true", "citespec.allocation": "missing-id"},
        )
    )
    summary = reconcile(connection, fake, now=NOW)
    assert summary["orphaned"] == 1
    assert "orphan" in fake.containers
    assert fake.removed == []
    connection.close()


def test_pending_failure_is_retried_when_removal_fails(tmp_path):
    connection = _connection(tmp_path)
    old = NOW - timedelta(minutes=10)
    allocation_id = _allocation(connection, created_at=old, expires_at=old + timedelta(hours=12))
    fake = FakeDocker(fail_remove=True)
    fake.add_container(_container(allocation_id))
    summary = reconcile(connection, fake, now=NOW)
    assert summary["pending_failed"] == 0
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "pending"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 1
    connection.close()


def test_reconcile_does_not_mutate_when_container_listing_fails(tmp_path):
    connection = _connection(tmp_path)
    old = NOW - timedelta(minutes=10)
    allocation_id = _allocation(connection, created_at=old, expires_at=old + timedelta(hours=12))

    class BrokenDocker(FakeDocker):
        def list_containers(self):
            raise RuntimeError("docker unavailable")

    summary = reconcile(connection, BrokenDocker(), now=NOW)
    assert summary["list_failed"] == 1
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "pending"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 1
    connection.close()


def test_pending_mismatched_container_label_is_not_removed(tmp_path):
    connection = _connection(tmp_path)
    old = NOW - timedelta(minutes=10)
    allocation_id = _allocation(connection, created_at=old, expires_at=old + timedelta(hours=12))
    fake = FakeDocker()
    fake.add_container(
        ContainerInfo(
            id="container-1",
            name="not-ours",
            image="old:latest",
            status="running",
            labels={"citespec.allocation": allocation_id},
        )
    )
    summary = reconcile(connection, fake, now=NOW)
    assert summary["unsafe_skipped"] == 1
    assert summary["pending_failed"] == 0
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "pending"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 1
    assert "container-1" in fake.containers
    assert fake.removed == []
    connection.close()


def test_active_expired_identity_mismatch_is_not_removed(tmp_path):
    connection = _connection(tmp_path)
    old = NOW - timedelta(hours=2)
    allocation_id = _allocation(connection, created_at=old, expires_at=NOW - timedelta(minutes=1))
    store.activate_allocation(
        connection,
        allocation_id,
        container_id="container-1",
        container_name="citespec-alice-test",
        now=old,
    )
    fake = FakeDocker()
    fake.add_container(
        ContainerInfo(
            id="container-1",
            name="not-ours",
            image="old:latest",
            status="running",
            labels={"citespec.allocation": allocation_id},
        )
    )
    summary = reconcile(connection, fake, now=NOW)
    assert summary["unsafe_skipped"] == 1
    assert summary["expired"] == 0
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "active"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 1
    assert "container-1" in fake.containers
    assert fake.removed == []
    connection.close()


def test_restarting_active_container_is_kept(tmp_path):
    """A restarting or paused container is live: never garbage-collected."""
    connection = _connection(tmp_path)
    old = NOW - timedelta(hours=1)
    allocation_id = _allocation(connection, created_at=old, expires_at=NOW + timedelta(hours=1))
    store.activate_allocation(
        connection,
        allocation_id,
        container_id="container-1",
        container_name="citespec-alice-test",
        now=old,
    )
    fake = FakeDocker()
    fake.add_container(_container(allocation_id, status="restarting"))
    summary = reconcile(connection, fake, now=NOW)
    assert summary["kept"] == 1
    assert fake.removed == []
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "active"
    connection.close()


def test_stopped_container_removal_failure_keeps_claims(tmp_path):
    """Removal failure must not release the GPU claim: the orphan may still
    hold the device. cleanup_pending defers finalization to the next pass."""
    connection = _connection(tmp_path)
    old = NOW - timedelta(hours=1)
    allocation_id = _allocation(connection, created_at=old, expires_at=NOW + timedelta(hours=1))
    store.activate_allocation(
        connection,
        allocation_id,
        container_id="container-1",
        container_name="citespec-alice-test",
        now=old,
    )
    fake = FakeDocker(fail_remove=True)
    fake.add_container(_container(allocation_id, status="exited"))
    summary = reconcile(connection, fake, now=NOW)
    assert summary["cleanup_failed"] == 1
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == ("cleanup_pending")
    # The claim still blocks the GPU.
    with pytest.raises(Exception, match="GPU"):
        store.create_allocation(
            connection,
            owner_uid=1001,
            owner_name="bob",
            gpu_uuids=("GPU-0",),
            cpus=8.0,
            memory_gb=32,
            image="citespec-base:2026.10",
            workspace="/data1/citespec-users/bob",
            note="",
            starts_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            max_cpus=32.0,
            max_memory_gb=128,
            now=NOW,
        )
    connection.close()


def test_cleanup_pending_is_finalized_once_removal_succeeds(tmp_path):
    connection = _connection(tmp_path)
    old = NOW - timedelta(hours=1)
    allocation_id = _allocation(connection, created_at=old, expires_at=NOW + timedelta(hours=1))
    store.activate_allocation(
        connection,
        allocation_id,
        container_id="container-1",
        container_name="citespec-alice-test",
        now=old,
    )
    fake = FakeDocker(fail_remove=True)
    fake.add_container(_container(allocation_id, status="exited"))
    reconcile(connection, fake, now=NOW)
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == ("cleanup_pending")
    fake.fail_remove = False
    summary = reconcile(connection, fake, now=NOW)
    assert summary["cleanup_retried"] == 1
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "failed"
    connection.close()
