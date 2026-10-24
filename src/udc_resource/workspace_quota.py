"""Workspace project-quota checks used by the active-mode gate."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QuotaCheckResult:
    passed: bool
    detail: str
    limit_bytes: int = 0

    def failure_message(self) -> str:
        return self.detail or "workspace project quota is unavailable"


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_workspace_quota(root: str, limit_gb: int, *, timeout: int = 30) -> QuotaCheckResult:
    """Verify that the workspace filesystem enforces project quotas.

    The Broker cannot enforce a hard per-directory quota from Python. This gate
    therefore proves that the host filesystem is mounted with project-quota
    support and that the quota tools can query it. If that is not true, active
    mode must not be entered.
    """

    target = Path(root)
    if not target.exists():
        return QuotaCheckResult(False, f"workspace root does not exist: {root}")
    findmnt = shutil.which("findmnt")
    if findmnt is None:
        return QuotaCheckResult(False, "findmnt is not installed")
    completed = _run([findmnt, "-no", "TARGET,FSTYPE,OPTIONS", "-T", str(target)], timeout)
    if completed is None or completed.returncode != 0 or not completed.stdout.strip():
        detail = completed.stderr.strip() if completed is not None else "findmnt failed"
        return QuotaCheckResult(False, f"cannot inspect workspace filesystem: {detail}")
    parts = completed.stdout.strip().split(None, 2)
    if len(parts) < 3:
        return QuotaCheckResult(False, f"unexpected findmnt output: {completed.stdout.strip()}")
    mountpoint, fstype, options = parts
    option_set = {item.strip() for item in options.split(",")}
    if fstype == "xfs":
        if not {"prjquota", "pquota"} & option_set:
            return QuotaCheckResult(False, f"{mountpoint} is XFS but not mounted with prjquota")
        xfs_quota = shutil.which("xfs_quota")
        if xfs_quota is None:
            return QuotaCheckResult(False, "xfs_quota is not installed")
        probe = _run([xfs_quota, "-x", "-c", "report -h -p", mountpoint], timeout)
        if probe is None or probe.returncode != 0:
            detail = probe.stderr.strip() if probe is not None else "xfs_quota failed"
            return QuotaCheckResult(False, f"project quota probe failed: {detail}")
    elif fstype == "ext4":
        if not {"prjquota", "pquota"} & option_set:
            return QuotaCheckResult(
                False, f"{mountpoint} is ext4 but not mounted with prjquota/pquota"
            )
        repquota = shutil.which("repquota")
        if repquota is None:
            return QuotaCheckResult(False, "repquota is not installed")
        probe = _run([repquota, "-P", mountpoint], timeout)
        if probe is None or probe.returncode != 0:
            detail = probe.stderr.strip() if probe is not None else "repquota failed"
            return QuotaCheckResult(False, f"project quota probe failed: {detail}")
    else:
        return QuotaCheckResult(False, f"unsupported workspace filesystem for quotas: {fstype}")

    # max_workspace_gb is a per-member project quota, not a limit on the total
    # workspace root. Comparing aggregate usage with it would block every member
    # once the shared filesystem crossed one member's allowance. The gate only
    # proves that the filesystem can enforce project quotas; the quota itself is
    # configured and enforced by the filesystem for each member directory.
    limit_bytes = max(0, limit_gb) * 1024**3
    return QuotaCheckResult(
        True,
        f"{mountpoint} supports project quotas",
        limit_bytes=limit_bytes,
    )
