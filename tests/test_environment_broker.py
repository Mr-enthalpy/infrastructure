"""Broker-level tests for environment memory commands."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from citespec_resource.broker.server import Broker
from citespec_resource.db import connect
from citespec_resource.gpu_audit import GpuAuditResult
from citespec_resource.identity import Identity
from citespec_resource.legacy_audit import LegacyAuditResult
from citespec_resource.membership import store as member_store
from citespec_resource.workspace_quota import QuotaCheckResult
from tests.fakes import FakeDocker, FakeIdentityResolver

FIXED = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ALICE = Identity(uid=1000, gid=1000, username="alice", role="member", is_admin=False)
BOB = Identity(uid=1001, gid=1001, username="bob", role="member", is_admin=False)
ADMIN = Identity(uid=0, gid=0, username="root", role="admin", is_admin=True)

PROBE = {
    "os": {"PRETTY_NAME": "Ubuntu 22.04.5 LTS"},
    "interpreters": [
        {
            "path": "/usr/local/bin/python3",
            "version": "3.11.9",
            "packages": [{"name": "torch", "version": "2.7.1"}],
        }
    ],
    "conda": [],
    "dpkg": [{"name": "libgl1", "version": "1.3.4-2.1"}],
}
IMAGE_ID = "sha256:" + "a" * 64


def _passing_member_quota(workspace, uid, limit):
    from citespec_resource.workspace_quota import MemberQuotaResult

    return MemberQuotaResult(
        True,
        "ok",
        project_id=uid,
        hard_limit_bytes=limit * 1024**3,
    )


def _broker(tmp_path, config, docker=None):
    fake = docker or FakeDocker()
    resolver = FakeIdentityResolver({ALICE.uid: ALICE, BOB.uid: BOB, ADMIN.uid: ADMIN})
    broker = Broker(
        config,
        db_path=tmp_path / "state.db",
        socket_path=tmp_path / "broker.sock",
        docker=fake,
        identity_resolver=resolver,
        legacy_audit=lambda: LegacyAuditResult(True, "clean"),
        gpu_audit=lambda: GpuAuditResult(True, "clean", fingerprint="fp"),
        quota_check=lambda: QuotaCheckResult(True, "filesystem supports project quotas"),
        member_quota_check=_passing_member_quota,
        member_quota_provision=_passing_member_quota,
        clock=lambda: FIXED,
    )
    broker.initialize()
    return broker, fake


async def _call(broker, command, args, identity):
    return await broker.handle_request(
        {"cmd": command, "args": args}, uid=identity.uid, gid=identity.gid
    )


def _provision(tmp_path, *, status="active"):
    connection = connect(tmp_path / "state.db")
    try:
        alice = member_store.create_member(
            connection, linux_uid=ALICE.uid, linux_username=ALICE.username, status=status
        )
        bob = member_store.create_member(
            connection, linux_uid=BOB.uid, linux_username=BOB.username, status="active"
        )
        return alice, bob
    finally:
        connection.close()


@pytest.fixture
def env_config(active_config):
    return replace(
        active_config,
        policy=replace(active_config.policy, environment_enabled=True),
    )


async def _running_allocation(broker):
    applied = await _call(broker, "apply", {"gpus": [0], "cpus": 8, "memory_gb": 32}, ALICE)
    assert applied["ok"] is True, applied
    return applied["data"]["id"]


async def test_capture_list_show_and_diff(tmp_path, env_config):
    broker, docker = _broker(tmp_path, env_config)
    _provision(tmp_path)
    allocation_id = await _running_allocation(broker)
    docker.images[env_config.image.base] = {"id": IMAGE_ID, "digests": [IMAGE_ID]}
    docker.exec_output = (0, json.dumps(PROBE), "")

    created = await _call(broker, "project_create", {"name": "reconstruction"}, ALICE)
    assert created["ok"] is True, created
    assert created["data"]["project"]["versions"] == 0

    captured = await _call(
        broker,
        "env_capture",
        {"project": "reconstruction", "allocation": allocation_id},
        ALICE,
    )
    assert captured["ok"] is True, captured
    version = captured["data"]["version"]
    assert version["ref"] == "reconstruction@v1"
    assert version["capture_fidelity"] == "complete"

    listed = await _call(broker, "env_list", {"project": "reconstruction"}, ALICE)
    assert [item["ref"] for item in listed["data"]["versions"]] == ["reconstruction@v1"]

    shown = await _call(broker, "env_show", {"ref": "reconstruction@v1"}, ALICE)
    assert shown["ok"] is True, shown
    manifest = shown["data"]["manifest"]
    assert manifest["python"]["packages"][0] == {"name": "torch", "version": "2.7.1"}
    assert manifest["system"]["packages"][0]["name"] == "libgl1"
    assert manifest["base"]["digest"] == IMAGE_ID

    # A second capture with one more package produces a real diff.
    second_probe = json.loads(json.dumps(PROBE))
    second_probe["interpreters"][0]["packages"].append({"name": "numpy", "version": "1.26.4"})
    docker.exec_output = (0, json.dumps(second_probe), "")
    second = await _call(
        broker,
        "env_capture",
        {"project": "reconstruction", "allocation": allocation_id},
        ALICE,
    )
    assert second["ok"] is True, second
    assert second["data"]["version"]["ref"] == "reconstruction@v2"

    diff = await _call(
        broker,
        "env_diff",
        {"left": "reconstruction@v1", "right": "reconstruction@v2"},
        ALICE,
    )
    assert diff["ok"] is True, diff
    added = diff["data"]["diff"]["python"]["packages"]["added"]
    assert added == [{"name": "numpy", "version": "1.26.4"}]


async def test_capture_requires_membership_and_ownership(tmp_path, env_config):
    broker, docker = _broker(tmp_path, env_config)
    _provision(tmp_path)
    allocation_id = await _running_allocation(broker)
    docker.images[env_config.image.base] = {"id": IMAGE_ID, "digests": [IMAGE_ID]}
    docker.exec_output = (0, json.dumps(PROBE), "")
    await _call(broker, "project_create", {"name": "alice-proj"}, ALICE)
    await _call(broker, "project_create", {"name": "bob-proj"}, BOB)

    denied = await _call(
        broker,
        "env_capture",
        {"project": "bob-proj", "allocation": allocation_id},
        BOB,
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "forbidden"

    with connect(tmp_path / "state.db") as connection:
        member = member_store.get_member_by_uid(connection, ALICE.uid)
        assert member is not None
        connection.execute(
            "UPDATE members SET status = 'suspended' WHERE member_id = ?",
            (member.member_id,),
        )
        connection.commit()
    suspended = await _call(
        broker,
        "env_capture",
        {"project": "alice-proj", "allocation": allocation_id},
        ALICE,
    )
    assert suspended["ok"] is False
    assert suspended["error"]["code"] == "forbidden"


async def test_environment_gate_is_off_by_default(tmp_path, active_config):
    broker, _docker = _broker(tmp_path, active_config)
    _provision(tmp_path)
    response = await _call(broker, "project_create", {"name": "proj"}, ALICE)
    assert response["ok"] is False
    assert response["error"]["code"] == "forbidden"
    assert "environment_enabled" in response["error"]["message"]


async def test_project_visibility_is_scoped_to_the_owner(tmp_path, env_config):
    broker, docker = _broker(tmp_path, env_config)
    _provision(tmp_path)
    allocation_id = await _running_allocation(broker)
    docker.images[env_config.image.base] = {"id": IMAGE_ID, "digests": [IMAGE_ID]}
    docker.exec_output = (0, json.dumps(PROBE), "")
    await _call(broker, "project_create", {"name": "alice-proj"}, ALICE)
    await _call(
        broker,
        "env_capture",
        {"project": "alice-proj", "allocation": allocation_id},
        ALICE,
    )

    hidden = await _call(broker, "env_show", {"ref": "alice-proj@v1"}, BOB)
    assert hidden["ok"] is False
    assert hidden["error"]["code"] == "not_found"

    admin_view = await _call(broker, "env_show", {"ref": "alice-proj@v1"}, ADMIN)
    assert admin_view["ok"] is True, admin_view
