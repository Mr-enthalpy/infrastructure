"""Consistent state backups and restore drills for Citespec.

The backup set contains:
- the broker SQLite database, copied through SQLite's backup API;
- the intake SQLite database when configured;
- the portal SQLite database when it exists;
- the root-owned ``/etc/citespec-resource`` configuration/key directory;
- the provisioning spool, including in-flight and completed operations.

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
BACKUP_SCHEMA = 2
SPOOL_BACKUP_NAME = "provision"
# Units that can write the state/intake databases or the provisioning spool.
# Restore stops every one of them and re-checks before touching live files.
WRITER_UNITS = (
    "citespec-resource-provision.path",
    "citespec-resource-provision.service",
    "citespec-resource-intake.service",
    "citespec-resource-broker.service",
    "citespec-resource-portal.service",
)
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
) -> tuple[Path, Path | None, Path | None, Path | None, Path]:
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

    onboarding = data.get("onboarding")
    if not isinstance(onboarding, dict):
        onboarding = {}
    intake_value = onboarding.get("intake_db")
    intake_db = (
        Path(intake_value).expanduser() if isinstance(intake_value, str) and intake_value else None
    )
    spool_value = onboarding.get("provisioning_spool")
    spool_dir = (
        Path(spool_value).expanduser() if isinstance(spool_value, str) and spool_value else None
    )
    portal = data.get("portal")
    if not isinstance(portal, dict):
        portal = {}
    portal_value = portal.get("db_path")
    portal_db = (
        Path(portal_value).expanduser() if isinstance(portal_value, str) and portal_value else None
    )
    return state_db, intake_db, spool_dir, portal_db, config_path.parent


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
    state_db, intake_db, spool_dir, portal_db, config_dir = _read_config_paths(
        config,
        Path(state_db_override) if state_db_override is not None else None,
    )
    if not config_dir.is_dir():
        raise BackupError(f"configuration directory does not exist: {config_dir}")
    if not state_db.is_file():
        raise BackupError(f"state database does not exist: {state_db}")
    if intake_db is not None and not intake_db.is_file():
        raise BackupError(f"intake database does not exist: {intake_db}")
    if spool_dir is not None and not spool_dir.is_dir():
        raise BackupError(f"provisioning spool does not exist: {spool_dir}")
    if portal_db is not None and not portal_db.is_file():
        portal_db = None

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
        if intake_db is not None:
            _sqlite_backup(intake_db, temporary / "intake.db")
        if portal_db is not None:
            _sqlite_backup(portal_db, temporary / "portal.db")
        # Preserve the whole root-owned config tree, including central member
        # keys. Symlinks are copied as links, never followed during backup.
        shutil.copytree(
            config_dir,
            temporary / "etc" / config_dir.name,
            symlinks=True,
            copy_function=shutil.copy2,
        )
        # The spool is part of the recovery state: operation ids and results
        # must match the databases after a restore.
        if spool_dir is not None:
            shutil.copytree(
                spool_dir,
                temporary / SPOOL_BACKUP_NAME,
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
                "portal_db": str(portal_db) if portal_db is not None else None,
                "spool_dir": str(spool_dir) if spool_dir is not None else None,
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
        if key in {"state.db", "intake.db", "portal.db"}:
            _sqlite_integrity(path)
    if "state.db" not in seen:
        raise BackupError(f"backup manifest has no state.db: {manifest_path}")
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        raise BackupError(f"backup manifest has no sources: {manifest_path}")
    if sources.get("intake_db") is not None and "intake.db" not in seen:
        raise BackupError(f"backup manifest has no intake.db: {manifest_path}")
    if sources.get("portal_db") is not None and "portal.db" not in seen:
        raise BackupError(f"backup manifest has no portal.db: {manifest_path}")
    config_value = sources.get("config")
    config_dir_value = sources.get("config_dir")
    if not isinstance(config_value, str) or not isinstance(config_dir_value, str):
        raise BackupError(f"backup manifest has no config sources: {manifest_path}")
    config_name = Path(config_value).name
    config_dir_name = Path(config_dir_value).name
    restored_config = root / "etc" / config_dir_name / config_name
    if not restored_config.is_file():
        raise BackupError(f"backup has no config file: {restored_config}")
    if sources.get("spool_dir") is not None and not (root / SPOOL_BACKUP_NAME).is_dir():
        raise BackupError(f"backup has no provisioning spool: {root / SPOOL_BACKUP_NAME}")
    return payload


def _systemctl_available() -> bool:
    return os.name == "posix" and shutil.which("systemctl") is not None


def _unit_is_active(unit: str) -> bool:
    if not _systemctl_available():
        return False
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _stop_writer_units() -> None:
    if not _systemctl_available():
        raise BackupError(
            "systemctl is unavailable; stop broker/intake/provisioning manually "
            "and pass --skip-service-check only after verifying they are inactive"
        )
    for unit in WRITER_UNITS:
        result = subprocess.run(
            ["systemctl", "stop", unit],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip()
            if "not loaded" in detail or "could not be found" in detail:
                continue
            raise BackupError(f"could not stop {unit}: {detail}")
    active = [unit for unit in WRITER_UNITS if _unit_is_active(unit)]
    if active:
        raise BackupError("writers are still active: " + ", ".join(active))


def _force_maintenance(path: Path) -> bool:
    """Set the Citespec mode to maintenance; return False for a non-Citespec DB."""
    if not path.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
        ).fetchone()
        if table is None:
            return False
        from . import store

        store.set_mode(connection, "maintenance", actor_uid=0, now=datetime.now(UTC))
        return store.get_mode(connection, "") == "maintenance"
    except sqlite3.Error as exc:
        raise BackupError(f"cannot force maintenance mode in {path}: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _stage_sqlite(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name(f".{destination.name}.restore-{os.getpid()}")
    try:
        staged.unlink(missing_ok=True)
        shutil.copy2(source, staged)
        _chmod(staged, 0o600)
        _sqlite_integrity(staged)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def _inherit_owner(staged: Path, target: Path) -> None:
    """Keep the intake DB writable by its low-privilege service account.

    The backup copy is created by root, but the live intake database may be
    owned by ``citespec-intake``.  Restore it with the target's owner (or the
    owner of its state directory when the DB does not exist yet).
    """
    chown = getattr(os, "chown", None)
    if chown is None:  # pragma: no cover - Windows test host
        return
    try:
        info = target.stat()
    except FileNotFoundError:
        try:
            info = target.parent.stat()
        except OSError:
            return
    try:
        chown(staged, info.st_uid, info.st_gid)
    except OSError as exc:
        raise BackupError(f"cannot restore database ownership for {target}: {exc}") from exc


def _stage_tree(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name(f".{destination.name}.restore-{os.getpid()}")
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(source, staged, symlinks=True, copy_function=shutil.copy2)
    return staged


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _swap_in(staged: Path, target: Path, *, sqlite: bool = False) -> Path | None:
    """Replace one target, returning the old path for rollback."""
    old = target.with_name(f".{target.name}.pre-restore-{os.getpid()}")
    _remove_path(old)
    if sqlite:
        for suffix in ("-wal", "-shm"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)
    had_target = target.exists() or target.is_symlink()
    if had_target:
        os.replace(target, old)
    try:
        os.replace(staged, target)
    except Exception:
        if had_target and old.exists():
            os.replace(old, target)
        raise
    return old if had_target else None


def _rollback_swaps(swaps: list[tuple[Path, Path | None]]) -> None:
    for target, old in reversed(swaps):
        _remove_path(target)
        if old is not None and old.exists():
            os.replace(old, target)


def _cleanup_swaps(swaps: list[tuple[Path, Path | None]]) -> None:
    for _target, old in swaps:
        if old is not None:
            _remove_path(old)


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
    """Restore databases, optional exact config tree, and the provisioning spool.

    The command stops every writer unit itself, forces the current and restored
    state to maintenance, stages and verifies every file before swapping any
    live path, and rolls back all swaps if a later step fails.
    """

    if not confirmed:
        raise BackupError("restore requires explicit confirmation (--yes)")
    root = Path(backup_dir)
    payload = verify_backup(root)
    config = Path(config_path)
    state_target, intake_target, spool_target, portal_target, config_dir = _read_config_paths(
        config,
        Path(state_db) if state_db is not None else None,
    )
    if not skip_service_check:
        _stop_writer_units()

    # Even if a later step fails, the live database must not reopen active.
    _force_maintenance(state_target)

    safety_path: Path | None = None
    if safety_backup and state_target.exists():
        safety_path = create_backup(
            config,
            root.parent,
            keep=0,
            prefix="pre-restore",
            state_db_override=state_target,
        )

    staged: list[Path] = []
    swaps: list[tuple[Path, Path | None]] = []
    succeeded = False
    try:
        staged_state = _stage_sqlite(root / "state.db", state_target)
        staged.append(staged_state)
        staged_intake: Path | None = None
        if intake_target is not None:
            intake_source = root / "intake.db"
            if not intake_source.is_file():
                raise BackupError("backup does not contain the configured intake database")
            staged_intake = _stage_sqlite(intake_source, intake_target)
            _inherit_owner(staged_intake, intake_target)
            staged.append(staged_intake)
        staged_portal: Path | None = None
        if portal_target is not None:
            portal_source = root / "portal.db"
            if portal_source.is_file():
                staged_portal = _stage_sqlite(portal_source, portal_target)
                _inherit_owner(staged_portal, portal_target)
                staged.append(staged_portal)
            elif payload.get("sources", {}).get("portal_db") is not None:
                raise BackupError("backup does not contain the configured portal database")
        staged_spool: Path | None = None
        if spool_target is not None:
            spool_source = root / SPOOL_BACKUP_NAME
            if not spool_source.is_dir():
                raise BackupError("backup does not contain the configured provisioning spool")
            staged_spool = _stage_tree(spool_source, spool_target)
            staged.append(staged_spool)
        staged_config: Path | None = None
        if restore_config:
            source_etc = root / "etc" / config_dir.name
            if not source_etc.is_dir():
                raise BackupError(f"backup has no config directory: {source_etc}")
            staged_config = _stage_tree(source_etc, config_dir)
            staged.append(staged_config)
            (
                restored_state,
                restored_intake,
                restored_spool,
                restored_portal,
                _,
            ) = _read_config_paths(staged_config / config.name)
            if restored_state != state_target:
                raise BackupError(
                    "restored config server.state_db does not match the restore target"
                )
            if restored_intake != intake_target:
                raise BackupError(
                    "restored config onboarding.intake_db does not match the restore target"
                )
            if restored_spool != spool_target:
                raise BackupError(
                    "restored config onboarding.provisioning_spool does not match the target"
                )
            if restored_portal != portal_target:
                raise BackupError(
                    "restored config portal.db_path does not match the restore target"
                )

        swaps.append((state_target, _swap_in(staged_state, state_target, sqlite=True)))
        staged.remove(staged_state)
        if staged_intake is not None and intake_target is not None:
            swaps.append((intake_target, _swap_in(staged_intake, intake_target, sqlite=True)))
            staged.remove(staged_intake)
        if staged_portal is not None and portal_target is not None:
            swaps.append((portal_target, _swap_in(staged_portal, portal_target, sqlite=True)))
            staged.remove(staged_portal)
        if staged_spool is not None and spool_target is not None:
            swaps.append((spool_target, _swap_in(staged_spool, spool_target)))
            staged.remove(staged_spool)
        if staged_config is not None:
            swaps.append((config_dir, _swap_in(staged_config, config_dir)))
            staged.remove(staged_config)

        if not _force_maintenance(state_target):
            raise BackupError("restored state database has no Citespec settings table")
        _sqlite_integrity(state_target)
        if intake_target is not None:
            _sqlite_integrity(intake_target)
        if portal_target is not None and portal_target.is_file():
            _sqlite_integrity(portal_target)
        if spool_target is not None and not spool_target.is_dir():
            raise BackupError("restored provisioning spool is missing")
        if restore_config and not config_dir.is_dir():
            raise BackupError("restored configuration directory is missing")
        succeeded = True
    except Exception:
        _rollback_swaps(swaps)
        raise
    finally:
        for path in staged:
            _remove_path(path)
        if succeeded:
            _cleanup_swaps(swaps)

    return {
        "backup": str(root),
        "state_db": str(state_target),
        "intake_db": str(intake_target) if intake_target is not None else None,
        "portal_db": str(portal_target) if portal_target is not None else None,
        "spool_dir": str(spool_target) if spool_target is not None else None,
        "config_restored": restore_config,
        "mode": "maintenance",
        "schema": int(payload.get("schema", BACKUP_SCHEMA)),
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
