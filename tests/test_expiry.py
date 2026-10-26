import sqlite3
from datetime import UTC, datetime, timedelta

from citespec_resource import store
from citespec_resource.broker.docker_driver import ContainerInfo
from citespec_resource.broker.expiry import expire_once
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


def _active_allocation(
    connection: sqlite3.Connection,
    *,
    expires_at: datetime,
    container_id: str = "container-1",
) -> str:
    allocation_id = store.create_allocation(
        connection,
        owner_uid=1000,
        owner_name="alice",
        gpu_uuids=("GPU-0",),
        cpus=8.0,
        memory_gb=32,
        image="citespec-base:2026.10",
        workspace="/data1/citespec-users/alice",
        note="",
        starts_at=NOW - timedelta(hours=12),
        expires_at=expires_at,
        max_cpus=32.0,
        max_memory_gb=128,
        now=NOW - timedelta(hours=12),
    )
    store.activate_allocation(
        connection,
        allocation_id,
        container_id=container_id,
        container_name="citespec-alice-test",
        now=NOW - timedelta(hours=12),
    )
    return allocation_id


def _add_managed_container(fake: FakeDocker, allocation_id: str) -> None:
    fake.add_container(
        ContainerInfo(
            id="container-1",
            name="citespec-alice-test",
            image="citespec-base:2026.10",
            status="running",
            labels={
                "citespec.managed": "true",
                "citespec.owner": "alice",
                "citespec.allocation": allocation_id,
            },
        )
    )


def test_expire_once_stops_container_and_releases_claim(tmp_path):
    connection = _connection(tmp_path)
    allocation_id = _active_allocation(connection, expires_at=NOW - timedelta(minutes=1))
    fake = FakeDocker()
    _add_managed_container(fake, allocation_id)
    expired = expire_once(connection, fake, now=NOW)
    assert expired == [allocation_id]
    allocation = store.get_allocation(connection, allocation_id, now=NOW)
    assert allocation["status"] == "expired"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 0
    assert fake.removed == ["container-1"]
    events = store.list_allocation_events(connection, allocation_id)
    assert events[0]["action"] == "allocation_expired"
    connection.close()


def test_expire_once_keeps_unexpired_allocation(tmp_path):
    connection = _connection(tmp_path)
    allocation_id = _active_allocation(connection, expires_at=NOW + timedelta(hours=1))
    fake = FakeDocker()
    _add_managed_container(fake, allocation_id)
    assert expire_once(connection, fake, now=NOW) == []
    allocation = store.get_allocation(connection, allocation_id, now=NOW)
    assert allocation["status"] == "active"
    assert fake.removed == []
    connection.close()


def test_expire_once_retries_when_container_removal_fails(tmp_path):
    connection = _connection(tmp_path)
    allocation_id = _active_allocation(connection, expires_at=NOW - timedelta(minutes=1))
    fake = FakeDocker(fail_remove=True)
    _add_managed_container(fake, allocation_id)
    assert expire_once(connection, fake, now=NOW) == []
    allocation = store.get_allocation(connection, allocation_id, now=NOW)
    assert allocation["status"] == "active"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 1
    connection.close()


def test_expire_once_skips_mismatched_container_identity(tmp_path):
    connection = _connection(tmp_path)
    allocation_id = _active_allocation(connection, expires_at=NOW - timedelta(minutes=1))
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
    assert expire_once(connection, fake, now=NOW) == []
    assert store.get_allocation(connection, allocation_id, now=NOW)["status"] == "active"
    assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 1
    assert fake.removed == []
    assert "container-1" in fake.containers
    connection.close()
