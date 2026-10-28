"""Environment memory: schema 7, immutable versions, claims, and access rules."""

from __future__ import annotations

import os
import sqlite3

import pytest

from citespec_resource import config as config_module
from citespec_resource import store
from citespec_resource.db import init_db
from citespec_resource.environment import authorization as env_authz
from citespec_resource.environment import builder as env_builder
from citespec_resource.environment import capture as env_capture
from citespec_resource.environment import models
from citespec_resource.environment import store as env_store
from citespec_resource.environment.normalize import digest_text, normalize_manifest
from citespec_resource.membership import store as member_store


@pytest.fixture
def connection(tmp_path):
    connection = init_db(tmp_path / "state.db")
    yield connection
    connection.close()


def _member(connection, uid: int, username: str, *, status: str = "active") -> str:
    return member_store.create_member(
        connection,
        linux_uid=uid,
        linux_username=username,
        status=status,
    )


def _manifest(**overrides):
    payload = {
        "schema": "citespec.environment.v1",
        "name": "reconstruction-pytorch",
        "platform": "linux/amd64",
        "base": {"image": "citespec-base:2026.10", "digest": ""},
        "system": {
            "package_manager": "apt",
            "packages": [
                {"name": "libgl1", "version": "1.3.4-2.1"},
                {"name": "ffmpeg", "version": ""},
            ],
        },
        "python": {
            "version": "3.11.9",
            "manager": "pip",
            "packages": [{"name": "torch", "version": "2.7.1"}],
            "lockfiles": [],
        },
        "gpu": {"framework": "pytorch", "cuda_userspace": "12.8"},
        "origin": {"type": "capture", "source_id": ""},
        "notes": ["训练脚本可复现"],
    }
    payload.update(overrides)
    return payload


def _version(connection, *, member_id: str, profile_id: str, payload=None, **kwargs):
    normalized = normalize_manifest(payload or _manifest())
    return env_store.create_version(
        connection,
        profile_id=profile_id,
        manifest_text=normalized.text,
        sha256=normalized.sha256,
        origin=str((payload or _manifest())["origin"]["type"]),
        capture_fidelity="complete",
        created_by_member_id=member_id,
        **kwargs,
    )


def test_project_creation_adds_default_profile(connection):
    member_id = _member(connection, 1001, "alice")
    project = env_store.create_project(connection, owner_member_id=member_id, name="reconstruction")
    assert project["status"] == "active"
    profile = env_store.default_profile(connection, project["project_id"])
    assert profile["name"] == "default"
    assert (
        env_store.get_project_by_name(connection, owner_member_id=member_id, name="reconstruction")[
            "project_id"
        ]
        == project["project_id"]
    )


def test_project_names_are_restricted(connection):
    member_id = _member(connection, 1001, "alice")
    for bad in ("", " has space", "../escape", "项目"):
        with pytest.raises(models.EnvironmentError):
            env_store.create_project(connection, owner_member_id=member_id, name=bad)


def test_versions_are_numbered_and_content_is_immutable(connection):
    member_id = _member(connection, 1001, "alice")
    project = env_store.create_project(connection, owner_member_id=member_id, name="proj")
    profile = env_store.default_profile(connection, project["project_id"])

    first = _version(connection, member_id=member_id, profile_id=profile["profile_id"])
    second = _version(connection, member_id=member_id, profile_id=profile["profile_id"])
    assert (first["version"], second["version"]) == (1, 2)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE environment_versions SET manifest = '{}' WHERE version_id = ?",
            (first["version_id"],),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE environment_versions SET sha256 = ? WHERE version_id = ?",
            ("0" * 64, first["version_id"]),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "DELETE FROM environment_versions WHERE version_id = ?", (first["version_id"],)
        )
    connection.rollback()


def test_version_digest_must_match_manifest(connection):
    member_id = _member(connection, 1001, "alice")
    project = env_store.create_project(connection, owner_member_id=member_id, name="proj")
    profile = env_store.default_profile(connection, project["project_id"])
    normalized = normalize_manifest(_manifest())
    with pytest.raises(models.EnvironmentError):
        env_store.create_version(
            connection,
            profile_id=profile["profile_id"],
            manifest_text=normalized.text,
            sha256="0" * 64,
            origin="capture",
            capture_fidelity="complete",
            created_by_member_id=member_id,
        )


def test_verification_transitions_move_forward_only(connection):
    member_id = _member(connection, 1001, "alice")
    project = env_store.create_project(connection, owner_member_id=member_id, name="proj")
    profile = env_store.default_profile(connection, project["project_id"])
    version = _version(connection, member_id=member_id, profile_id=profile["profile_id"])
    version_id = version["version_id"]

    with pytest.raises(models.EnvironmentConflict):
        env_store.set_verification(connection, version_id, "functional_passed")

    assert env_store.set_verification(connection, version_id, "built")["verification"] == "built"
    passed = env_store.set_verification(connection, version_id, "functional_passed")
    assert passed["verification"] == "functional_passed"
    with pytest.raises(models.EnvironmentConflict):
        env_store.set_verification(connection, version_id, "built")


def test_artifacts_are_bounded_and_immutable(connection):
    member_id = _member(connection, 1001, "alice")
    project = env_store.create_project(connection, owner_member_id=member_id, name="proj")
    profile = env_store.default_profile(connection, project["project_id"])
    version = _version(connection, member_id=member_id, profile_id=profile["profile_id"])

    artifact = env_store.add_artifact(
        connection,
        version_id=version["version_id"],
        kind="requirements",
        name="requirements.txt",
        content="torch==2.7.1\n",
        max_bytes=1024,
    )
    assert artifact["sha256"] == digest_text("torch==2.7.1\n")
    assert env_store.artifacts_total_bytes(connection, version["version_id"]) == len(
        "torch==2.7.1\n"
    )
    with pytest.raises(models.EnvironmentError):
        env_store.add_artifact(
            connection,
            version_id=version["version_id"],
            kind="requirements",
            name="big.txt",
            content="x" * 2048,
            max_bytes=1024,
        )
    with pytest.raises(models.EnvironmentError):
        env_store.add_artifact(
            connection,
            version_id=version["version_id"],
            kind="requirements",
            name="../escape",
            content="x",
            max_bytes=1024,
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE environment_artifacts SET content = 'x' WHERE artifact_id = ?",
            (artifact["artifact_id"],),
        )
    connection.rollback()


def test_manifest_normalization_is_stable_and_strict():
    first = normalize_manifest(_manifest())
    reordered = dict(reversed(list(_manifest().items())))
    second = normalize_manifest(reordered)
    assert first.text == second.text
    assert first.sha256 == second.sha256
    assert first.data["system"]["packages"] == [
        {"name": "libgl1", "version": "1.3.4-2.1"},
        {"name": "ffmpeg", "version": ""},
    ]

    with pytest.raises(models.ManifestError):
        normalize_manifest(_manifest(extra_field=True))
    with pytest.raises(models.ManifestError):
        normalize_manifest(
            _manifest(python={"packages": [{"name": "torch", "version": "2.7 ; rm -rf /"}]})
        )
    with pytest.raises(models.ManifestError):
        normalize_manifest(_manifest(platform="windows/amd64"))
    with pytest.raises(models.ManifestError):
        normalize_manifest(_manifest(base={"image": "a", "digest": "sha256:xyz"}))
    with pytest.raises(models.ManifestError):
        normalize_manifest(_manifest(python={"packages": [{"name": "torch"}] * 3}), max_packages=2)


def test_private_versions_are_hidden_from_other_members(connection):
    owner = _member(connection, 1001, "alice")
    other = _member(connection, 1002, "bob")
    project = env_store.create_project(connection, owner_member_id=owner, name="proj")
    profile = env_store.default_profile(connection, project["project_id"])
    version = _version(connection, member_id=owner, profile_id=profile["profile_id"])

    context = env_authz.require_version(
        connection, uid=1001, is_admin=False, version_id=version["version_id"]
    )
    assert context["owner_member_id"] == owner

    with pytest.raises(models.EnvironmentForbidden):
        env_authz.require_version(
            connection, uid=1002, is_admin=False, version_id=version["version_id"]
        )
    # Admins keep the trusted path.
    assert env_authz.require_version(
        connection, uid=9999, is_admin=True, version_id=version["version_id"]
    )

    env_store.set_visibility(connection, version["version_id"], models.VISIBILITY_ADMIN_SHARED)
    shared = env_authz.require_version(
        connection, uid=1002, is_admin=False, version_id=version["version_id"]
    )
    assert shared["version_id"] == version["version_id"]
    assert other


def test_suspended_member_loses_access_immediately(connection):
    owner = _member(connection, 1001, "alice")
    project = env_store.create_project(connection, owner_member_id=owner, name="proj")
    connection.execute("UPDATE members SET status = 'suspended' WHERE member_id = ?", (owner,))
    connection.commit()
    with pytest.raises(models.EnvironmentForbidden):
        env_authz.require_project(connection, uid=1001, is_admin=False, name="proj")
    with pytest.raises(models.EnvironmentForbidden):
        env_authz.require_active_member(connection, uid=1001)
    assert project["owner_member_id"] == owner


def test_claims_reject_duplicates_and_require_explicit_conflict_resolution(connection):
    alice = _member(connection, 1001, "alice")
    bob = _member(connection, 1002, "bob")
    alice_project = env_store.create_project(connection, owner_member_id=alice, name="alice-proj")
    bob_project = env_store.create_project(connection, owner_member_id=bob, name="bob-proj")
    source = env_store.record_legacy_source(
        connection,
        server_id="server-a",
        container_id="c" * 64,
        image_ref="citespec-base:2026.10",
        image_id="sha256:" + "1" * 64,
        container_name="legacy-1",
        state_at_scan="exited",
        sanitized_metadata={"cuda": "12.1"},
    )

    first = env_store.create_claim(
        connection,
        source_id=source["source_id"],
        member_id=alice,
        project_id=alice_project["project_id"],
        reason="我的旧训练环境",
    )
    with pytest.raises(models.EnvironmentConflict):
        env_store.create_claim(
            connection,
            source_id=source["source_id"],
            member_id=alice,
            project_id=alice_project["project_id"],
        )
    second = env_store.create_claim(
        connection,
        source_id=source["source_id"],
        member_id=bob,
        project_id=bob_project["project_id"],
        reason="也有我的实验",
    )
    with pytest.raises(models.EnvironmentConflict):
        env_store.decide_claim(connection, first["claim_id"], approve=True, reviewer="admin")

    env_store.decide_claim(
        connection,
        second["claim_id"],
        approve=False,
        reviewer="admin",
        note="重复认领，请联系管理员",
    )
    approved = env_store.decide_claim(connection, first["claim_id"], approve=True, reviewer="admin")
    assert approved["status"] == "approved"
    assert env_store.get_legacy_source(connection, source["source_id"])["status"] == "claimed"
    with pytest.raises(models.EnvironmentConflict):
        env_store.decide_claim(connection, first["claim_id"], approve=True, reviewer="admin")


def test_build_records_only_one_active_build_per_version(connection):
    member_id = _member(connection, 1001, "alice")
    project = env_store.create_project(connection, owner_member_id=member_id, name="proj")
    profile = env_store.default_profile(connection, project["project_id"])
    version = _version(connection, member_id=member_id, profile_id=profile["profile_id"])

    build = env_store.create_build(
        connection,
        version_id=version["version_id"],
        server_id="server-a",
        base_image="citespec-base:2026.10",
        image_tag="citespec-env:test",
    )
    with pytest.raises(sqlite3.IntegrityError):
        env_store.create_build(
            connection,
            version_id=version["version_id"],
            server_id="server-a",
            base_image="citespec-base:2026.10",
            image_tag="citespec-env:other",
        )
    connection.rollback()

    env_store.mark_build_running(connection, build["build_id"])
    finished = env_store.finish_build(
        connection,
        build["build_id"],
        status="built",
        image_id="sha256:" + "2" * 64,
        evidence={"pip_check": "ok"},
    )
    assert finished["status"] == "built"
    with pytest.raises(models.EnvironmentConflict):
        env_store.finish_build(connection, build["build_id"], status="failed")


def test_environment_gates_default_to_disabled(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[server]
name = "test"
ssh_alias = "test"

[hardware]
gpu_count = 4
gpu_model = "NVIDIA GeForce RTX 4090"
gpu_memory_mib = 24564
cpu_logical = 64
ram_gib = 256

[policy]
mode = "monitor"
""",
        encoding="utf-8",
    )
    cfg = config_module.load_config(path)
    assert cfg.policy.environment_enabled is False
    assert cfg.policy.environment_builds_enabled is False
    assert cfg.policy.legacy_claims_enabled is False

    path.write_text(
        path.read_text(encoding="utf-8") + "environment_builds_enabled = true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="environment_builds_enabled"):
        config_module.load_config(path)


def test_queue_and_admission_carry_the_environment_snapshot(connection):
    member_id = _member(connection, 1001, "alice")
    project = env_store.create_project(connection, owner_member_id=member_id, name="proj")
    profile = env_store.default_profile(connection, project["project_id"])
    version = _version(connection, member_id=member_id, profile_id=profile["profile_id"])
    build = env_store.create_build(
        connection,
        version_id=version["version_id"],
        server_id="server-a",
        base_image="citespec-base:2026.10",
        image_tag="citespec-env:test",
    )
    image_id = "sha256:" + "3" * 64
    queue_id = store.create_queue_request(
        connection,
        owner_uid=1001,
        owner_name="alice",
        requested_gpus=[],
        cpus=1,
        memory_gb=1,
        hours=1,
        image=build["image_tag"],
        workspace="/tmp/workspace",
        note="",
        max_queue_length=10,
        max_queued_allocations_per_user=10,
        max_queue_hours=24,
        env_version_id=version["version_id"],
        env_build_id=build["build_id"],
        image_id=image_id,
    )
    queued = store.get_queue_request(connection, queue_id)
    assert queued["env_version_id"] == version["version_id"]
    assert queued["env_build_id"] == build["build_id"]
    assert queued["image_id"] == image_id

    allocation_id = store.admit_next_queued(
        connection,
        max_cpus=8,
        max_memory_gb=64,
        max_cpus_per_user=8,
        max_memory_gb_per_user=64,
        max_gpus_per_user=4,
        max_active_allocations_per_user=4,
        now=None,
    )
    assert allocation_id is not None
    allocation = store.get_allocation(connection, allocation_id)
    assert allocation["env_version_id"] == version["version_id"]
    assert allocation["env_build_id"] == build["build_id"]
    assert allocation["image_id"] == image_id
    assert allocation["image"] == build["image_tag"]


def test_build_inputs_are_deterministic():
    requirements = env_builder.requirements_from_manifest(
        {
            "python": {
                "packages": [
                    {"name": "torch", "version": "2.7.1"},
                    {"name": "numpy", "version": ""},
                ]
            }
        }
    )
    assert requirements == "torch==2.7.1\nnumpy\n"
    tag = env_builder.image_tag_for("ver_abc123", "bld_0123456789abcdef")
    assert tag.startswith("citespec-env:")
    assert env_builder.image_tag_for("ver_abc123", "bld_0123456789abcdef") == tag
    assert env_builder.image_tag_for("ver_abc123", "bld_fedcba9876543210") != tag


def test_pytorch_local_versions_use_the_matching_index():
    """``2.7.1+cu128`` only exists on the PyTorch wheel index, not on PyPI."""

    requirements = env_builder.requirements_from_manifest(
        {
            "python": {
                "packages": [
                    {"name": "torch", "version": "2.7.1+cu128"},
                    {"name": "torchvision", "version": "0.22.1+cu128"},
                    {"name": "numpy", "version": "2.3.2+local"},
                ]
            }
        }
    )

    assert requirements.splitlines() == [
        "--extra-index-url https://download.pytorch.org/whl/cu128",
        "torch==2.7.1+cu128",
        "torchvision==0.22.1+cu128",
        "numpy==2.3.2",
    ]


def test_probe_records_installers_and_skips_conda_distributions():
    """Conda-installed dists must not become pip requirements.

    ``conda``, ``menuinst`` and similar packages only exist on conda channels,
    so a pip install of the recorded name fails.  The probe records INSTALLER
    and leaves those to the pinned base image.
    """

    assert '"installer": installer(path)' in env_capture.PROBE_SCRIPT
    assert 'item.get("installer") == "conda"' in env_capture.PROBE_SCRIPT


def test_builder_venv_inherits_the_base_image_python_environment():
    assert "--system-site-packages" in env_builder.BUILDER_SCRIPT


def test_probe_records_the_per_user_site_packages():
    """``pip install --user`` is common in read-only containers.

    The interpreter imports the user site directory before its own
    site-packages, so a capture that only reads ``site.getsitepackages()``
    records versions the member does not actually use.
    """

    assert "site.getusersitepackages()" in env_capture.PROBE_SCRIPT


def test_conda_metadata_is_not_turned_into_pip_requirements():
    """Conda-only packages such as ``_libgcc_mutex`` cannot be pip-installed.

    They are recorded for fidelity, but the build input must only contain the
    distributions that were actually present in the interpreter's
    site-packages.
    """

    result = env_capture.build_manifest(
        project_name="conda-proj",
        probe={
            "interpreters": [
                {
                    "path": "/opt/conda/bin/python",
                    "version": "3.11.13",
                    "packages": [{"name": "torch", "version": "2.7.1"}],
                }
            ],
            "conda": [
                {"name": "_libgcc_mutex", "version": "0.1"},
                {"name": "torch", "version": "2.7.1"},
            ],
            "dpkg": [],
            "errors": [],
        },
        base_image="citespec-base:2026.10",
        base_digest="sha256:" + "a" * 64,
        lockfiles=[],
    )

    python = result.manifest["python"]
    assert python["packages"] == [{"name": "torch", "version": "2.7.1"}]
    assert python["conda_packages"] == [
        {"name": "_libgcc_mutex", "version": "0.1"},
        {"name": "torch", "version": "2.7.1"},
    ]
    normalized = normalize_manifest(result.manifest)
    assert normalized.data["python"]["conda_packages"][0]["name"] == "_libgcc_mutex"
    requirements = env_builder.requirements_from_manifest(normalized.data)
    assert requirements == "torch==2.7.1\n"
    assert "_libgcc_mutex" not in requirements


@pytest.mark.skipif(os.name != "posix", reason="chown is POSIX-only")
def test_chown_is_skipped_when_the_broker_is_not_root(monkeypatch, tmp_path):
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(env_builder.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(env_builder.os, "chown", lambda *args: calls.append(args))

    env_builder._chown(tmp_path, 1000, 1000)

    assert calls == []
