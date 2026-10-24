"""Continuous GPU permission audit.

The Broker runs this gate at startup, when entering active mode, periodically,
and whenever the set of GPU device nodes changes. A failed audit blocks new
allocations; existing allocations keep running so release and expiry remain
safe.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GpuAuditResult:
    passed: bool
    output: str
    details: dict[str, Any] | None = None
    fingerprint: str = ""

    def failure_message(self) -> str:
        if self.details:
            failures = self.details.get("failures")
            if isinstance(failures, list) and failures:
                return "; ".join(str(item) for item in failures[:5])
        return self.output.strip() or "GPU permission audit failed"


def device_fingerprint(dev_root: str = "/dev") -> str:
    """Fingerprint GPU device nodes without opening or touching them."""

    entries: list[str] = []
    try:
        root = Path(dev_root)
        candidates = sorted(root.glob("nvidia*"))
        # nvidia-caps holds per-capability device nodes that also control GPU
        # access; include them so a permission change triggers a fresh audit.
        candidates.extend(sorted(root.glob("nvidia-caps/nvidia*")))
    except OSError:
        return ""
    for path in candidates:
        try:
            metadata = path.stat()
        except OSError:
            continue
        entries.append(
            ":".join(
                (
                    path.name,
                    str(metadata.st_rdev),
                    str(metadata.st_mode),
                    str(metadata.st_uid),
                    str(metadata.st_gid),
                )
            )
        )
    if not entries:
        return ""
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


class GpuAuditRunner:
    """Invoke the root-owned GPU permission verifier with a fixed argument list."""

    def __init__(
        self,
        script: str,
        *,
        timeout: int = 180,
        expected_gpus: int = 0,
        bash: str = "/bin/bash",
    ) -> None:
        self.script = script
        self.timeout = timeout
        self.expected_gpus = expected_gpus
        self.bash = bash

    def __call__(self) -> GpuAuditResult:
        fingerprint = device_fingerprint()
        if os.name != "posix":
            return GpuAuditResult(
                False,
                "GPU permission audit is only supported on the Linux server",
                fingerprint=fingerprint,
            )
        if not os.path.isfile(self.script):
            return GpuAuditResult(
                False,
                f"GPU audit script not found: {self.script}",
                fingerprint=fingerprint,
            )
        try:
            metadata = os.stat(self.script)
        except OSError as exc:
            return GpuAuditResult(
                False,
                f"cannot stat GPU audit script: {exc}",
                fingerprint=fingerprint,
            )
        if metadata.st_uid != 0:
            return GpuAuditResult(
                False,
                f"GPU audit script is not root-owned: {self.script}",
                fingerprint=fingerprint,
            )
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return GpuAuditResult(
                False,
                f"GPU audit script is group/world-writable: {self.script}",
                fingerprint=fingerprint,
            )
        command = [self.bash, self.script, "--production", "--json"]
        if self.expected_gpus > 0:
            command.extend(["--expected-gpus", str(self.expected_gpus)])
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
            return GpuAuditResult(
                False,
                f"GPU audit could not run: {exc}",
                fingerprint=fingerprint,
            )
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
        return GpuAuditResult(passed, output, details, fingerprint)
