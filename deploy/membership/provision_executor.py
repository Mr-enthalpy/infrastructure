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
  (/etc/udc-resource/authorized-keys.d/<username>) that sshd consumes via
  AuthorizedKeysCommand/Include — members cannot rebuild revoked entries;
- idempotent: re-running a finished request re-verifies and reports success.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

CREATE_USER_SCRIPT = Path("/usr/local/libexec/udc-resource/create_user.sh")
CENTRAL_KEY_DIR = Path("/etc/udc-resource/authorized-keys.d")
WORKSPACE_ROOT = Path("/data1/udc-users")
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

ALLOWED_ACTIONS = ("provision_member", "set_keys", "disable_member")
ACTION_FIELDS: dict[str, tuple[str, ...]] = {
    "provision_member": (
        "username",
        "display_name",
        "public_keys",
        "workspace_root",
        "quota_gb",
        "valid_until",
    ),
    "set_keys": ("username", "public_keys"),
    "disable_member": ("username", "reason"),
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
    if keys is not None and (
        not isinstance(keys, list) or any(not isinstance(k, str) for k in keys)
    ):
        raise StageError("validate", "public_keys must be a list of strings")
    return action, frozen


def _user_exists(username: str) -> bool:
    result = _run(["getent", "passwd", username], check=False)
    return bool(result.stdout.strip())


def _write_central_keys(username: str, keys: list[str]) -> None:
    """Write the root-managed central key file for one member (0644, root)."""
    target = CENTRAL_KEY_DIR / username
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{key.strip()}\n" for key in keys if key.strip())
    temp = target.with_suffix(".tmp")
    temp.write_text(content, encoding="utf-8")
    _run(["chmod", "0644", str(temp)])
    _run(["chown", "root:root", str(temp)])
    import os

    os.replace(temp, target)


def _action_provision_member(frozen: dict[str, Any], stages: list[str]) -> None:
    username = str(frozen["username"])
    keys = [str(key) for key in frozen.get("public_keys", [])]
    quota_gb = int(frozen.get("quota_gb", QUOTA_GB_DEFAULT))
    ssh_key_args: list[str] = []
    for key in keys:
        ssh_key_args.extend(["--ssh-key-string", key])
    if not _user_exists(username):
        argv = [str(CREATE_USER_SCRIPT), "--yes", "--limit-gb", str(quota_gb)]
        argv.extend(ssh_key_args)
        argv.append(username)
        if not CREATE_USER_SCRIPT.exists():
            raise StageError("create_account", f"missing {CREATE_USER_SCRIPT}")
        _run(argv)
        stages.append(f"account_created:{username}")
    else:
        stages.append(f"account_existed:{username}")
    _write_central_keys(username, keys)
    stages.append("central_keys_installed")
    if not _user_exists(username):
        raise StageError("verify_account", "account disappeared after provisioning")
    import pwd

    stages.append(f"uid:{pwd.getpwnam(username).pw_uid}")
    stages.append("verified")


def _action_set_keys(frozen: dict[str, Any], stages: list[str]) -> None:
    username = str(frozen["username"])
    if not _user_exists(username):
        raise StageError("lookup", f"account {username} does not exist")
    keys = [str(key) for key in frozen.get("public_keys", [])]
    _write_central_keys(username, keys)
    stages.append(f"central_keys_updated:{len(keys)}")


def _action_disable_member(frozen: dict[str, Any], stages: list[str]) -> None:
    username = str(frozen["username"])
    if not _user_exists(username):
        stages.append("account_absent")
        return
    # INV-08: keep the member shell and group semantics; revoke the keys and
    # lock the password so SSH login cannot re-authenticate.
    _write_central_keys(username, [])
    _run(["usermod", "-L", username])
    stages.append("account_locked")


ACTIONS = {
    "provision_member": _action_provision_member,
    "set_keys": _action_set_keys,
    "disable_member": _action_disable_member,
}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        operation_id = str(request.get("operation_id", ""))
        action, frozen = _validate(request)
        stages: list[str] = []
        ACTIONS[action](frozen, stages)
        result = {
            "operation_id": operation_id,
            "action": action,
            "status": "succeeded",
            "stages": stages,
        }
    except StageError as exc:
        print(
            json.dumps(
                {
                    "operation_id": str(request.get("operation_id", "")),
                    "status": "failed",
                    "stage": exc.stage,
                    "error": exc.error,
                }
            )
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - executor must always answer
        print(
            json.dumps(
                {
                    "operation_id": str(request.get("operation_id", "")),
                    "status": "failed",
                    "stage": "internal",
                    "error": f"{type(exc).__name__}",
                }
            )
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
