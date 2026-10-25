"""Fixed protocol between the broker and the narrow provisioning executor.

Decision record (goal §8.2 — why the broker's privileges are not widened):

The broker's systemd unit keeps ``ProtectSystem=strict`` and read-only HOME;
being root does NOT mean it may edit /etc/passwd, member home directories,
or SSH key files. Instead the broker submits a *frozen, validated* operation
record to a separate root-owned executor (systemd path/oneshot unit watching
a spool directory, or a fixed command in isolated test environments). The
executor:

- accepts only the fixed action names and field whitelist defined here;
- never receives shell strings, arbitrary paths, UIDs, groups, or unit names;
- refuses to touch root, system users, recovery admins, or unmanaged accounts;
- marks the operation successful only after the safety steps verify;
- is idempotent per operation (re-running a finished request is a no-op).

The broker polls result files; a crash anywhere converges on the next retry
because every action re-reads the real system state before reporting.
"""

from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

ALLOWED_ACTIONS = ("provision_member", "set_keys", "disable_member")

# Field whitelist per action: anything else in the frozen input is rejected
# by both the broker client and the executor (defense in depth).
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


def validate_request(request: dict[str, Any]) -> dict[str, Any]:
    """Whitelist-validate one executor request (fail closed, no echoes)."""
    action = str(request.get("action", ""))
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unsupported provisioning action: {action}")
    fields = ACTION_FIELDS[action]
    frozen = request.get("frozen_input")
    if not isinstance(frozen, dict):
        raise ValueError("frozen_input must be an object")
    extra = set(frozen) - set(fields)
    if extra:
        raise ValueError(f"unexpected fields for {action}: {', '.join(sorted(extra))}")
    username = str(frozen.get("username", ""))
    if action in {"provision_member", "set_keys", "disable_member"}:
        import re

        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
            raise ValueError("invalid username for provisioning")
        if username in {"root", "daemon", "bin", "sys", "sshd", "nobody"}:
            raise ValueError("refusing to provision a system account")
    keys = frozen.get("public_keys")
    if keys is not None:
        if not isinstance(keys, list) or any(
            not isinstance(key, str) or not key.strip() for key in keys
        ):
            raise ValueError("public_keys must be a list of non-empty strings")
        if len(keys) > 20:
            raise ValueError("too many public keys")
    return request


class ProvisioningError(RuntimeError):
    """Raised when the executor reports a failed stage."""


class ProvisioningClient(ABC):
    """Submit one validated operation and wait for its result."""

    @abstractmethod
    def execute(self, action: str, frozen_input: dict[str, Any]) -> dict[str, Any]:
        """Run the operation; returns the executor result record.

        Raises ProvisioningError with the executor's stage/error on failure.
        """


class SpoolProvisioningClient(ProvisioningClient):
    """File-based hand-off for the systemd path-unit deployment."""

    def __init__(self, spool_dir: str | Path, *, timeout_seconds: float = 300.0) -> None:
        self.spool_dir = Path(spool_dir)
        self.timeout_seconds = timeout_seconds

    def execute(self, action: str, frozen_input: dict[str, Any]) -> dict[str, Any]:
        import time

        operation_id = uuid.uuid4().hex
        request = {
            "operation_id": operation_id,
            "action": action,
            "frozen_input": frozen_input,
        }
        validate_request(request)
        requests_dir = self.spool_dir / "requests"
        results_dir = self.spool_dir / "results"
        requests_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)
        request_file = requests_dir / f"{operation_id}.json"
        temp = request_file.with_suffix(".tmp")
        temp.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
        os.replace(temp, request_file)
        result_file = results_dir / f"{operation_id}.json"
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if result_file.exists():
                try:
                    result = json.loads(result_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(0.2)
                    continue
                if result.get("status") == "succeeded":
                    return result
                raise ProvisioningError(
                    f"stage={result.get('stage', '')}: {result.get('error', 'unknown')}"
                )
            time.sleep(0.25)
        raise ProvisioningError("provisioning executor did not answer in time")


class CommandExecutorClient(ProvisioningClient):
    """Run a fixed executor command (isolated test environments only).

    The command receives the request JSON on stdin and answers with the
    result JSON on stdout. Used where no spool/systemd deployment exists.
    """

    def __init__(self, command: list[str], *, timeout_seconds: float = 120.0) -> None:
        self.command = list(command)
        self.timeout_seconds = timeout_seconds

    def execute(self, action: str, frozen_input: dict[str, Any]) -> dict[str, Any]:
        import subprocess

        request = {"operation_id": uuid.uuid4().hex, "action": action, "frozen_input": frozen_input}
        validate_request(request)
        try:
            completed = subprocess.run(
                self.command,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProvisioningError(f"executor did not run: {exc}") from exc
        try:
            result = json.loads(completed.stdout)
        except ValueError as exc:
            raise ProvisioningError(
                f"executor returned invalid JSON (exit {completed.returncode})"
            ) from exc
        if completed.returncode != 0 or result.get("status") != "succeeded":
            raise ProvisioningError(
                f"stage={result.get('stage', '')}: {result.get('error', 'unknown')}"
            )
        return result
