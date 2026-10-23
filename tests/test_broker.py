import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes import FakeDocker, FakeIdentityResolver
from udc_resource import store
from udc_resource.broker import workspace_io
from udc_resource.broker.docker_driver import ContainerInfo, GPUProcess
from udc_resource.broker.server import Broker
from udc_resource.db import connect, init_db
from udc_resource.identity import Identity
from udc_resource.models import GPU

FIXED = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ALICE = Identity(uid=1000, gid=1000, username="alice", role="member", is_admin=False)
BOB = Identity(uid=1001, gid=1001, username="bob", role="member", is_admin=False)
ADMIN = Identity(uid=0, gid=0, username="root", role="admin", is_admin=True)


def _broker(
    tmp_path,
    config,
    *,
    docker: FakeDocker | None = None,
):
    fake_docker = docker or FakeDocker()
    resolver = FakeIdentityResolver(
        {
            ALICE.uid: ALICE,
            BOB.uid: BOB,
            ADMIN.uid: ADMIN,
        }
    )
    broker = Broker(
        config,
        db_path=tmp_path / "state.db",
        socket_path=tmp_path / "broker.sock",
        docker=fake_docker,
        identity_resolver=resolver,
        clock=lambda: FIXED,
    )
    broker.initialize()
    return broker, fake_docker


async def _call(broker: Broker, command: str, args: dict, identity: Identity) -> dict:
    return await broker.handle_request(
        {"cmd": command, "args": args},
        uid=identity.uid,
        gid=identity.gid,
    )


async def test_ping_returns_peer_identity(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    response = await _call(broker, "ping", {}, ALICE)
    assert response["ok"] is True
    assert response["data"]["uid"] == ALICE.uid
    assert response["data"]["username"] == "alice"
    assert response["data"]["admin"] is False
    assert response["data"]["mode"] == "active"


async def test_list_has_gpu_inventory(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    response = await _call(broker, "list", {}, ALICE)
    assert response["ok"] is True
    assert len(response["data"]["gpus"]) == 8
    assert response["data"]["gpus"][0]["state"] == "free"


async def test_apply_creates_active_allocation(tmp_path, active_config):
    broker, docker = _broker(tmp_path, active_config)
    response = await _call(
        broker,
        "apply",
        {"gpus": [3, 5], "cpus": 8, "memory_gb": 32, "hours": 12},
        ALICE,
    )
    assert response["ok"] is True
    allocation = response["data"]
    assert allocation["status"] == "active"
    assert allocation["owner"] == "alice"
    assert allocation["gpus"] == [3, 5]
    assert allocation["container_name"]
    assert len(docker.created) == 1

    listed = await _call(broker, "list", {}, ALICE)
    gpu3 = next(gpu for gpu in listed["data"]["gpus"] if gpu["index"] == 3)
    assert gpu3["state"] == "allocated"
    assert gpu3["allocation_id"] == allocation["id"]


async def test_second_apply_for_same_gpu_is_busy(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    first = await _call(
        broker,
        "apply",
        {"gpus": [3], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert first["ok"] is True
    second = await _call(
        broker,
        "apply",
        {"gpus": [3], "cpus": 8, "memory_gb": 32},
        BOB,
    )
    assert second["ok"] is False
    assert second["error"]["code"] == "gpu_busy"


async def test_monitor_mode_rejects_apply(tmp_path, monitor_config):
    broker, _ = _broker(tmp_path, monitor_config)
    response = await _call(
        broker,
        "apply",
        {"gpus": [0], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "monitor_only"


@pytest.mark.parametrize("hours", [0, 37])
async def test_invalid_hours_are_rejected(tmp_path, active_config, hours):
    broker, _ = _broker(tmp_path, active_config)
    response = await _call(
        broker,
        "apply",
        {"gpus": [0], "cpus": 8, "memory_gb": 32, "hours": hours},
        ALICE,
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "bad_request"


async def test_cpu_policy_limit_is_enforced(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    response = await _call(
        broker,
        "apply",
        {"gpus": [0], "cpus": 999, "memory_gb": 32},
        ALICE,
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "insufficient_resources"


async def test_docker_failure_rolls_back_claim(tmp_path, active_config):
    broker, _ = _broker(
        tmp_path,
        active_config,
        docker=FakeDocker(fail_create=True),
    )
    response = await _call(
        broker,
        "apply",
        {"gpus": [4], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "docker_error"
    connection = connect(tmp_path / "state.db")
    try:
        assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 0
        rows = store.list_allocations(connection, owner_uid=ALICE.uid, now=FIXED)
        assert rows[0]["status"] == "failed"
    finally:
        connection.close()


async def test_status_is_owner_or_admin_only(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    applied = await _call(
        broker,
        "apply",
        {"gpus": [1], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    allocation_id = applied["data"]["id"]
    owner = await _call(broker, "status", {"id": allocation_id}, ALICE)
    assert owner["ok"] is True
    other = await _call(broker, "status", {"id": allocation_id}, BOB)
    assert other["ok"] is False
    assert other["error"]["code"] == "forbidden"
    admin = await _call(broker, "status", {"id": allocation_id}, ADMIN)
    assert admin["ok"] is True


async def test_admin_commands_require_admin(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    forbidden = await _call(broker, "admin_users", {}, ALICE)
    assert forbidden["ok"] is False
    assert forbidden["error"]["code"] == "forbidden"
    allowed = await _call(broker, "admin_users", {}, ADMIN)
    assert allowed["ok"] is True
    usernames = {user["username"] for user in allowed["data"]["users"]}
    assert "alice" in usernames


async def test_admin_mode_switch(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    switched = await _call(broker, "admin_mode", {"mode": "monitor"}, ADMIN)
    assert switched["ok"] is True
    blocked = await _call(
        broker,
        "apply",
        {"gpus": [0], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert blocked["error"]["code"] == "monitor_only"
    switched_back = await _call(broker, "admin_mode", {"mode": "active"}, ADMIN)
    assert switched_back["ok"] is True
    applied = await _call(
        broker,
        "apply",
        {"gpus": [0], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert applied["ok"] is True


async def test_persisted_active_is_forced_to_monitor_without_approval(tmp_path, monitor_config):
    connection = init_db(tmp_path / "state.db")
    store.set_mode(connection, "active", actor_uid=0, now=FIXED)
    connection.close()
    broker, _ = _broker(tmp_path, monitor_config)
    response = await _call(broker, "ping", {}, ALICE)
    assert response["data"]["mode"] == "monitor"


async def test_admin_cannot_enable_active_without_approval(tmp_path, monitor_config):
    broker, _ = _broker(tmp_path, monitor_config)
    response = await _call(broker, "admin_mode", {"mode": "active"}, ADMIN)
    assert response["ok"] is False
    assert response["error"]["code"] == "forbidden"


async def test_audit_events_are_available_to_admin(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    await _call(
        broker,
        "apply",
        {"gpus": [2], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    response = await _call(broker, "admin_audit", {"limit": 10}, ADMIN)
    assert response["ok"] is True
    actions = [event["action"] for event in response["data"]["events"]]
    assert "allocation_pending" in actions
    assert "allocation_active" in actions


async def test_unknown_command_and_duplicate_gpus_are_rejected(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    unknown = await _call(broker, "launch", {}, ALICE)
    assert unknown["error"]["code"] == "bad_request"
    duplicate = await _call(
        broker,
        "apply",
        {"gpus": [3, 3], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert duplicate["error"]["code"] == "bad_request"


async def test_apply_creates_workspace(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    response = await _call(
        broker,
        "apply",
        {"gpus": [0], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert response["ok"] is True
    workspace = Path(active_config.server.workspace_root) / ALICE.username
    assert workspace.is_dir()


async def test_apply_rejects_gpu_with_unmanaged_process(tmp_path, active_config):
    fake = FakeDocker()
    fake.add_process(GPUProcess(gpu_uuid="GPU-3", pid=4242, used_mib=1024, process_name="python"))
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    response = await _call(
        broker,
        "apply",
        {"gpus": [3], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "gpu_busy"
    assert fake.created == []


async def test_release_is_owner_or_admin_only(tmp_path, active_config):
    broker, docker = _broker(tmp_path, active_config)
    applied = await _call(
        broker,
        "apply",
        {"gpus": [1], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    allocation_id = applied["data"]["id"]
    denied = await _call(broker, "release", {"id": allocation_id}, BOB)
    assert denied["ok"] is False
    assert denied["error"]["code"] == "forbidden"
    assert docker.removed == []
    released = await _call(broker, "release", {"id": allocation_id}, ALICE)
    assert released["ok"] is True
    assert released["data"]["status"] == "released"
    assert docker.removed == [applied["data"]["container_id"]]
    status = await _call(broker, "status", {"id": allocation_id}, ALICE)
    assert status["data"]["allocation"]["status"] == "released"


async def test_admin_release_records_reason(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    applied = await _call(
        broker,
        "apply",
        {"gpus": [1], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    allocation_id = applied["data"]["id"]
    released = await _call(
        broker,
        "admin_release",
        {"id": allocation_id, "reason": "maintenance"},
        ADMIN,
    )
    assert released["ok"] is True
    assert released["data"]["status"] == "released"
    connection = connect(tmp_path / "state.db")
    try:
        events = store.list_allocation_events(connection, allocation_id)
    finally:
        connection.close()
    released_event = next(event for event in events if event["action"] == "allocation_released")
    assert released_event["detail"]["reason"] == "maintenance"
    assert released_event["actor_uid"] == ADMIN.uid


async def test_list_reports_unmanaged_containers_and_processes(tmp_path, active_config):
    fake = FakeDocker()
    fake.add_container(
        ContainerInfo(
            id="other-container",
            name="other",
            image="other:latest",
            status="running",
            labels={},
        )
    )
    fake.add_process(GPUProcess(gpu_uuid="GPU-7", pid=999, used_mib=2048, process_name="train"))
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    response = await _call(broker, "list", {}, ALICE)
    assert response["ok"] is True
    unmanaged = response["data"]["unmanaged"]
    assert [container["id"] for container in unmanaged["containers"]] == ["other-container"]
    assert [process["pid"] for process in unmanaged["processes"]] == [999]
    assert unmanaged["processes"][0]["gpu_index"] == 7


async def test_admin_reconcile_keeps_running_allocation(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    applied = await _call(
        broker,
        "apply",
        {"gpus": [0], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert applied["ok"] is True
    response = await _call(broker, "admin_reconcile", {}, ADMIN)
    assert response["ok"] is True
    assert response["data"]["kept"] == 1
    assert response["data"]["lost"] == 0


async def test_member_cannot_select_custom_image(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    denied = await _call(
        broker,
        "apply",
        {
            "gpus": [0],
            "cpus": 8,
            "memory_gb": 32,
            "image": "untrusted:latest",
        },
        ALICE,
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "forbidden"
    allowed = await _call(
        broker,
        "apply",
        {
            "gpus": [0],
            "cpus": 8,
            "memory_gb": 32,
            "image": "admin-image:latest",
        },
        ADMIN,
    )
    assert allowed["ok"] is True


async def test_release_pending_allocation_removes_container_by_label(tmp_path, active_config):
    fake = FakeDocker()
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    connection = init_db(tmp_path / "state.db")
    try:
        store.upsert_gpus(
            connection, [GPU(index=0, uuid="GPU-0", name="RTX 4090", memory_mib=49140)]
        )
        allocation_id = store.create_allocation(
            connection,
            owner_uid=ALICE.uid,
            owner_name=ALICE.username,
            gpu_uuids=("GPU-0",),
            cpus=8.0,
            memory_gb=32,
            image=active_config.image.base,
            workspace="/tmp/alice",
            note="",
            starts_at=FIXED,
            expires_at=FIXED.replace(hour=23),
            max_cpus=active_config.policy.max_cpus,
            max_memory_gb=active_config.policy.max_memory_gb,
            now=FIXED,
        )
    finally:
        connection.close()
    fake.add_container(
        ContainerInfo(
            id="pending-container",
            name="udc-alice-pending",
            image=active_config.image.base,
            status="running",
            labels={
                "udc.managed": "true",
                "udc.owner": ALICE.username,
                "udc.allocation": allocation_id,
            },
        )
    )
    response = await _call(broker, "release", {"id": allocation_id}, ALICE)
    assert response["ok"] is True
    assert response["data"]["status"] == "released"
    assert fake.removed == ["pending-container"]


async def test_cpu_only_allocation_is_supported(tmp_path, active_config):
    broker, docker = _broker(tmp_path, active_config)
    response = await _call(
        broker,
        "apply",
        {"cpus": 4, "memory_gb": 8, "hours": 1},
        ALICE,
    )
    assert response["ok"] is True
    assert response["data"]["gpus"] == []
    assert response["data"]["status"] == "active"
    assert len(docker.created) == 1
    connection = connect(tmp_path / "state.db")
    try:
        assert connection.execute("SELECT COUNT(*) FROM active_gpu_claims").fetchone()[0] == 0
    finally:
        connection.close()


async def test_monitor_mode_does_not_stop_or_reconcile(tmp_path, monitor_config):
    fake = FakeDocker()
    broker, _ = _broker(tmp_path, monitor_config, docker=fake)
    connection = init_db(tmp_path / "state.db")
    try:
        store.upsert_gpus(
            connection, [GPU(index=0, uuid="GPU-0", name="RTX 4090", memory_mib=49140)]
        )
        allocation_id = store.create_allocation(
            connection,
            owner_uid=ALICE.uid,
            owner_name=ALICE.username,
            gpu_uuids=("GPU-0",),
            cpus=8.0,
            memory_gb=32,
            image=monitor_config.image.base,
            workspace="/tmp/alice",
            note="",
            starts_at=FIXED.replace(hour=0),
            expires_at=FIXED.replace(hour=1),
            max_cpus=monitor_config.policy.max_cpus,
            max_memory_gb=monitor_config.policy.max_memory_gb,
            now=FIXED.replace(hour=0),
        )
        store.activate_allocation(
            connection,
            allocation_id,
            container_id="existing-container",
            container_name="udc-alice-existing",
            now=FIXED.replace(hour=0),
        )
    finally:
        connection.close()
    fake.add_container(
        ContainerInfo(
            id="existing-container",
            name="udc-alice-existing",
            image=monitor_config.image.base,
            status="running",
            labels={
                "udc.managed": "true",
                "udc.owner": ALICE.username,
                "udc.allocation": allocation_id,
            },
        )
    )
    assert await broker.expire_once() == []
    assert fake.removed == []
    assert broker.reconcile_once() == {"skipped": 1}


async def test_managed_container_processes_are_not_listed_as_unmanaged(
    tmp_path, active_config, monkeypatch
):
    fake = FakeDocker()
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    applied = await _call(
        broker,
        "apply",
        {"gpus": [3], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert applied["ok"] is True
    fake.add_process(GPUProcess(gpu_uuid="GPU-3", pid=1234, used_mib=1024, process_name="python"))
    monkeypatch.setattr(Broker, "_pid_in_container", staticmethod(lambda pid, cid: True))
    response = await _call(broker, "list", {}, ALICE)
    assert response["data"]["unmanaged"]["processes"] == []


async def test_logs_are_owner_or_admin_only(tmp_path, active_config):
    fake = FakeDocker()
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    applied = await _call(
        broker,
        "apply",
        {"gpus": [2], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    allocation_id = applied["data"]["id"]
    container_id = applied["data"]["container_id"]
    fake.container_logs[container_id] = "hello from container\n"
    owner = await _call(broker, "logs", {"id": allocation_id, "tail": 10}, ALICE)
    assert owner["ok"] is True
    assert owner["data"]["logs"] == "hello from container\n"
    denied = await _call(broker, "logs", {"id": allocation_id}, BOB)
    assert denied["ok"] is False
    assert denied["error"]["code"] == "forbidden"
    admin = await _call(broker, "logs", {"id": allocation_id}, ADMIN)
    assert admin["ok"] is True


async def test_list_marks_compute_process_as_unmanaged(tmp_path, active_config):
    fake = FakeDocker()
    fake.add_process(GPUProcess(gpu_uuid="GPU-3", pid=4242, used_mib=1024, process_name="python"))
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    response = await _call(broker, "list", {}, ALICE)
    gpu = next(item for item in response["data"]["gpus"] if item["index"] == 3)
    assert gpu["state"] == "unmanaged"
    assert gpu["compute_pids"] == [4242]


async def test_list_marks_container_gpu_holder_as_unmanaged(tmp_path, active_config):
    fake = FakeDocker()
    fake.add_container(
        ContainerInfo(
            id="old-container",
            name="old-training",
            image="old:latest",
            status="running",
            labels={},
            gpu_refs=("GPU-4",),
        )
    )
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    response = await _call(broker, "list", {}, ALICE)
    gpu = next(item for item in response["data"]["gpus"] if item["index"] == 4)
    assert gpu["state"] == "unmanaged"
    assert gpu["unmanaged_container_ids"] == ["old-container"]
    unmanaged = response["data"]["unmanaged"]["containers"][0]
    assert unmanaged["gpus"] == [4]


async def test_list_marks_unknown_when_live_detection_fails(tmp_path, active_config, monkeypatch):
    fake = FakeDocker()
    broker, _ = _broker(tmp_path, active_config, docker=fake)

    def fail():
        raise RuntimeError("nvidia-smi failed")

    monkeypatch.setattr(fake, "list_gpus", fail)
    response = await _call(broker, "list", {}, ALICE)
    assert response["ok"] is True
    assert response["data"]["detection_error"]
    assert {gpu["state"] for gpu in response["data"]["gpus"]} == {"unknown"}


async def test_apply_rejects_container_gpu_holder_without_compute(tmp_path, active_config):
    fake = FakeDocker()
    fake.add_container(
        ContainerInfo(
            id="old-container",
            name="old-training",
            image="old:latest",
            status="running",
            labels={},
            gpu_refs=("GPU-3",),
        )
    )
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    response = await _call(
        broker,
        "apply",
        {"gpus": [3], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "gpu_busy"


async def test_admin_active_blocked_by_unmanaged_gpu_holder(tmp_path, active_config):
    fake = FakeDocker()
    fake.add_container(
        ContainerInfo(
            id="old-container",
            name="old-training",
            image="old:latest",
            status="running",
            labels={},
            gpu_refs=("GPU-5",),
        )
    )
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    response = await _call(broker, "admin_mode", {"mode": "active"}, ADMIN)
    assert response["ok"] is False
    assert response["error"]["code"] == "conflict"
    assert "unmanaged" in response["error"]["message"]


async def test_apply_fails_closed_when_live_detection_fails(tmp_path, active_config, monkeypatch):
    fake = FakeDocker()
    broker, _ = _broker(tmp_path, active_config, docker=fake)

    def fail():
        raise RuntimeError("nvidia-smi failed")

    monkeypatch.setattr(fake, "list_gpus", fail)
    response = await _call(
        broker,
        "apply",
        {"gpus": [3], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "docker_error"


async def test_admin_monitor_blocked_with_active_lease(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    applied = await _call(
        broker,
        "apply",
        {"gpus": [1], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert applied["ok"] is True
    blocked = await _call(broker, "admin_mode", {"mode": "monitor"}, ADMIN)
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "conflict"
    assert "maintenance" in blocked["error"]["message"]


async def test_maintenance_keeps_expiry_and_blocks_new_apply(tmp_path, active_config):
    broker, _ = _broker(tmp_path, active_config)
    applied = await _call(
        broker,
        "apply",
        {"gpus": [1], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    allocation_id = applied["data"]["id"]
    switched = await _call(broker, "admin_mode", {"mode": "maintenance"}, ADMIN)
    assert switched["ok"] is True
    blocked = await _call(
        broker,
        "apply",
        {"gpus": [2], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert blocked["error"]["code"] == "monitor_only"
    connection = connect(tmp_path / "state.db")
    try:
        connection.execute(
            "UPDATE allocations SET expires_at = ? WHERE id = ?",
            ((FIXED - timedelta(minutes=1)).isoformat(), allocation_id),
        )
        connection.commit()
    finally:
        connection.close()
    expired = await broker.expire_once()
    assert expired == [allocation_id]
    status = await _call(broker, "status", {"id": allocation_id}, ALICE)
    assert status["data"]["allocation"]["status"] == "expired"


async def test_initialize_promotes_monitor_with_lease_to_maintenance(tmp_path, monitor_config):
    connection = init_db(tmp_path / "state.db")
    try:
        store.upsert_gpus(
            connection, [GPU(index=0, uuid="GPU-0", name="RTX 4090", memory_mib=49140)]
        )
        allocation_id = store.create_allocation(
            connection,
            owner_uid=ALICE.uid,
            owner_name=ALICE.username,
            gpu_uuids=("GPU-0",),
            cpus=8.0,
            memory_gb=32,
            image=monitor_config.image.base,
            workspace="/tmp/alice",
            note="",
            starts_at=FIXED.replace(hour=0),
            expires_at=FIXED.replace(hour=1),
            max_cpus=monitor_config.policy.max_cpus,
            max_memory_gb=monitor_config.policy.max_memory_gb,
            now=FIXED.replace(hour=0),
        )
        store.activate_allocation(
            connection,
            allocation_id,
            container_id="existing-container",
            container_name="udc-alice-existing",
            now=FIXED.replace(hour=0),
        )
    finally:
        connection.close()
    broker, _ = _broker(tmp_path, monitor_config)
    response = await _call(broker, "ping", {}, ALICE)
    assert response["data"]["mode"] == "maintenance"


async def test_stopped_gpu_container_does_not_block_new_allocation(tmp_path, active_config):
    fake = FakeDocker()
    fake.add_container(
        ContainerInfo(
            id="old-stopped",
            name="old-stopped",
            image="old:latest",
            status="exited",
            labels={},
            gpu_refs=("GPU-4",),
        )
    )
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    before = await _call(broker, "list", {}, ALICE)
    gpu = next(item for item in before["data"]["gpus"] if item["index"] == 4)
    assert gpu["state"] == "free"
    history = next(
        item for item in before["data"]["unmanaged"]["containers"] if item["id"] == "old-stopped"
    )
    assert history["active_gpu_access"] is False
    response = await _call(
        broker,
        "apply",
        {"gpus": [4], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    assert response["ok"] is True


async def test_unmanaged_process_reports_owner(tmp_path, active_config):
    fake = FakeDocker()
    fake.add_process(
        GPUProcess(
            gpu_uuid="GPU-7",
            pid=4321,
            used_mib=512,
            process_name="python",
            uid=1001,
            username="bob",
        )
    )
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    response = await _call(broker, "list", {}, ALICE)
    process = response["data"]["unmanaged"]["processes"][0]
    assert process["uid"] == 1001
    assert process["username"] == "bob"


async def test_file_transfer_is_disabled_by_default(tmp_path, active_config):
    config = replace(
        active_config,
        policy=replace(active_config.policy, file_transfer_enabled=False),
    )
    broker, _ = _broker(tmp_path, config)

    class Writer:
        def __init__(self) -> None:
            self.data = bytearray()

        def write(self, data: bytes) -> None:
            self.data.extend(data)

        async def drain(self) -> None:
            return None

    writer = Writer()
    await broker._handle_file(
        {"cmd": "file_put", "args": {"id": "missing", "path": "data.bin"}},
        reader=asyncio.StreamReader(),
        writer=writer,  # type: ignore[arg-type]
        uid=ALICE.uid,
        gid=ALICE.gid,
    )
    response = json.loads(writer.data.decode("utf-8"))
    assert response["ok"] is False
    assert response["error"]["code"] == "forbidden"
    assert "disabled" in response["error"]["message"]


async def test_list_enriches_public_member_identity_and_note(tmp_path, active_config):
    members = tmp_path / "members.toml"
    members.write_text(
        """
[members.alice]
display_name = "Alice Zhang"
nickname = "alice"
public_contact = "alice@example.invalid"
private_phone = "+1-555-0100"
""",
        encoding="utf-8",
    )
    config = replace(
        active_config,
        server=replace(active_config.server, members_file=str(members)),
    )
    broker, _ = _broker(tmp_path, config)
    applied = await _call(
        broker,
        "apply",
        {"gpus": [0], "cpus": 4, "memory_gb": 8, "note": "needs the large model"},
        ALICE,
    )
    assert applied["ok"] is True
    listed = await _call(broker, "list", {}, ALICE)
    allocation = next(
        item for item in listed["data"]["allocations"] if item["id"] == applied["data"]["id"]
    )
    assert allocation["note"] == "needs the large model"
    assert allocation["owner_display"] == "Alice Zhang"
    assert allocation["owner_nickname"] == "alice"
    assert allocation["owner_public_contact"] == "alice@example.invalid"
    assert allocation["owner"] == "alice"
    assert "private_phone" not in json.dumps(listed["data"])
    assert listed["data"]["members"]["alice"]["display_name"] == "Alice Zhang"


@pytest.mark.linux
@pytest.mark.skipif(
    not workspace_io.supported(),
    reason="descriptor-relative job logs require Linux",
)
async def test_job_lifecycle(tmp_path, active_config):
    fake = FakeDocker()
    broker, _ = _broker(tmp_path, active_config, docker=fake)
    applied = await _call(
        broker,
        "apply",
        {"gpus": [2], "cpus": 8, "memory_gb": 32},
        ALICE,
    )
    allocation_id = applied["data"]["id"]
    container_id = applied["data"]["container_id"]
    started = await _call(
        broker,
        "job_start",
        {"id": allocation_id, "name": "train", "command": ["python", "train.py"]},
        ALICE,
    )
    assert started["ok"] is True
    assert started["data"]["session"] == "udc-train"
    assert "train" in fake.jobs[container_id]
    log_path = (
        Path(active_config.server.workspace_root) / ALICE.username / ".udc-logs" / "train.log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"epoch 1\nepoch 2\n")
    listed = await _call(broker, "job_list", {"id": allocation_id}, ALICE)
    assert listed["data"]["jobs"][0]["name"] == "train"
    assert listed["data"]["jobs"][0]["log_bytes"] > 0
    logs = await _call(broker, "job_logs", {"id": allocation_id, "name": "train"}, ALICE)
    assert logs["data"]["logs"] == "epoch 1\nepoch 2\n"
    stopped = await _call(broker, "job_stop", {"id": allocation_id, "name": "train"}, ALICE)
    assert stopped["ok"] is True
    assert "train" not in fake.jobs[container_id]
