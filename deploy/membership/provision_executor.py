#!/usr/bin/env python3
"""Narrow root-owned provisioning executor (goal member-onboarding-and-ux.md §8.2).

Reads ONE request JSON from stdin (operation_id, action, frozen_input),
answers with ONE result JSON on stdout:

    {"operation_id": ..., "action": ..., "status": "succeeded", "stages": [...]}
    {"operation_id": ..., "status": "failed", "stage": "...", "error": "..."}

Hard limits (the whole point of this process):
- fixed actions only; field whitelist re-validated here independently;
- no shell interpretation: every command is a fixed argv against fixed
  program paths with validated arguments;
- refuses root, system/service accounts, and reserved recovery names;
- account creation reuses deploy/create_user.sh (the battle-tested path) so
  member provisioning and the admin CLI follow one set of rules;
- keys are installed into a root-managed central directory
  (/etc/citespec-resource/authorized-keys.d/<username>) that sshd consumes via
  AuthorizedKeysCommand/Include — members cannot rebuild revoked entries;
- idempotent: re-running a finished request re-verifies and reports success.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised only on the Linux executor host
    import pwd
except ImportError:  # Windows test host
    pwd = None  # type: ignore[assignment]

CREATE_USER_SCRIPT = Path("/usr/local/libexec/citespec-resource/create_user.sh")
CENTRAL_KEY_DIR = Path("/etc/citespec-resource/authorized-keys.d")
WORKSPACE_ROOT = Path(os.environ.get("CITESPEC_RESOURCE_WORKSPACE_ROOT", "/data1/citespec-users"))
QUOTA_GB_DEFAULT = 200

FORBIDDEN_NAMES = {
    "root",
    "daemon",
    "bin",
    "sys",
    "sync",
    "games",
    "man",
    "lp",
    "mail",
    "news",
    "uucp",
    "proxy",
    "www-data",
    "backup",
    "list",
    "irc",
    "gnats",
    "nobody",
    "sshd",
    "systemd-network",
    "systemd-resolve",
}

ALLOWED_ACTIONS = (
    "inspect_member",
    "provision_member",
    "set_keys",
    "disable_member",
    "enable_member",
)
ACTION_FIELDS: dict[str, tuple[str, ...]] = {
    "inspect_member": ("username",),
    "provision_member": (
        "username",
        "display_name",
        "public_keys",
        "workspace_root",
        "quota_gb",
        "valid_until",
        "allow_existing",
    ),
    "set_keys": ("username", "public_keys"),
    "disable_member": ("username", "reason"),
    "enable_member": ("username", "public_keys"),
}
PUBLIC_KEY_TYPES = {
    "ssh-rsa",
    "ssh-dss",
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}


class StageError(RuntimeError):
    def __init__(self, stage: str, error: str) -> None:
        super().__init__(error)
        self.stage = stage
        self.error = error


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise StageError(argv[0], result.stderr.strip() or f"exit {result.returncode}")
    return result


def _validate(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    action = str(request.get("action", ""))
    if action not in ALLOWED_ACTIONS:
        raise StageError("validate", f"unsupported action: {action}")
    frozen = request.get("frozen_input")
    if not isinstance(frozen, dict):
        raise StageError("validate", "frozen_input must be an object")
    extra = set(frozen) - set(ACTION_FIELDS[action])
    if extra:
        raise StageError("validate", f"unexpected fields: {', '.join(sorted(extra))}")
    username = str(frozen.get("username", ""))
    import re

    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
        raise StageError("validate", "invalid username")
    if username in FORBIDDEN_NAMES:
        raise StageError("validate", f"refusing to manage reserved account {username}")
    keys = frozen.get("public_keys")
    if keys is not None:
        if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys):
            raise StageError("validate", "public_keys must be a list of strings")
        if len(keys) > 20:
            raise StageError("validate", "too many public keys")
        for key in keys:
            parts = key.strip().split()
            if len(parts) < 2 or parts[0] not in PUBLIC_KEY_TYPES:
                raise StageError("validate", "unsupported public key type")
            if any(("=" in option or option.startswith('"')) for option in parts[2:]):
                raise StageError("validate", "authorized_keys options are not accepted")
    allow_existing = frozen.get("allow_existing", False)
    if not isinstance(allow_existing, bool):
        raise StageError("validate", "allow_existing must be a boolean")
    return action, frozen


def _user_exists(username: str) -> bool:
    try:
        result = _run(["getent", "passwd", username], check=False)
    except (OSError, StageError):
        # No getent on this platform (e.g. Windows test host): treat as absent
        # so spool-mode tests can run without privileges.
        return False
    return bool(result.stdout.strip())


def _account_record(username: str) -> Any:
    if pwd is None:
        raise StageError("lookup", "pwd database is unavailable on this host")
    try:
        return pwd.getpwnam(username)
    except KeyError as exc:
        raise StageError("lookup", f"account {username} does not exist") from exc


def _account_groups(username: str) -> set[str]:
    try:
        result = _run(["id", "-nG", username], check=False)
    except (OSError, StageError):
        return set()
    return set(result.stdout.split())


def _is_managed_account(username: str) -> bool:
    """True only for a real human account already in the member group.

    This is deliberately stricter than "the name exists": an unrelated
    pre-existing Linux account must enter the explicit import path, never be
    silently adopted by a normal approval/retry.
    """
    if not _user_exists(username):
        return False
    try:
        record = _account_record(username)
    except StageError:
        return False
    if int(record.pw_uid) < 1000:
        return False
    groups = _account_groups(username)
    if "citespec-members" not in groups:
        return False
    if groups & {"sudo", "docker", "citespec-gpu", "video", "render", "citespec-admins"}:
        return False
    return True


def _scrub_home_keys(username: str, stages: list[str]) -> None:
    """Remove legacy per-user authorized_keys fallbacks.

    sshd no longer reads these files, but leaving stale copies behind would
    make a future configuration mistake silently restore revoked access.
    """
    record = _account_record(username)
    home = Path(record.pw_dir)
    if not home.is_absolute() or home == Path("/"):
        raise StageError("scrub_home_keys", "refusing unsafe home directory")
    ssh_dir = home / ".ssh"
    if not ssh_dir.is_dir() or ssh_dir.is_symlink():
        return
    removed = 0
    for name in ("authorized_keys", "authorized_keys2"):
        path = ssh_dir / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not (path.is_symlink() or info.st_mode & 0o170000 == 0o100000):
            raise StageError("scrub_home_keys", f"{path} is not a regular file")
        path.unlink()
        removed += 1
    stages.append(f"home_keys_removed:{removed}")


def _kill_member_sessions(username: str) -> None:
    """Terminate the member's host-side SSH/login processes, not other users."""
    _run(["pkill", "-KILL", "-u", username], check=False)


def _write_central_keys(username: str, keys: list[str]) -> None:
    """Atomically write the root-managed central key file (0644, root)."""
    CENTRAL_KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CENTRAL_KEY_DIR, 0o755)
    target = CENTRAL_KEY_DIR / username
    content = "".join(f"{key.strip()}\n" for key in keys if key.strip())
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{username}.", dir=CENTRAL_KEY_DIR)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.geteuid() == 0:
            os.chown(temp_name, 0, 0)
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, target)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    if target.read_text(encoding="utf-8") != content:
        raise StageError("central_keys", "central key file failed read-back verification")


def _verify_central_keys(username: str, keys: list[str]) -> None:
    target = CENTRAL_KEY_DIR / username
    expected = "".join(f"{key.strip()}\n" for key in keys if key.strip())
    try:
        actual = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise StageError("verify_central_keys", str(exc)) from exc
    if actual != expected:
        raise StageError("verify_central_keys", "central key file does not match")


def _action_inspect_member(frozen: dict[str, Any], stages: list[str]) -> dict[str, Any]:
    """Read-only facts for the explicit legacy-import dry run.

    Never changes the account, keys, groups, or workspace.
    """
    username = str(frozen["username"])
    if not _user_exists(username):
        stages.append("account_absent")
        return {"exists": False, "username": username}
    record = _account_record(username)
    home = Path(record.pw_dir)
    groups = sorted(_account_groups(username))
    home_keys = [
        str(home / ".ssh" / name)
        for name in ("authorized_keys", "authorized_keys2")
        if (home / ".ssh" / name).exists()
    ]
    central = CENTRAL_KEY_DIR / username
    try:
        central_count = sum(
            1
            for line in central.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except OSError:
        central_count = 0
    stages.append("inspected")
    return {
        "exists": True,
        "username": username,
        "uid": int(record.pw_uid),
        "gid": int(record.pw_gid),
        "home": str(home),
        "home_exists": home.is_dir(),
        "home_is_symlink": home.is_symlink(),
        "shell": str(record.pw_shell),
        "groups": groups,
        "managed": _is_managed_account(username),
        "home_authorized_keys": home_keys,
        "central_key_count": central_count,
    }


def _action_provision_member(frozen: dict[str, Any], stages: list[str]) -> None:
    username = str(frozen["username"])
    keys = [str(key) for key in frozen.get("public_keys", [])]
    if not keys:
        raise StageError("validate", "at least one public key is required")
    quota_gb = int(frozen.get("quota_gb", QUOTA_GB_DEFAULT))
    if not 1 <= quota_gb <= 10000:
        raise StageError("validate", "quota_gb is out of range")
    workspace_root = Path(str(frozen.get("workspace_root", WORKSPACE_ROOT)))
    if workspace_root != WORKSPACE_ROOT or not workspace_root.is_absolute():
        raise StageError("validate", "workspace_root is not the managed workspace root")
    allow_existing = bool(frozen.get("allow_existing", False))

    existed = _user_exists(username)
    if existed and not allow_existing and not _is_managed_account(username):
        raise StageError(
            "account_conflict",
            f"account {username} already exists but is not a managed member; "
            "use the explicit legacy import path",
        )
    if not existed and allow_existing:
        raise StageError("account_conflict", "legacy import target account is absent")
    if not CREATE_USER_SCRIPT.exists():
        raise StageError("create_account", f"missing {CREATE_USER_SCRIPT}")

    ssh_key_args: list[str] = []
    for key in keys:
        ssh_key_args.extend(["--ssh-key-string", key])
    argv = [
        str(CREATE_USER_SCRIPT),
        "--yes",
        "--workspace-root",
        str(WORKSPACE_ROOT),
        "--limit-gb",
        str(quota_gb),
    ]
    argv.extend(ssh_key_args)
    argv.append(username)
    _run(argv)
    stages.append(f"account_{'updated' if existed else 'created'}:{username}")
    if not _is_managed_account(username):
        raise StageError(
            "verify_account",
            f"account {username} is not a restricted managed member account",
        )
    _scrub_home_keys(username, stages)
    _write_central_keys(username, keys)
    _verify_central_keys(username, keys)
    stages.append("central_keys_installed")
    record = _account_record(username)
    stages.append(f"uid:{record.pw_uid}")
    stages.append("verified")


def _action_set_keys(frozen: dict[str, Any], stages: list[str]) -> None:
    username = str(frozen["username"])
    if not _is_managed_account(username):
        raise StageError("lookup", f"account {username} is not a managed member")
    keys = [str(key) for key in frozen.get("public_keys", [])]
    if not keys:
        raise StageError("validate", "at least one public key is required")
    _scrub_home_keys(username, stages)
    _write_central_keys(username, keys)
    _verify_central_keys(username, keys)
    stages.append(f"central_keys_updated:{len(keys)}")


def _action_disable_member(frozen: dict[str, Any], stages: list[str]) -> None:
    username = str(frozen["username"])
    if not _user_exists(username):
        stages.append("account_absent")
        return
    if not _is_managed_account(username):
        raise StageError("lookup", f"account {username} is not a managed member")
    # INV-08: keep the member shell/group semantics and workspace, but remove
    # every login credential and terminate old interactive sessions. Running
    # containers are left alone; emergency stop is a separate explicit action.
    _write_central_keys(username, [])
    _scrub_home_keys(username, stages)
    _run(["usermod", "-L", username])
    _kill_member_sessions(username)
    stages.append("account_locked")
    stages.append("sessions_terminated")


def _action_enable_member(frozen: dict[str, Any], stages: list[str]) -> None:
    username = str(frozen["username"])
    if not _is_managed_account(username):
        raise StageError("lookup", f"account {username} is not a managed member")
    keys = [str(key) for key in frozen.get("public_keys", [])]
    if not keys:
        raise StageError("validate", "at least one public key is required")
    _write_central_keys(username, keys)
    _verify_central_keys(username, keys)
    _scrub_home_keys(username, stages)
    _run(["usermod", "-U", username])
    stages.append("account_unlocked")
    stages.append("central_keys_restored")


ACTIONS = {
    "inspect_member": _action_inspect_member,
    "provision_member": _action_provision_member,
    "set_keys": _action_set_keys,
    "disable_member": _action_disable_member,
    "enable_member": _action_enable_member,
}


def _process_one(request: dict[str, Any]) -> dict[str, Any]:
    operation_id = str(request.get("operation_id", ""))
    try:
        action, frozen = _validate(request)
        stages: list[str] = []
        details = ACTIONS[action](frozen, stages)
        result: dict[str, Any] = {
            "operation_id": operation_id,
            "action": action,
            "status": "succeeded",
            "stages": stages,
        }
        if isinstance(details, dict):
            result.update(details)
        for stage in stages:
            if stage.startswith("uid:"):
                try:
                    result["uid"] = int(stage.split(":", 1)[1])
                except ValueError:
                    pass
        return result
    except StageError as exc:
        return {
            "operation_id": operation_id,
            "status": "failed",
            "stage": exc.stage,
            "error": exc.error,
        }
    except Exception as exc:  # noqa: BLE001 - executor must always answer
        return {
            "operation_id": operation_id,
            "status": "failed",
            "stage": "internal",
            "error": f"{type(exc).__name__}",
        }


def _run_spool(spool: Path) -> int:
    """Process every pending request file; write results next to them.

    Idempotent: a request whose result file already exists is skipped so a
    retried executor pass never re-runs a finished operation.
    """
    requests_dir = spool / "requests"
    results_dir = spool / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    if not requests_dir.is_dir():
        return 0
    for request_file in sorted(requests_dir.glob("*.json")):
        result_file = results_dir / request_file.name
        if result_file.exists():
            continue
        try:
            request = json.loads(request_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        result = _process_one(request)
        temp = result_file.with_suffix(".tmp")
        temp.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
        temp.replace(result_file)
    return 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--spool":
        sys.exit(_run_spool(Path(sys.argv[2])))
    try:
        request = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        print(
            json.dumps(
                {
                    "operation_id": "",
                    "status": "failed",
                    "stage": "validate",
                    "error": "invalid request JSON",
                }
            )
        )
        return 1
    result = _process_one(request)
    print(json.dumps(result))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
