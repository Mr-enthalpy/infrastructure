from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from citespec_resource import backup


def _write_config(
    config_path: Path,
    state_db: Path,
    intake_db: Path | None = None,
) -> None:
    lines = [
        "[server]",
        'name = "test"',
        'ssh_alias = "test"',
        f'state_db = "{state_db.as_posix()}"',
    ]
    if intake_db is not None:
        lines += ["", "[onboarding]", f'intake_db = "{intake_db.as_posix()}"']
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


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    config_dir = tmp_path / "etc" / "citespec-resource"
    config_dir.mkdir(parents=True)
    state_db = tmp_path / "var" / "state.db"
    intake_db = tmp_path / "var" / "intake.db"
    _make_db(state_db, "broker")
    _make_db(intake_db, "intake")
    (config_dir / "authorized-keys.d").mkdir()
    (config_dir / "authorized-keys.d" / "alice").write_text("ssh-ed25519 AAAA test\n")
    config_path = config_dir / "config.toml"
    _write_config(config_path, state_db, intake_db)
    return config_path, state_db, intake_db, tmp_path / "backups"


def test_backup_contains_state_intake_config_and_verified_manifest(tmp_path):
    config_path, _, _, backup_root = _fixture(tmp_path)

    result = backup.create_backup(config_path, backup_root, stamp="20260101-000000")

    assert result.is_dir()
    assert (result / "state.db").is_file()
    assert (result / "intake.db").is_file()
    assert (result / "etc" / "citespec-resource" / "authorized-keys.d" / "alice").is_file()
    manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == backup.BACKUP_SCHEMA
    assert {entry["path"] for entry in manifest["files"]} >= {
        "state.db",
        "intake.db",
        "etc/citespec-resource/config.toml",
    }
    assert backup.verify_backup(result)["schema"] == backup.BACKUP_SCHEMA

    with sqlite3.connect(result / "state.db") as connection:
        assert connection.execute("SELECT value FROM items").fetchone() == ("broker",)


def test_restore_replaces_state_database_and_keeps_safety_copy(tmp_path):
    config_path, _, _, backup_root = _fixture(tmp_path)
    result = backup.create_backup(config_path, backup_root, stamp="20260101-000000")
    target = tmp_path / "restore" / "state.db"
    _make_db(target, "old")
    restore_config = tmp_path / "restore" / "config.toml"
    _write_config(restore_config, target)

    restored = backup.restore_backup(
        result,
        restore_config,
        state_db=target,
        confirmed=True,
        skip_service_check=True,
    )

    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT value FROM items").fetchone() == ("broker",)
    assert restored["state_db"] == str(target)
    assert restored["safety_backup"] is not None
    assert Path(restored["safety_backup"]).is_dir()


def test_restore_requires_confirmation_and_rejects_tampered_backup(tmp_path):
    config_path, _, _, backup_root = _fixture(tmp_path)
    result = backup.create_backup(config_path, backup_root, stamp="20260101-000000")

    with pytest.raises(backup.BackupError, match="confirmation"):
        backup.restore_backup(result, config_path, skip_service_check=True)

    (result / "etc" / "citespec-resource" / "config.toml").write_text("tampered\n")
    with pytest.raises(backup.BackupError, match="mismatch"):
        backup.verify_backup(result)


def test_retention_keeps_newest_state_backups_and_preserves_other_directories(tmp_path):
    config_path, _, _, backup_root = _fixture(tmp_path)
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
    config_path, _, _, backup_root = _fixture(tmp_path)
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
