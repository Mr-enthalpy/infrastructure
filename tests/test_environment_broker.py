"""Broker-level tests for environment memory commands."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from citespec_resource.broker.docker_driver import ContainerInfo
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


@pytest.fixture
def build_config(active_config, tmp_path):
    return replace(
        active_config,
        policy=replace(
            active_config.policy,
            environment_enabled=True,
            environment_builds_enabled=True,
            env_state_dir=str(tmp_path / "envs"),
            env_build_keep_per_version=1,
        ),
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


# -- image builds ----------------------------------------------------------


PROBE_NO_APT = {**PROBE, "dpkg": []}
BASE_IMAGE_ID = "sha256:" + "b" * 64


async def _captured_version(broker, docker, config, *, probe):
    allocation_id = await _running_allocation(broker)
    docker.images[config.image.base] = {"id": BASE_IMAGE_ID, "digests": [], "size": 2048}
    docker.exec_output = (0, json.dumps(probe), "")
    await _call(broker, "project_create", {"name": "reconstruction"}, ALICE)
    captured = await _call(
        broker,
        "env_capture",
        {"project": "reconstruction", "allocation": allocation_id},
        ALICE,
    )
    assert captured["ok"] is True, captured
    return captured["data"]["version"], allocation_id


async def test_build_then_start_and_verify(tmp_path, build_config):
    broker, docker = _broker(tmp_path, build_config)
    _provision(tmp_path)
    version, _allocation = await _captured_version(broker, docker, build_config, probe=PROBE_NO_APT)

    built = await _call(broker, "env_build", {"ref": "reconstruction@v1"}, ALICE)
    assert built["ok"] is True, built
    build = built["data"]["build"]
    assert build["status"] == "built"
    assert build["image_tag"].startswith("citespec-env:")
    assert built["data"]["version"]["verification"] == "built"

    call = docker.builder_calls[-1]
    assert call["image"] == BASE_IMAGE_ID
    assert call["user"] == f"{ALICE.uid}:{ALICE.gid}"
    assert call["cpus"] == build_config.policy.env_build_cpus
    binds = {spec["bind"] for spec in call["volumes"].values()}
    assert binds == {"/opt/citespec-env", "/citespec/requirements.txt"}
    assert docker.build_image_calls[-1]["tag"] == build["image_tag"]

    shown = await _call(broker, "env_show", {"ref": "reconstruction@v1"}, ALICE)
    assert shown["data"]["builds"][0]["status"] == "built"

    started = await _call(
        broker,
        "apply",
        {
            "gpus": [1],
            "cpus": 8,
            "memory_gb": 32,
            "env": "reconstruction@v1",
        },
        ALICE,
    )
    assert started["ok"] is True, started
    allocation = started["data"]
    assert allocation["image"] == build["image_tag"]
    assert allocation["env_version_id"] == version["version_id"]
    assert allocation["image_id"] == build["image_id"]

    verified = await _call(
        broker,
        "env_verify",
        {"ref": "reconstruction@v1", "allocation": allocation["id"]},
        ALICE,
    )
    assert verified["ok"] is True, verified
    assert verified["data"]["version"]["verification"] == "functional_passed"


async def test_build_warns_about_system_packages(tmp_path, build_config):
    """V1 cannot run apt; the build proceeds and reports the recorded packages."""

    broker, docker = _broker(tmp_path, build_config)
    _provision(tmp_path)
    await _captured_version(broker, docker, build_config, probe=PROBE)

    built = await _call(broker, "env_build", {"ref": "reconstruction@v1"}, ALICE)
    assert built["ok"] is True, built
    build = built["data"]["build"]
    assert build["status"] == "built"
    assert "1 个系统包" in build["detail"]
    assert "apt" not in build["detail"]

    shown = await _call(broker, "env_show", {"ref": "reconstruction@v1"}, ALICE)
    assert shown["data"]["version"]["verification"] == "built"


async def test_apply_with_env_requires_a_successful_build(tmp_path, build_config):
    broker, docker = _broker(tmp_path, build_config)
    _provision(tmp_path)
    await _captured_version(broker, docker, build_config, probe=PROBE_NO_APT)

    denied = await _call(
        broker,
        "apply",
        {"gpus": [1], "cpus": 8, "memory_gb": 32, "env": "reconstruction@v1"},
        ALICE,
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "conflict"
    assert "env build" in denied["error"]["message"]

    both = await _call(
        broker,
        "apply",
        {
            "gpus": [1],
            "cpus": 8,
            "memory_gb": 32,
            "env": "reconstruction@v1",
            "image": build_config.image.base,
        },
        ALICE,
    )
    assert both["ok"] is False
    assert both["error"]["code"] == "bad_request"


async def test_apply_fails_closed_when_built_image_disappears(tmp_path, build_config):
    broker, docker = _broker(tmp_path, build_config)
    _provision(tmp_path)
    await _captured_version(broker, docker, build_config, probe=PROBE_NO_APT)
    built = await _call(broker, "env_build", {"ref": "reconstruction@v1"}, ALICE)
    tag = built["data"]["build"]["image_tag"]
    docker.images.pop(tag)

    denied = await _call(
        broker,
        "apply",
        {"gpus": [1], "cpus": 8, "memory_gb": 32, "env": "reconstruction@v1"},
        ALICE,
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "conflict"
    assert "env build" in denied["error"]["message"]


async def test_build_keeps_only_the_configured_number_of_images(tmp_path, build_config):
    broker, docker = _broker(tmp_path, build_config)
    _provision(tmp_path)
    await _captured_version(broker, docker, build_config, probe=PROBE_NO_APT)

    first = await _call(broker, "env_build", {"ref": "reconstruction@v1"}, ALICE)
    assert first["ok"] is True, first
    second = await _call(broker, "env_build", {"ref": "reconstruction@v1"}, ALICE)
    assert second["ok"] is True, second
    assert first["data"]["build"]["image_tag"] in docker.removed_images
    assert second["data"]["build"]["image_tag"] in docker.images


async def test_build_requires_the_builds_gate(tmp_path, env_config):
    broker, docker = _broker(tmp_path, env_config)
    _provision(tmp_path)
    await _captured_version(broker, docker, env_config, probe=PROBE_NO_APT)

    denied = await _call(broker, "env_build", {"ref": "reconstruction@v1"}, ALICE)
    assert denied["ok"] is False
    assert denied["error"]["code"] == "forbidden"
    assert "environment_builds_enabled" in denied["error"]["message"]


# -- legacy containers ------------------------------------------------------


@pytest.fixture
def legacy_config(active_config):
    return replace(
        active_config,
        policy=replace(
            active_config.policy,
            environment_enabled=True,
            legacy_claims_enabled=True,
        ),
    )


def _legacy_container(**kwargs):
    payload = {
        "id": "c" * 64,
        "name": "old-workspace",
        "image": "citespec-base:2024.09",
        "status": "running",
        "labels": {},
        "created": "2025-03-01T10:00:00Z",
        "image_id": "sha256:" + "d" * 64,
    }
    payload.update(kwargs)
    return ContainerInfo(**payload)


async def _legacy_source_id(broker, docker):
    scanned = await _call(broker, "admin_legacy_scan", {}, ADMIN)
    assert scanned["ok"] is True, scanned
    listed = await _call(broker, "admin_legacy_sources", {}, ADMIN)
    assert listed["ok"] is True, listed
    return listed["data"]["sources"][0]["source_id"]


async def test_legacy_scan_skips_managed_and_dismisses_missing(tmp_path, legacy_config):
    broker, docker = _broker(tmp_path, legacy_config)
    _provision(tmp_path)
    docker.add_container(_legacy_container())
    docker.add_container(
        _legacy_container(
            id="e" * 64,
            name="citespec-managed",
            labels={"citespec.managed": "true"},
        )
    )

    scanned = await _call(broker, "admin_legacy_scan", {}, ADMIN)
    assert scanned["ok"] is True, scanned
    assert scanned["data"]["scanned"] == 1

    admin_view = await _call(broker, "admin_legacy_sources", {}, ADMIN)
    source = admin_view["data"]["sources"][0]
    assert source["container_name"] == "old-workspace"
    assert source["status"] == "available"

    member_view = await _call(broker, "legacy_sources", {}, ALICE)
    assert member_view["ok"] is True, member_view
    visible = member_view["data"]["sources"][0]
    assert "container_name" not in visible
    assert "container_id" not in visible
    assert "labels" not in visible["metadata"]

    docker.containers.clear()
    rescanned = await _call(broker, "admin_legacy_scan", {}, ADMIN)
    assert rescanned["data"] == {"scanned": 0, "dismissed": 1}
    after = await _call(broker, "admin_legacy_sources", {}, ADMIN)
    assert after["data"]["sources"][0]["status"] == "dismissed"


async def test_legacy_commands_require_the_gate(tmp_path, env_config):
    broker, docker = _broker(tmp_path, env_config)
    _provision(tmp_path)
    denied = await _call(broker, "legacy_sources", {}, ALICE)
    assert denied["ok"] is False
    assert denied["error"]["code"] == "forbidden"
    assert "legacy_claims_enabled" in denied["error"]["message"]


async def test_legacy_claim_needs_approval_and_conflicts_are_not_auto_resolved(
    tmp_path, legacy_config
):
    broker, docker = _broker(tmp_path, legacy_config)
    _provision(tmp_path)
    docker.add_container(_legacy_container())
    source_id = await _legacy_source_id(broker, docker)

    await _call(broker, "project_create", {"name": "alice-proj"}, ALICE)
    await _call(broker, "project_create", {"name": "bob-proj"}, BOB)

    alice_claim = await _call(
        broker,
        "legacy_claim",
        {"source": source_id, "project": "alice-proj", "reason": "my old runs"},
        ALICE,
    )
    assert alice_claim["ok"] is True, alice_claim
    assert alice_claim["data"]["claim"]["status"] == "pending"
    bob_claim = await _call(
        broker,
        "legacy_claim",
        {"source": source_id, "project": "bob-proj", "reason": "mine"},
        BOB,
    )
    assert bob_claim["ok"] is True, bob_claim
    bob_claim_id = bob_claim["data"]["claim"]["claim_id"]
    alice_claim_id = alice_claim["data"]["claim"]["claim_id"]

    contested = await _call(
        broker, "admin_legacy_decide", {"id": alice_claim_id, "approve": True}, ADMIN
    )
    assert contested["ok"] is False
    assert contested["error"]["code"] == "conflict"

    rejected = await _call(
        broker,
        "admin_legacy_decide",
        {"id": bob_claim_id, "approve": False, "note": "belongs to alice"},
        ADMIN,
    )
    assert rejected["ok"] is True, rejected
    approved = await _call(
        broker, "admin_legacy_decide", {"id": alice_claim_id, "approve": True}, ADMIN
    )
    assert approved["ok"] is True, approved
    assert approved["data"]["claim"]["status"] == "approved"

    bob_view = await _call(broker, "legacy_claim_show", {"id": alice_claim_id}, BOB)
    assert bob_view["ok"] is False
    assert bob_view["error"]["code"] == "forbidden"

    bob_again = await _call(
        broker,
        "legacy_claim",
        {"source": source_id, "project": "bob-proj"},
        BOB,
    )
    assert bob_again["ok"] is False
    assert bob_again["error"]["code"] == "conflict"


async def test_legacy_running_capture_uses_the_fixed_probe_as_nobody(tmp_path, legacy_config):
    broker, docker = _broker(tmp_path, legacy_config)
    _provision(tmp_path)
    docker.add_container(_legacy_container())
    docker.images["citespec-base:2024.09"] = {
        "id": "sha256:" + "f" * 64,
        "digests": ["sha256:" + "f" * 64],
        "size": 2048,
    }
    docker.exec_output = (0, json.dumps(PROBE_NO_APT), "")
    source_id = await _legacy_source_id(broker, docker)
    await _call(broker, "project_create", {"name": "alice-proj"}, ALICE)
    claim = await _call(
        broker,
        "legacy_claim",
        {"source": source_id, "project": "alice-proj"},
        ALICE,
    )
    claim_id = claim["data"]["claim"]["claim_id"]

    early = await _call(broker, "admin_legacy_capture", {"id": claim_id}, ADMIN)
    assert early["ok"] is False
    assert early["error"]["code"] == "conflict"

    await _call(broker, "admin_legacy_decide", {"id": claim_id, "approve": True}, ADMIN)
    captured = await _call(broker, "admin_legacy_capture", {"id": claim_id}, ADMIN)
    assert captured["ok"] is True, captured
    version = captured["data"]["version"]
    assert version["origin"] == "legacy_claim"
    assert version["capture_fidelity"] == "complete"
    assert version["ref"] == "alice-proj@v1"
    assert captured["data"]["claim"]["version_id"] == version["version_id"]

    container_id, argv, user = docker.exec_calls[-1]
    assert container_id == "c" * 64
    assert argv[:3] == ["python3", "-I", "-S"]
    assert user == "65534:65534"
    assert docker.archive_calls == []

    shown = await _call(broker, "env_show", {"ref": "alice-proj@v1"}, ALICE)
    manifest = shown["data"]["manifest"]
    assert manifest["origin"]["type"] == "legacy_claim"
    assert manifest["origin"]["source_id"] == source_id
    assert manifest["python"]["packages"] == [{"name": "torch", "version": "2.7.1"}]

    twice = await _call(broker, "admin_legacy_capture", {"id": claim_id}, ADMIN)
    assert twice["ok"] is False
    assert twice["error"]["code"] == "conflict"


async def test_legacy_stopped_capture_reads_archives_without_exec(tmp_path, legacy_config):
    broker, docker = _broker(tmp_path, legacy_config)
    _provision(tmp_path)
    container_id = "s" * 64
    docker.add_container(_legacy_container(id=container_id, name="stopped-old", status="exited"))
    docker.archives[container_id] = {
        "/etc/os-release": b'PRETTY_NAME="Ubuntu 22.04.5 LTS"\n',
        "/opt/conda/conda-meta/numpy-1.26.4.json": json.dumps(
            {"name": "numpy", "version": "1.26.4"}
        ).encode("utf-8"),
    }
    source_id = await _legacy_source_id(broker, docker)
    await _call(broker, "project_create", {"name": "alice-proj"}, ALICE)
    claim = await _call(
        broker,
        "legacy_claim",
        {"source": source_id, "project": "alice-proj"},
        ALICE,
    )
    claim_id = claim["data"]["claim"]["claim_id"]
    await _call(broker, "admin_legacy_decide", {"id": claim_id, "approve": True}, ADMIN)

    captured = await _call(broker, "admin_legacy_capture", {"id": claim_id}, ADMIN)
    assert captured["ok"] is True, captured
    version = captured["data"]["version"]
    assert version["capture_fidelity"] == "partial"
    assert docker.exec_calls == []
    assert docker.archive_calls[-1][0] == container_id
    notes = " ".join(version["notes"])
    assert "容器已停止" in notes

    shown = await _call(broker, "env_show", {"ref": "alice-proj@v1"}, ALICE)
    manifest = shown["data"]["manifest"]
    assert manifest["python"]["packages"] == []
    assert manifest["python"]["conda_packages"] == [{"name": "numpy", "version": "1.26.4"}]
    assert manifest["origin"]["type"] == "legacy_claim"
