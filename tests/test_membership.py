"""Membership authorization and lifecycle tests (goal §6, AC-16/17/21)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from citespec_resource import store
from citespec_resource.membership import store as member_store
from citespec_resource.membership.models import parse_public_key
from tests.test_broker import ADMIN, ALICE, FIXED, _broker, _call


@pytest.fixture
def member_config(active_config):
    return replace(active_config, policy=replace(active_config.policy, membership_required=True))


def _provision_alice(connection, *, status: str = "active", valid_until: str = "") -> str:
    return member_store.create_member(
        connection,
        linux_uid=ALICE.uid,
        linux_username=ALICE.username,
        status=status,
        valid_until=valid_until,
        display_name="Alice Zhang",
    )


async def test_apply_requires_active_member(tmp_path, member_config):
    broker, docker = _broker(tmp_path, member_config)
    response = await _call(broker, "apply", {"gpus": [1], "cpus": 8, "memory_gb": 32}, ALICE)
    assert response["ok"] is False
    assert response["error"]["code"] == "membership_required"

    # Provisioning a record with a non-active status keeps the gate closed.
    with broker._connection() as connection:
        _provision_alice(connection, status="suspended")
    suspended = await _call(broker, "apply", {"gpus": [1], "cpus": 8, "memory_gb": 32}, ALICE)
    assert suspended["ok"] is False
    assert suspended["error"]["code"] == "membership_suspended"

    # Activating the record opens the normal self-service path (INV-01: no
    # per-use approval).
    with broker._connection() as connection:
        member_store.transition_member(
            connection,
            _member_id_for(connection, ALICE.uid),
            target="active",
            reason="provisioning complete",
            actor_uid=ADMIN.uid,
        )
    allowed = await _call(broker, "apply", {"gpus": [1], "cpus": 8, "memory_gb": 32}, ALICE)
    assert allowed["ok"] is True
    assert docker.created


def _member_id_for(connection, uid: int) -> str:
    member = member_store.get_member_by_uid(connection, uid)
    assert member is not None
    return member.member_id


async def test_shell_and_tunnel_denied_for_suspended(tmp_path, member_config):
    config = replace(
        member_config,
        policy=replace(
            member_config.policy,
            vscode_enabled=True,
            container_profile="development",
            container_read_only=False,
            container_sudo=True,
            writable_layer_gb=20,
            min_free_docker_gb=1,
            docker_storage_budget_gb=200,
        ),
    )
    broker, _ = _broker(tmp_path, config)
    with broker._connection() as connection:
        _provision_alice(connection, status="suspended")
    # The shell handler needs a writer; exercise the shared gate through the
    # same helper the handler uses instead of faking the stream objects.
    with broker._connection() as connection:
        denial = broker._membership_denial(connection, ALICE, "shell")
    assert denial is not None
    assert denial["code"] == "membership_suspended"
    with broker._connection() as connection:
        tunnel_denial = broker._membership_denial(connection, ALICE, "tunnel")
    assert tunnel_denial is not None


async def test_admin_is_exempt_from_membership_gate(tmp_path, member_config):
    broker, docker = _broker(tmp_path, member_config)
    response = await _call(broker, "apply", {"gpus": [1], "cpus": 8, "memory_gb": 32}, ADMIN)
    assert response["ok"] is True
    assert docker.created


async def test_expired_member_is_denied_immediately(tmp_path, member_config):
    """AC-21: expiry is checked per authorization call, not by a periodic job."""
    broker, _ = _broker(tmp_path, member_config)
    past = (FIXED - timedelta(days=1)).isoformat()
    with broker._connection() as connection:
        _provision_alice(connection, status="active", valid_until=past)
    response = await _call(broker, "apply", {"gpus": [1], "cpus": 8, "memory_gb": 32}, ALICE)
    assert response["ok"] is False
    assert response["error"]["code"] == "membership_expired"


async def test_allocation_never_outlives_membership(tmp_path, member_config):
    """AC-19 boundary: expires_at is clamped to the member's valid_until."""
    deadline = FIXED + timedelta(hours=2)
    config = replace(
        member_config,
        policy=replace(member_config.policy, max_hours=36),
    )
    broker, _ = _broker(tmp_path, config)
    with broker._connection() as connection:
        _provision_alice(connection, valid_until=deadline.isoformat())
    response = await _call(
        broker,
        "apply",
        {"gpus": [1], "cpus": 8, "memory_gb": 32, "hours": 12},
        ALICE,
    )
    assert response["ok"] is True
    assert response["data"]["expires_at"] == deadline.isoformat()


async def test_suspended_member_queue_requests_fail_at_admission(tmp_path, member_config):
    """AC-17: a queued request from a suspended member cannot be admitted."""
    broker, fake = _broker(tmp_path, member_config)
    with broker._connection() as connection:
        _provision_alice(connection, status="active")
    # GPU 0 busy so alice's apply queues.
    fake.add_process(
        __import__(
            "citespec_resource.broker.docker_driver",
            fromlist=["GPUProcess"],
        ).GPUProcess(gpu_uuid="GPU-0", pid=4242, used_mib=1024, process_name="python")
    )
    queued = await _call(broker, "apply", {"gpus": [0], "cpus": 8, "memory_gb": 32}, ALICE)
    assert queued["data"]["status"] == "queued"

    # Suspend after queueing.
    with broker._connection() as connection:
        member_store.transition_member(
            connection,
            _member_id_for(connection, ALICE.uid),
            target="suspended",
            reason="leave of absence",
            actor_uid=ADMIN.uid,
        )
    admitted = await broker._schedule_queue()
    assert admitted == []
    with broker._connection() as connection:
        request = store.get_queue_request(connection, queued["data"]["queue_id"], now=FIXED)
    assert request["status"] == "failed"


async def test_maintenance_blocks_new_jobs_but_allows_release(tmp_path, member_config):
    broker, fake = _broker(tmp_path, member_config)
    with broker._connection() as connection:
        _provision_alice(connection, status="active")
    applied = await _call(broker, "apply", {"gpus": [1], "cpus": 8, "memory_gb": 32}, ALICE)
    assert applied["ok"] is True
    allocation_id = applied["data"]["id"]
    switched = await _call(broker, "admin_mode", {"mode": "maintenance"}, ADMIN)
    assert switched["ok"] is True
    blocked = await _call(
        broker,
        "job_start",
        {"id": allocation_id, "name": "second", "command": ["python", "train.py"]},
        ALICE,
    )
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "monitor_only"
    assert fake.started_job_commands == []

    released = await _call(broker, "release", {"id": allocation_id}, ALICE)
    assert released["ok"] is True
    assert released["data"]["status"] == "released"


async def test_maintenance_mode_applies_when_membership_gate_is_disabled(tmp_path, member_config):
    config = replace(
        member_config,
        policy=replace(member_config.policy, membership_required=False),
    )
    broker, _ = _broker(tmp_path, config)
    assert (await _call(broker, "admin_mode", {"mode": "maintenance"}, ADMIN))["ok"] is True

    with broker._connection() as connection:
        denial = broker._membership_denial(connection, ALICE, "shell")
    assert denial is not None
    assert denial["code"] == "monitor_only"


async def test_admin_revoke_closes_already_authenticated_broker_streams(tmp_path, member_config):
    broker, _ = _broker(tmp_path, member_config, provisioning=_FakeProvisioning())
    with broker._connection() as connection:
        member_id = _provision_alice(connection)

    class Writer:
        closed = False

        def close(self) -> None:
            self.closed = True

    writer = Writer()
    broker._client_writers[ALICE.uid] = {writer}  # type: ignore[assignment]

    result = await _call(
        broker,
        "admin_members_revoke",
        {"member_id": member_id, "reason": "trial ended"},
        ADMIN,
    )

    assert result["ok"] is True
    assert writer.closed is True


async def test_maintenance_blocks_streaming_handlers_for_members(tmp_path, member_config):
    broker, _ = _broker(tmp_path, member_config)
    with broker._connection() as connection:
        _provision_alice(connection, status="active")
    applied = await _call(broker, "apply", {"gpus": [1], "cpus": 8, "memory_gb": 32}, ALICE)
    assert applied["ok"] is True
    assert (await _call(broker, "admin_mode", {"mode": "maintenance"}, ADMIN))["ok"] is True

    with broker._connection() as connection:
        for command in ("shell", "tunnel", "file_get", "file_put"):
            denial = broker._membership_denial(connection, ALICE, command)
            assert denial is not None
            assert denial["code"] == "monitor_only"
        assert broker._membership_denial(connection, ADMIN, "shell") is None
        assert broker._membership_denial(connection, ALICE, "release") is None


async def test_maintenance_still_enforces_suspended_membership(tmp_path, member_config):
    broker, _ = _broker(tmp_path, member_config)
    with broker._connection() as connection:
        _provision_alice(connection, status="active")
    applied = await _call(broker, "apply", {"gpus": [1], "cpus": 8, "memory_gb": 32}, ALICE)
    allocation_id = applied["data"]["id"]
    assert (await _call(broker, "admin_mode", {"mode": "maintenance"}, ADMIN))["ok"] is True
    with broker._connection() as connection:
        member_store.transition_member(
            connection,
            _member_id_for(connection, ALICE.uid),
            target="suspended",
            reason="pause",
            actor_uid=ADMIN.uid,
        )
    # Mode permits release, but membership is checked independently and denies
    # a suspended member's cleanup command.
    blocked = await _call(broker, "release", {"id": allocation_id}, ALICE)
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "membership_suspended"


async def test_idempotent_apply_returns_same_allocation(tmp_path, member_config):
    """AC-06: a retried apply (client timeout) must not create a second env."""
    broker, docker = _broker(tmp_path, member_config)
    with broker._connection() as connection:
        _provision_alice(connection)
    args = {
        "gpus": [1],
        "cpus": 8,
        "memory_gb": 32,
        "idempotency_key": "retry-1",
    }
    first = await _call(broker, "apply", dict(args), ALICE)
    assert first["ok"] is True
    second = await _call(broker, "apply", dict(args), ALICE)
    assert second["ok"] is True
    assert second["data"]["id"] == first["data"]["id"]
    assert len(docker.created) == 1


async def test_ensure_user_does_not_create_membership(tmp_path, member_config):
    """AC-16: re-login (ensure_user) can never fabricate an active member."""
    broker, _ = _broker(tmp_path, member_config)
    response = await _call(broker, "ping", {}, ALICE)
    assert response["ok"] is True  # identity recording still works
    with broker._connection() as connection:
        member = member_store.get_member_by_uid(connection, ALICE.uid)
    assert member is None


def test_public_key_parsing_rejects_private_keys():
    with pytest.raises(ValueError, match="私钥"):
        parse_public_key("-----BEGIN OPENSSH PRIVATE KEY-----")
    with pytest.raises(ValueError):
        parse_public_key('ssh-ed25519 AAAA nope command="rm" extra=1')
    normalized, fingerprint = parse_public_key("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test@lab")
    assert normalized.startswith("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI")
    assert fingerprint.startswith("SHA256:")


class _FakeProvisioning:
    def __init__(self, *, fail: bool = False, inspect: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.inspect = inspect or {}

    def execute(
        self,
        action: str,
        frozen_input: dict,
        *,
        operation_id: str | None = None,
    ) -> dict:
        from citespec_resource.membership.provisioning import (
            ProvisioningError,
            validate_request,
        )

        validate_request({"action": action, "frozen_input": frozen_input})
        self.calls.append((action, frozen_input))
        if action == "inspect_member":
            return {
                "status": "succeeded",
                "stages": ["inspected"],
                "username": frozen_input.get("username"),
                **self.inspect,
            }
        if self.fail:
            raise ProvisioningError("stage=set_keys: simulated host failure")
        return {"status": "succeeded", "stages": [action], "uid": ALICE.uid}


KEY_ONE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOneKeyForBrokerTests00000000000000000001 one"
KEY_TWO = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITwoKeyForBrokerTests00000000000000000002 two"


def _seed_active_member(broker) -> str:
    with broker._connection() as connection:
        member_id = _provision_alice(connection)
        member_store.add_key(
            connection,
            member_id,
            public_key=KEY_ONE,
            fingerprint=parse_public_key(KEY_ONE)[1],
        )
    return member_id


async def test_member_self_service_key_add_and_revoke(tmp_path, member_config):
    executor = _FakeProvisioning()
    broker, _ = _broker(tmp_path, member_config, provisioning=executor)
    _seed_active_member(broker)
    added = await _call(broker, "me_keys_add", {"public_key": KEY_TWO}, ALICE)
    assert added["ok"] is True
    assert executor.calls[-1][0] == "set_keys"
    assert len(executor.calls[-1][1]["public_keys"]) == 2
    key_id = added["data"]["key_id"]
    revoked = await _call(broker, "me_keys_revoke", {"key_id": key_id}, ALICE)
    assert revoked["ok"] is True
    assert executor.calls[-1][0] == "set_keys"
    assert len(executor.calls[-1][1]["public_keys"]) == 1


async def test_readding_revoked_key_to_suspended_member_reactivates_it(tmp_path, member_config):
    broker, _ = _broker(tmp_path, member_config, provisioning=_FakeProvisioning())
    with broker._connection() as connection:
        member_id = _provision_alice(connection, status="suspended")

    first = await _call(
        broker,
        "admin_members_keys_add",
        {"member_id": member_id, "public_key": KEY_ONE},
        ADMIN,
    )
    second = await _call(
        broker,
        "admin_members_keys_add",
        {"member_id": member_id, "public_key": KEY_TWO},
        ADMIN,
    )
    assert first["ok"] is True
    assert second["ok"] is True
    revoked = await _call(
        broker,
        "admin_members_keys_revoke",
        {"member_id": member_id, "key_id": first["data"]["key_id"]},
        ADMIN,
    )
    assert revoked["ok"] is True

    restored = await _call(
        broker,
        "admin_members_keys_add",
        {"member_id": member_id, "public_key": KEY_ONE},
        ADMIN,
    )

    assert restored["ok"] is True
    assert restored["data"]["key_id"] == first["data"]["key_id"]


async def test_admin_key_add_rolls_back_when_host_sync_fails(tmp_path, member_config):
    executor = _FakeProvisioning(fail=True)
    broker, _ = _broker(tmp_path, member_config, provisioning=executor)
    member_id = _seed_active_member(broker)
    response = await _call(
        broker,
        "admin_members_keys_add",
        {"member_id": member_id, "public_key": KEY_TWO},
        ADMIN,
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "conflict"
    with broker._connection() as connection:
        keys = member_store.get_member(connection, member_id)["keys"]
    assert len(keys) == 1


async def test_auto_gpu_count_selects_requested_number(tmp_path, member_config):
    broker, docker = _broker(tmp_path, member_config)
    with broker._connection() as connection:
        _provision_alice(connection)
    response = await _call(
        broker,
        "apply",
        {"gpu_count": 2, "cpus": 8, "memory_gb": 32, "idempotency_key": "count-2"},
        ALICE,
    )
    assert response["ok"] is True
    assert len(response["data"]["gpus"]) == 2
    assert len(docker.created[-1].gpus) == 2


async def test_admin_legacy_import_dry_run_dispatch(tmp_path, member_config):
    executor = _FakeProvisioning(
        inspect={
            "exists": True,
            "uid": 42424,
            "home": "/home/legacy",
            "home_exists": True,
            "home_is_symlink": False,
            "groups": ["citespec-members"],
            "managed": False,
        }
    )
    broker, _ = _broker(tmp_path, member_config, provisioning=executor)
    response = await _call(
        broker,
        "admin_members_import_legacy",
        {
            "username": "legacy",
            "display_name": "Legacy",
            "valid_until": "",
            "quota_gb": 50,
            "public_keys": [KEY_ONE],
            "dry_run": True,
        },
        ADMIN,
    )
    assert response["ok"] is True
    report = response["data"]
    assert report["dry_run"] is True
    assert report["conflicts"] == []
    assert report["inspection"]["uid"] == 42424
    assert executor.calls[0][0] == "inspect_member"


async def test_queue_idempotency_and_requested_count(tmp_path, member_config):
    broker, fake = _broker(tmp_path, member_config)
    with broker._connection() as connection:
        _provision_alice(connection)
    for index in range(8):
        fake.add_process(
            __import__(
                "citespec_resource.broker.docker_driver",
                fromlist=["GPUProcess"],
            ).GPUProcess(gpu_uuid=f"GPU-{index}", pid=5000 + index, used_mib=1024, process_name="x")
        )
    args = {
        "gpu_count": 2,
        "cpus": 8,
        "memory_gb": 32,
        "queue": True,
        "idempotency_key": "queue-count-2",
    }
    first = await _call(broker, "apply", dict(args), ALICE)
    assert first["ok"] is True
    assert first["data"]["status"] == "queued"
    second = await _call(broker, "apply", dict(args), ALICE)
    assert second["ok"] is True
    assert second["data"]["queue_id"] == first["data"]["queue_id"]
    with broker._connection() as connection:
        rows = store.list_queue_requests(connection, owner_uid=ALICE.uid, now=FIXED)
    assert len(rows) == 1
    assert rows[0]["requested_gpu_count"] == 2
    assert rows[0]["gpus"] == []
