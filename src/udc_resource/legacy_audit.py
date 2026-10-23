"""Run the strict legacy-access audit as a production gate."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LegacyAuditResult:
    passed: bool
    output: str
    details: dict[str, Any] | None = None

    def failure_message(self) -> str:
        if self.details:
            failures = self.details.get("failures")
            if isinstance(failures, list) and failures:
                return "; ".join(str(item) for item in failures[:5])
        return self.output.strip() or "legacy access audit failed"


class LegacyAuditRunner:
    """Invoke the root-owned audit script with a fixed argument list."""

    def __init__(
        self,
        script: str,
        *,
        timeout: int = 120,
        allow_root_ssh_keys: bool = False,
        bash: str = "/bin/bash",
    ) -> None:
        self.script = script
        self.timeout = timeout
        self.allow_root_ssh_keys = allow_root_ssh_keys
        self.bash = bash

    def __call__(self) -> LegacyAuditResult:
        if os.name != "posix":
            return LegacyAuditResult(
                False,
                "legacy access audit is only supported on the Linux server",
            )
        if not os.path.isfile(self.script):
            return LegacyAuditResult(False, f"legacy audit script not found: {self.script}")
        try:
            metadata = os.stat(self.script)
        except OSError as exc:
            return LegacyAuditResult(False, f"cannot stat legacy audit script: {exc}")
        if metadata.st_uid != 0:
            return LegacyAuditResult(
                False,
                f"legacy audit script is not root-owned: {self.script}",
            )
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return LegacyAuditResult(
                False,
                f"legacy audit script is group/world-writable: {self.script}",
            )
        command = [self.bash, self.script, "--production", "--json"]
        if self.allow_root_ssh_keys:
            command.append("--allow-root-ssh-keys")
        environment = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/root",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return LegacyAuditResult(False, f"legacy audit could not run: {exc}")
        details: dict[str, Any] | None = None
        try:
            decoded = json.loads(completed.stdout)
            if isinstance(decoded, dict):
                details = decoded
        except json.JSONDecodeError:
            pass
        passed = completed.returncode == 0 and details is not None
        if details is not None:
            failures = details.get("failures")
            passed = (
                passed
                and details.get("ok") is True
                and (not isinstance(failures, list) or not failures)
            )
        output = (completed.stdout + completed.stderr).strip()
        return LegacyAuditResult(passed, output, details)
