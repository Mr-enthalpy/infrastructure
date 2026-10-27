from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from citespec_resource import backup, store
from citespec_resource.db import connect, init_db


def _write_config(
    config_path: Path,
    state_db: Path,
    intake_db: Path | None = None,
    spool_dir: Path | None = None,
    portal_db: Path | None = None,
    portal_enabled: bool = False,
) -> None:
    lines = [
        "[server]",
        'name = "test"',
        'ssh_alias = "test"',
        f'state_db = "{state_db.as_posix()}"',
    ]
    if intake_db is not None or spool_dir is not None:
        lines += ["", "[onboarding]"]
    if intake_db is not None:
        lines.append(f'intake_db = "{intake_db.as_posix()}"')
    if spool_dir is not None:
        lines.append(f'provisioning_spool = "{spool_dir.as_posix()}"')
    if portal_db is not None or portal_enabled:
        lines += ["", "[portal]"]
        if portal_db is not None:
            lines.append(f'db_path = "{portal_db.as_posix()}"')
        if portal_enabled:
            lines.append("enabled = true")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE items (value TEXT NOT NULL)")
        connection.execute("INSERT INTO items VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def _make_state_db(path: Path, value: str, *, mode: str = "active") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = init_db(path)
    try:
        connection.execute("CREATE TABLE items (value TEXT NOT NULL)")
        connection.execute("INSERT INTO items VALUES (?)", (value,))
        connection.commit()
        store.set_mode(connection, mode, actor_uid=0)
        connection.commit()
    finally:
        connection.close()


def _insert_uncheckpointed_wal(path: Path, value: str) -> None:
    """Leave a real WAL behind by ending the writer process without closing."""

    script = (
        "import os, sqlite3, sys\n"
        "connection = sqlite3.connect(sys.argv[1])\n"
        "connection.execute('PRAGMA journal_mode=WAL')\n"
        "connection.execute('PRAGMA wal_autocheckpoint=0')\n"
        "connection.execute('INSERT INTO items VALUES (?)', (sys.argv[2],))\n"
        "connection.commit()\n"
        "os._exit(0)\n"
    )
    subprocess.run(
        [sys.executable, "-c", script, str(path), value],
        check=True,
        timeout=60,
    )


def _make_spool(path: Path) -> None:
    for name in ("requests", "results", "processing"):
        (path / name).mkdir(parents=True, exist_ok=True)
    (path / "requests" / "op-1.json").write_text('{"operation_id": "op-1"}\n', encoding="utf-8")
    (path / "results" / "op-1.json").write_text('{"ok": true}\n', encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    config_dir = tmp_path / "etc" / "citespec-resource"
    config_dir.mkdir(parents=True)
    state_db = tmp_path / "var" / "state.db"
    intake_db = tmp_path / "var" / "intake.db"
    spool_dir = tmp_path / "var" / "provision"
    _make_state_db(state_db, "broker")
    _make_db(intake_db, "intake")
    _make_spool(spool_dir)
    (config_dir / "authorized-keys.d").mkdir()
    (config_dir / "authorized-keys.d" / "alice").write_text("ssh-ed25519 AAAA test\n")
    config_path = config_dir / "config.toml"
    _write_config(config_path, state_db, intake_db, spool_dir)
    return config_path, state_db, intake_db, spool_dir, tmp_path / "backups"


def _make_restore_target(
    tmp_path: Path,
    *,
    with_intake_and_spool: bool,
) -> tuple[Path, Path, Path | None, Path | None]:
    restore_dir = tmp_path / "restore"
    state_db = restore_dir / "state.db"
    _make_state_db(state_db, "old")
    intake_db: Path | None = None
    spool_dir: Path | None = None
    if with_intake_and_spool:
        intake_db = restore_dir / "intake.db"
        spool_dir = restore_dir / "provision"
        _make_db(intake_db, "old intake")
        _make_spool(spool_dir)
        (spool_dir / "requests" / "stale.json").write_text("stale\n", encoding="utf-8")
    config_path = restore_dir / "config.toml"
    _write_config(config_path, state_db, intake_db, spool_dir)
    return config_path, state_db, intake_db, spool_dir


def test_backup_contains_state_intake_config_spool_and_verified_manifest(tmp_path):
    config_path, _, _, _, backup_root = _fixture(tmp_path)

    result = backup.create_backup(config_path, backup_root, stamp="20260101-000000")

    assert result.is_dir()
    assert (result / "state.db").is_file()
    assert (result / "intake.db").is_file()
    assert (result / "etc" / "citespec-resource" / "authorized-keys.d" / "alice").is_file()
    assert (result / "provision" / "requests" / "op-1.json").is_file()
    assert (result / "provision" / "results" / "op-1.json").is_file()
    manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == backup.BACKUP_SCHEMA
    assert {entry["path"] for entry in manifest["files"]} >= {
        "state.db",
        "intake.db",
        "etc/citespec-resource/config.toml",
        "provision/requests/op-1.json",
    }
    assert manifest["sources"]["spool_dir"] == str(tmp_path / "var" / "provision")
    assert backup.verify_backup(result)["schema"] == backup.BACKUP_SCHEMA

    with sqlite3.connect(result / "state.db") as connection:
        assert connection.execute("SELECT value FROM items").fetchone() == ("broker",)


def test_backup_quiesces_writers_and_restarts_in_order(tmp_path, monkeypatch):
    config_path, _, _, _, backup_root = _fixture(tmp_path)
    active = {
        "citespec-resource-broker.service",
        "citespec-resource-intake.service",
        "citespec-resource-provision.path",
    }
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(backup, "_should_quiesce", lambda quiesce: quiesce)
    monkeypatch.setattr(backup, "_systemctl_available", lambda: True)
    monkeypatch.setattr(backup, "_unit_is_active", lambda unit: unit in active)

    def stop_unit(unit: str) -> bool:
        active.discard(unit)
        events.append(("stop", unit))
        return True

    def start_unit(unit: str) -> None:
        active.add(unit)
        events.append(("start", unit))

    monkeypatch.setattr(backup, "_stop_unit", stop_unit)
    monkeypatch.setattr(backup, "_start_unit", start_unit)
    original_write = backup._write_backup

    def write_backup(**kwargs: object) -> Path:
        assert active == set()
        return original_write(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backup, "_write_backup", write_backup)

    backup.create_backup(config_path, backup_root, stamp="20260101-000000")

    assert [event for event in events if event[0] == "stop"] == [
        ("stop", "citespec-resource-broker.service"),
        ("stop", "citespec-resource-intake.service"),
        ("stop", "citespec-resource-provision.path"),
    ]
    assert [event for event in events if event[0] == "start"] == [
        ("start", "citespec-resource-provision.path"),
        ("start", "citespec-resource-intake.service"),
        ("start", "citespec-resource-broker.service"),
    ]
    assert active == {
        "citespec-resource-broker.service",
        "citespec-resource-intake.service",
        "citespec-resource-provision.path",
    }


def test_backup_restarts_writers_when_the_copy_fails(tmp_path, monkeypatch):
    config_path, _, _, _, backup_root = _fixture(tmp_path)
    active = {"citespec-resource-broker.service"}
    monkeypatch.setattr(backup, "_should_quiesce", lambda quiesce: quiesce)
    monkeypatch.setattr(backup, "_systemctl_available", lambda: True)
    monkeypatch.setattr(backup, "_unit_is_active", lambda unit: unit in active)
    monkeypatch.setattr(
        backup,
        "_stop_unit",
        lambda unit: active.discard(unit) or True,
    )
    monkeypatch.setattr(backup, "_start_unit", lambda unit: active.add(unit))
    monkeypatch.setattr(
        backup,
        "_write_backup",
        lambda **kwargs: (_ for _ in ()).throw(backup.BackupError("copy failed")),
    )

    with pytest.raises(backup.BackupError, match="copy failed"):
        backup.create_backup(config_path, backup_root, stamp="20260101-000000")

    assert active == {"citespec-resource-broker.service"}


def test_backup_no_quiesce_does_not_touch_units(tmp_path, monkeypatch):
    config_path, _, _, _, backup_root = _fixture(tmp_path)

    def unexpected(unit: str) -> bool:
        raise AssertionError(f"unit state was queried: {unit}")

    monkeypatch.setattr(backup, "_unit_is_active", unexpected)

    backup.create_backup(
        config_path,
        backup_root,
        stamp="20260101-000000",
        quiesce=False,
    )


def test_backup_lock_rejects_a_second_run(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    if os.name != "posix":
        pytest.skip("flock is the production lock")
    with backup._backup_lock(root):
        with pytest.raises(backup.BackupError, match="already running"):
            with backup._backup_lock(root):
                pass


def test_restore_reconciles_nonterminal_operations(tmp_path):
    from citespec_resource.membership import store as member_store

    config_path, state_db, _, _, backup_root = _fixture(tmp_path)
    connection = sqlite3.connect(state_db)
    connection.row_factory = sqlite3.Row
    try:
        member_id = member_store.create_member(
            connection,
            linux_uid=42424,
            linux_username="legacy",
            status="provisioning",
            display_name="Legacy",
        )
        operation_id, _ = member_store.create_operation(
            connection,
            action="provision_member",
            frozen_input={"username": "legacy"},
            member_id=member_id,
        )
        member_store.update_operation(connection, operation_id, status="running")
        connection.commit()
    finally:
        connection.close()
    result = backup.create_backup(config_path, backup_root, stamp="20260101-000000")
    restore_config, target, _, _ = _make_restore_target(tmp_path, with_intake_and_spool=False)

    restored = backup.restore_backup(
        result,
        restore_config,
        confirmed=True,
        skip_service_check=True,
    )

    assert restored["reconciled_operations"] == [
        {
            "operation_id": operation_id,
            "action": "provision_member",
            "previous_status": "running",
            "status": "failed",
        }
    ]
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    try:
        operation = member_store.get_operation(connection, operation_id)
        assert operation["status"] == "failed"
        assert operation["stage"] == "restore"
        member = member_store.get_member(connection, member_id)
        assert member["member"]["status"] == "provision_failed"
    finally:
        connection.close()


def test_restore_replaces_databases_and_spool_and_forces_maintenance(tmp_path):
    _, _, _, _, backup_root = _fixture(tmp_path)
    result = backup.create_backup(
        tmp_path / "etc" / "citespec-resource" / "config.toml",
        backup_root,
        stamp="20260101-000000",
    )
    restore_config, target, target_intake, target_spool = _make_restore_target(
        tmp_path, with_intake_and_spool=True
    )

    restored = backup.restore_backup(
        result,
        restore_config,
        confirmed=True,
        skip_service_check=True,
    )

    connection = connect(target)
    try:
        assert connection.execute("SELECT value FROM items").fetchone()["value"] == "broker"
        assert store.get_mode(connection) == "maintenance"
    finally:
        connection.close()
    connection = connect(target_intake)
    try:
        assert connection.execute("SELECT value FROM items").fetchone()["value"] == "intake"
    finally:
        connection.close()
    assert target_spool is not None
    assert (target_spool / "requests" / "op-1.json").is_file()
    assert not (target_spool / "requests" / "stale.json").exists()
    assert restored["state_db"] == str(target)
    assert restored["intake_db"] == str(target_intake)
    assert restored["spool_dir"] == str(target_spool)
    assert restored["mode"] == "maintenance"
    assert restored["safety_backup"] is not None
    assert Path(restored["safety_backup"]).parent == backup_root
    assert Path(restored["safety_backup"]).is_dir()
    backup.verify_backup(Path(restored["safety_backup"]))


def test_full_restore_replaces_config_tree_exactly(tmp_path):
    config_path, _, _, _, backup_root = _fixture(tmp_path)
    result = backup.create_backup(config_path, backup_root, stamp="20260101-000000")
    stale = config_path.parent / "obsolete.txt"
    stale.write_text("remove me\n", encoding="utf-8")

    restored = backup.restore_backup(
        result,
        config_path,
        restore_config=True,
        confirmed=True,
        skip_service_check=True,
    )

    assert restored["config_restored"] is True
    assert not stale.exists()
    assert (config_path.parent / "authorized-keys.d" / "alice").is_file()
    connection = connect(tmp_path / "var" / "state.db")
    try:
        assert store.get_mode(connection) == "maintenance"
    finally:
        connection.close()


def test_restore_stops_all_writer_units_before_replacing_state(tmp_path, monkeypatch):
    config_path, _, _, _, backup_root = _fixture(tmp_path)
    result = backup.create_backup(config_path, backup_root, stamp="20260101-000000")
    restore_config, target, _, _ = _make_restore_target(tmp_path, with_intake_and_spool=False)
    calls: list[str] = []

    def stop_writers() -> None:
        connection = sqlite3.connect(target)
        try:
            assert connection.execute("SELECT value FROM items").fetchone() == ("old",)
        finally:
            connection.close()
        calls.append("stop")

    monkeypatch.setattr(backup, "_stop_writer_units", stop_writers)

    backup.restore_backup(
        result,
        restore_config,
        confirmed=True,
    )

    assert calls == ["stop"]


def test_restore_rolls_back_all_swaps_when_post_restore_check_fails(tmp_path, monkeypatch):
    config_path, _, _, _, backup_root = _fixture(tmp_path)
    result = backup.create_backup(config_path, backup_root, stamp="20260101-000000")
    restore_config, target, _, _ = _make_restore_target(tmp_path, with_intake_and_spool=False)
    calls = 0

    def fail_after_swap(path: Path) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    monkeypatch.setattr(backup, "_force_maintenance", fail_after_swap)

    with pytest.raises(backup.BackupError, match="settings table"):
        backup.restore_backup(
            result,
            restore_config,
            confirmed=True,
            skip_service_check=True,
        )

    connection = sqlite3.connect(target)
    try:
        assert connection.execute("SELECT value FROM items").fetchone() == ("old",)
    finally:
        connection.close()
    assert not (target.parent / f".{target.name}.pre-restore-{os.getpid()}").exists()


def test_restore_rolls_back_wal_group_when_post_restore_check_fails(tmp_path, monkeypatch):
    config_path, _, _, _, backup_root = _fixture(tmp_path)
    result = backup.create_backup(config_path, backup_root, stamp="20260101-000000")
    restore_config, target, _, _ = _make_restore_target(tmp_path, with_intake_and_spool=False)
    _insert_uncheckpointed_wal(target, "wal-only")
    wal = target.with_name(target.name + "-wal")
    assert wal.stat().st_size > 0
    calls = 0

    def fail_after_swap(path: Path) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    monkeypatch.setattr(backup, "_force_maintenance", fail_after_swap)

    with pytest.raises(backup.BackupError, match="settings table"):
        backup.restore_backup(
            result,
            restore_config,
            confirmed=True,
            skip_service_check=True,
        )

    for suffix in ("", "-wal", "-shm"):
        stale = target.with_name(f".{target.name}{suffix}.pre-restore-{os.getpid()}")
        assert not stale.exists()
    connection = sqlite3.connect(target)
    try:
        assert {row[0] for row in connection.execute("SELECT value FROM items")} == {
            "old",
            "wal-only",
        }
    finally:
        connection.close()


def test_restore_aborts_before_swap_when_wal_checkpoint_is_busy(tmp_path):
    config_path, _, _, _, backup_root = _fixture(tmp_path)
    result = backup.create_backup(config_path, backup_root, stamp="20260101-000000")
    restore_config, target, _, _ = _make_restore_target(tmp_path, with_intake_and_spool=False)
    writer = connect(target)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO items VALUES ('wal-only')")
    writer.commit()
    writer.execute("BEGIN")
    writer.execute("SELECT value FROM items").fetchall()

    with pytest.raises(backup.BackupError, match="busy"):
        backup.restore_backup(
            result,
            restore_config,
            confirmed=True,
            skip_service_check=True,
        )

    for suffix in ("", "-wal", "-shm"):
        stale = target.with_name(f".{target.name}{suffix}.pre-restore-{os.getpid()}")
        assert not stale.exists()
    writer.rollback()
    writer.close()
    connection = sqlite3.connect(target)
    try:
        assert {row[0] for row in connection.execute("SELECT value FROM items")} == {
            "old",
            "wal-only",
        }
    finally:
        connection.close()


def test_restore_requires_confirmation_and_rejects_tampered_backup(tmp_path):
    config_path, _, _, _, backup_root = _fixture(tmp_path)
    result = backup.create_backup(config_path, backup_root, stamp="20260101-000000")

    with pytest.raises(backup.BackupError, match="confirmation"):
        backup.restore_backup(result, config_path, skip_service_check=True)

    (result / "etc" / "citespec-resource" / "config.toml").write_text("tampered\n")
    with pytest.raises(backup.BackupError, match="mismatch"):
        backup.verify_backup(result)


def test_backup_fails_if_configured_spool_is_missing(tmp_path):
    config_path, _, _, spool_dir, backup_root = _fixture(tmp_path)
    spool_dir.rename(tmp_path / "var" / "provision-moved")

    with pytest.raises(backup.BackupError, match="provisioning spool"):
        backup.create_backup(config_path, backup_root, stamp="20260101-000000")


def test_enabled_portal_with_missing_database_fails_closed(tmp_path):
    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    state_db = tmp_path / "state.db"
    _make_state_db(state_db, "broker")
    portal_db = tmp_path / "missing-portal.db"
    config_path = config_dir / "config.toml"
    _write_config(config_path, state_db, portal_db=portal_db, portal_enabled=True)

    with pytest.raises(backup.BackupError, match="Portal is enabled"):
        backup.create_backup(config_path, tmp_path / "backups", stamp="20260101-000000")
    assert not (tmp_path / "backups").exists()


def test_backup_marks_enabled_portal_in_manifest(tmp_path):
    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    state_db = tmp_path / "state.db"
    portal_db = tmp_path / "portal.db"
    _make_state_db(state_db, "broker")
    _make_db(portal_db, "portal")
    config_path = config_dir / "config.toml"
    _write_config(config_path, state_db, portal_db=portal_db, portal_enabled=True)

    result = backup.create_backup(config_path, tmp_path / "backups", stamp="20260101-000000")
    manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"]["portal_enabled"] is True
    assert "portal.db" in {entry["path"] for entry in manifest["files"]}


def test_backup_and_restore_include_portal_database(tmp_path):
    config_dir = tmp_path / "portal-source" / "etc"
    config_dir.mkdir(parents=True)
    state_db = tmp_path / "portal-source" / "state.db"
    intake_db = tmp_path / "portal-source" / "intake.db"
    spool_dir = tmp_path / "portal-source" / "provision"
    portal_db = tmp_path / "portal-source" / "portal.db"
    _make_state_db(state_db, "broker")
    _make_db(intake_db, "intake")
    _make_db(portal_db, "portal-source")
    _make_spool(spool_dir)
    source_config = config_dir / "config.toml"
    _write_config(
        source_config,
        state_db,
        intake_db,
        spool_dir,
        portal_db=portal_db,
    )
    backup_root = tmp_path / "portal-source" / "backups"
    result = backup.create_backup(source_config, backup_root, stamp="20260101-000000")
    manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    assert "portal.db" in {entry["path"] for entry in manifest["files"]}
    assert manifest["sources"]["portal_db"] == str(portal_db)
    assert backup.verify_backup(result)["schema"] == backup.BACKUP_SCHEMA

    restore_dir = tmp_path / "portal-restore"
    target_config_dir = restore_dir / "etc"
    target_config_dir.mkdir(parents=True)
    target_state = restore_dir / "state.db"
    target_intake = restore_dir / "intake.db"
    target_spool = restore_dir / "provision"
    target_portal = restore_dir / "portal.db"
    _make_state_db(target_state, "old")
    _make_db(target_intake, "old-intake")
    _make_db(target_portal, "old-portal")
    _make_spool(target_spool)
    target_config = target_config_dir / "config.toml"
    _write_config(
        target_config,
        target_state,
        target_intake,
        target_spool,
        portal_db=target_portal,
    )

    restored = backup.restore_backup(
        result,
        target_config,
        confirmed=True,
        skip_service_check=True,
    )
    assert restored["portal_db"] == str(target_portal)
    assert restored["mode"] == "maintenance"
    with sqlite3.connect(target_portal) as connection:
        assert connection.execute("SELECT value FROM items").fetchone()[0] == "portal-source"


def test_retention_keeps_newest_state_backups_and_preserves_other_directories(tmp_path):
    config_path, _, _, _, backup_root = _fixture(tmp_path)
    backup_root.mkdir()
    (backup_root / "pre-cutover-20260101").mkdir()
    (backup_root / "state-old").mkdir()
    for stamp in ("20260101-000000", "20260101-000001", "20260101-000002"):
        backup.create_backup(config_path, backup_root, stamp=stamp, keep=2)

    state_backups = sorted(path.name for path in backup_root.glob("state-*") if path.is_dir())
    assert len([name for name in state_backups if name.startswith("state-2026")]) == 2
    assert (backup_root / "pre-cutover-20260101").is_dir()
    assert (backup_root / "state-old").is_dir()


def test_cli_verify_and_backup_json(tmp_path, capsys):
    config_path, _, _, _, backup_root = _fixture(tmp_path)
    assert (
        backup.main(
            ["backup", "--config", str(config_path), "--backup-root", str(backup_root), "--json"]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert Path(output["backup"]).is_dir()
    assert backup.main(["verify", output["backup"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == backup.BACKUP_SCHEMA
