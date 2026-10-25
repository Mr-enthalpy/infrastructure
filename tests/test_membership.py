"""Membership authorization and lifecycle tests (goal §6, AC-16/17/21)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.test_broker import ADMIN, ALICE, FIXED, _broker, _call
from udc_resource import store
from udc_resource.membership import store as member_store
from udc_resource.membership.models import parse_public_key


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
            "udc_resource.broker.docker_driver",
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
