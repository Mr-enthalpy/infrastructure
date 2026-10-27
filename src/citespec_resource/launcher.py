"""Stable packaged-client launcher.

The launcher is built once and lives next to ``versions/``.  It checks the
signed manifest at most once per day, installs a newer payload atomically, and
then executes the payload with the original arguments.  Auto-update failures
never replace a working payload; they only print a warning to stderr so JSON
stdout remains clean.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .update import (
    VERSIONS_DIR,
    UpdateError,
    check_for_update,
    find_app_root,
    install_update,
    mark_update_checked,
    resolve_payload,
    update_check_due,
)

_COMMANDS_WITHOUT_UPDATE = {"help", "--help", "-h", "version", "update"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = find_app_root()
    if root is not None and _should_auto_update(args):
        _maybe_update(root)

    payload = resolve_payload(root) if root is not None else None
    if payload is None:
        print(
            "citespec-resource: no installed client payload; "
            "set CITESPEC_UPDATE_MANIFEST or reinstall the client",
            file=sys.stderr,
        )
        return 1
    return _run(payload, args)


def _should_auto_update(args: list[str]) -> bool:
    if os.environ.get("CITESPEC_SKIP_UPDATE", "").strip() in {"1", "true", "yes"}:
        return False
    if args and args[0] in _COMMANDS_WITHOUT_UPDATE:
        return False
    return True


def _maybe_update(root: Path) -> None:
    current = resolve_payload(root)
    if current is not None and not update_check_due(root):
        return
    timeout = _float_env("CITESPEC_UPDATE_TIMEOUT", 5.0)
    if current is not None and current.parent.parent.name == VERSIONS_DIR:
        current_version = current.parent.name
    else:
        current_version = __version__
    try:
        info = check_for_update(
            current_version=current_version,
            timeout=timeout,
            allow_insecure=_allow_insecure(),
        )
        mark_update_checked(root)
        if not info.available:
            return
        print(
            f"citespec-resource: installing {info.latest_version} (current {info.current_version})",
            file=sys.stderr,
        )
        install_update(
            info,
            app_root=root,
            allow_insecure=_allow_insecure(),
            timeout=_float_env("CITESPEC_UPDATE_DOWNLOAD_TIMEOUT", 120.0),
        )
    except UpdateError as exc:
        mark_update_checked(root)
        if current is None:
            print(f"citespec-resource: update failed: {exc}", file=sys.stderr)
        else:
            print(
                f"citespec-resource: update check failed; using {current.name}: {exc}",
                file=sys.stderr,
            )
    except OSError as exc:
        mark_update_checked(root)
        if current is None:
            print(f"citespec-resource: update failed: {exc}", file=sys.stderr)
        else:
            print(f"citespec-resource: update check failed: {exc}", file=sys.stderr)


def _run(payload: Path, args: list[str]) -> int:
    try:
        completed = subprocess.run([str(payload), *args], check=False)
    except OSError as exc:
        print(f"citespec-resource: cannot start client payload: {exc}", file=sys.stderr)
        return 1
    return completed.returncode


def _allow_insecure() -> bool:
    return os.environ.get("CITESPEC_UPDATE_ALLOW_HTTP", "").strip() in {"1", "true", "yes"}


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.5, float(raw))
    except ValueError:
        return default


if __name__ == "__main__":  # pragma: no cover - exercised by the built launcher
    raise SystemExit(main())
