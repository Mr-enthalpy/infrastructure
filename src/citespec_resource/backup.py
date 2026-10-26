"""Consistent state backups and restore drills for Citespec.

The backup set contains:
- the broker SQLite database, copied through SQLite's backup API;
- the intake SQLite database when configured and present;
- the root-owned ``/etc/citespec-resource`` configuration/key directory.

Workspaces are intentionally not copied: they can be very large and have their
own quota/retention policy. The manifest records source paths and SHA-256
checksums so a restore can be verified before it touches live state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_CONFIG = "/etc/citespec-resource/config.toml"
DEFAULT_BACKUP_ROOT = "/var/backups/citespec-resource"
DEFAULT_KEEP = 14
MANIFEST_NAME = "manifest.json"
BACKUP_SCHEMA = 1
SERVICE_NAME = "citespec-resource-broker.service"
_STATE_BACKUP_NAME = re.compile(r"^state-\d{8}-\d{6}-\d+$")


class BackupError(RuntimeError):
    """Raised when a backup or restore cannot be completed safely."""


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        # Windows test environments do not implement POSIX modes. Production
        # runs on Linux, where the installer also sets the directory mode.
        pass


def _read_config_paths(
    config_path: Path,
    state_db_override: Path | None = None,
) -> tuple[Path, Path | None, Path]:
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BackupError(f"cannot read config {config_path}: {exc}") from exc

    server = data.get("server")
    if not isinstance(server, dict):
        raise BackupError(f"{config_path} has no [server] section")
    state_value = state_db_override or server.get("state_db")
    if not isinstance(state_value, (str, os.PathLike)) or not str(state_value):
        raise BackupError(f"{config_path} has no server.state_db")
    state_db = Path(state_value).expanduser()

    intake_value = (
        data.get("onboarding", {}).get("intake_db")
        if isinstance(data.get("onboarding"), dict)
        else None
    )
    intake_db = (
        Path(intake_value).expanduser() if isinstance(intake_value, str) and intake_value else None
    )
    return state_db, intake_db, config_path.parent


def _sqlite_backup(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise BackupError(f"SQLite database does not exist: {source}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_conn: sqlite3.Connection | None = None
    destination_conn: sqlite3.Connection | None = None
    try:
        uri = source.resolve().as_uri() + "?mode=ro"
        source_conn = sqlite3.connect(uri, uri=True)
        destination_conn = sqlite3.connect(destination)
        source_conn.backup(destination_conn)
        destination_conn.execute("PRAGMA journal_mode=DELETE")
        row = destination_conn.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise BackupError(f"SQLite integrity check failed for {source}")
    except sqlite3.Error as exc:
        raise BackupError(f"cannot back up SQLite database {source}: {exc}") from exc
    finally:
        if destination_conn is not None:
            destination_conn.close()
        if source_conn is not None:
            source_conn.close()
    _chmod(destination, 0o600)


def _sqlite_integrity(path: Path) -> None:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise BackupError(f"SQLite integrity check failed: {path}")
    except sqlite3.Error as exc:
        raise BackupError(f"cannot verify SQLite database {path}: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_files(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.name == MANIFEST_NAME or path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return entries


def _write_manifest(root: Path, payload: dict[str, Any]) -> None:
    manifest = root / MANIFEST_NAME
    manifest.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _chmod(manifest, 0o600)


def _prune_backups(backup_root: Path, keep: int) -> list[Path]:
    if keep <= 0:
        return []
    candidates = sorted(
        (
            child
            for child in backup_root.iterdir()
            if child.is_dir()
            and _STATE_BACKUP_NAME.fullmatch(child.name) is not None
            and (child / MANIFEST_NAME).is_file()
            and not child.is_symlink()
        ),
        key=lambda path: path.name,
    )
    removed: list[Path] = []
    for stale in candidates[:-keep]:
        shutil.rmtree(stale)
        removed.append(stale)
    return removed


def create_backup(
    config_path: str | Path = DEFAULT_CONFIG,
    backup_root: str | Path = DEFAULT_BACKUP_ROOT,
    *,
    keep: int = DEFAULT_KEEP,
    prefix: str = "state",
    state_db_override: str | Path | None = None,
    stamp: str | None = None,
) -> Path:
    """Create and verify one timestamped backup; return its directory."""

    config = Path(config_path)
    root = Path(backup_root)
    state_db, intake_db, config_dir = _read_config_paths(
        config,
        Path(state_db_override) if state_db_override is not None else None,
    )
    if not config_dir.is_dir():
        raise BackupError(f"configuration directory does not exist: {config_dir}")
    if not state_db.is_file():
        raise BackupError(f"state database does not exist: {state_db}")

    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod(root, 0o700)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target = root / f"{prefix}-{stamp}-{os.getpid()}"
    temporary = root / f".{target.name}.tmp"
    if target.exists() or temporary.exists():
        raise BackupError(f"backup path already exists: {target}")
    temporary.mkdir(mode=0o700)

    try:
        _sqlite_backup(state_db, temporary / "state.db")
        if intake_db is not None and intake_db.is_file():
            _sqlite_backup(intake_db, temporary / "intake.db")
        # Preserve the whole root-owned config tree, including central member
        # keys. Symlinks are copied as links, never followed during backup.
        shutil.copytree(
            config_dir,
            temporary / "etc" / config_dir.name,
            symlinks=True,
            copy_function=shutil.copy2,
        )
        payload: dict[str, Any] = {
            "schema": BACKUP_SCHEMA,
            "created_at": datetime.now(UTC).isoformat(),
            "host": socket.gethostname(),
            "sources": {
                "config": str(config),
                "config_dir": str(config_dir),
                "state_db": str(state_db),
                "intake_db": str(intake_db) if intake_db is not None else None,
            },
            "files": _manifest_files(temporary),
        }
        _write_manifest(temporary, payload)
        verify_backup(temporary)
        temporary.replace(target)
        _chmod(target, 0o700)
        _prune_backups(root, keep)
        return target
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_backup(backup_dir: str | Path) -> dict[str, Any]:
    """Verify manifest contents and SQLite integrity for a backup directory."""

    root = Path(backup_dir)
    manifest_path = root / MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"cannot read backup manifest {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != BACKUP_SCHEMA:
        raise BackupError(f"unsupported backup manifest: {manifest_path}")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise BackupError(f"backup manifest has no files: {manifest_path}")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise BackupError(f"invalid manifest entry in {manifest_path}")
        relative = PurePosixPath(str(entry.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise BackupError(f"unsafe backup path in manifest: {relative}")
        key = relative.as_posix()
        if key in seen:
            raise BackupError(f"duplicate backup path in manifest: {key}")
        seen.add(key)
        path = root.joinpath(*relative.parts)
        if not path.is_file():
            raise BackupError(f"backup file is missing: {path}")
        if path.stat().st_size != int(entry.get("size", -1)):
            raise BackupError(f"backup file size mismatch: {path}")
        if _sha256(path) != str(entry.get("sha256", "")):
            raise BackupError(f"backup file checksum mismatch: {path}")
        if key in {"state.db", "intake.db"}:
            _sqlite_integrity(path)
    return payload


def _service_is_active() -> bool:
    if os.name != "posix" or shutil.which("systemctl") is None:
        return False
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", SERVICE_NAME],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.restore-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        _chmod(temporary, 0o600)
        _sqlite_integrity(temporary)
        for suffix in ("-wal", "-shm"):
            sidecar = destination.with_name(destination.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def restore_backup(
    backup_dir: str | Path,
    config_path: str | Path = DEFAULT_CONFIG,
    *,
    state_db: str | Path | None = None,
    restore_config: bool = False,
    confirmed: bool = False,
    safety_backup: bool = True,
    skip_service_check: bool = False,
) -> dict[str, Any]:
    """Restore a verified backup. Live broker use requires confirmed=True."""

    if not confirmed:
        raise BackupError("restore requires explicit confirmation (--yes)")
    root = Path(backup_dir)
    verify_backup(root)
    config = Path(config_path)
    state_target, _intake_db, config_dir = _read_config_paths(
        config,
        Path(state_db) if state_db is not None else None,
    )
    if not skip_service_check and _service_is_active():
        raise BackupError("broker is active; stop citespec-resource-broker.service before restore")

    safety_path: Path | None = None
    if safety_backup and state_target.exists():
        safety_path = create_backup(
            config,
            root.parent,
            keep=0,
            prefix="pre-restore",
            state_db_override=state_target,
        )

    _atomic_copy(root / "state.db", state_target)
    config_restored = False
    if restore_config:
        source_etc = root / "etc" / config_dir.name
        if not source_etc.is_dir():
            raise BackupError(f"backup has no config directory: {source_etc}")
        config_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_etc, config_dir, dirs_exist_ok=True, symlinks=True)
        config_restored = True

    return {
        "backup": str(root),
        "state_db": str(state_target),
        "config_restored": config_restored,
        "safety_backup": str(safety_path) if safety_path is not None else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="citespec-resource-backup",
        description="Create, verify, and restore Citespec state backups.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="create a consistent state backup")
    backup.add_argument("--config", default=DEFAULT_CONFIG)
    backup.add_argument("--backup-root", default=DEFAULT_BACKUP_ROOT)
    backup.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    backup.add_argument("--state-db", default=None)
    backup.add_argument("--json", action="store_true")

    verify = commands.add_parser("verify", help="verify a backup directory")
    verify.add_argument("backup_dir")
    verify.add_argument("--json", action="store_true")

    restore = commands.add_parser("restore", help="restore a verified backup")
    restore.add_argument("backup_dir")
    restore.add_argument("--config", default=DEFAULT_CONFIG)
    restore.add_argument("--state-db", default=None)
    restore.add_argument("--full", action="store_true", help="also restore the config/key tree")
    restore.add_argument("--yes", action="store_true")
    restore.add_argument("--no-safety-backup", action="store_true")
    restore.add_argument("--skip-service-check", action="store_true")
    restore.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            path = create_backup(
                args.config,
                args.backup_root,
                keep=args.keep,
                state_db_override=args.state_db,
            )
            payload = {"backup": str(path), "schema": BACKUP_SCHEMA}
        elif args.command == "verify":
            payload = verify_backup(args.backup_dir)
        else:
            payload = restore_backup(
                args.backup_dir,
                args.config,
                state_db=args.state_db,
                restore_config=args.full,
                confirmed=args.yes,
                safety_backup=not args.no_safety_backup,
                skip_service_check=args.skip_service_check,
            )
    except BackupError as exc:
        print(f"backup: error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
