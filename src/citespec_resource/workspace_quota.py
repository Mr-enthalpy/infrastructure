"""Workspace project-quota checks used by the active-mode gate."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Multipliers accepted when parsing human-readable xfs_quota output.
_HUMAN_UNITS = {
    "": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    "P": 1024**5,
}
# Candidate byte sizes of one reported "block" in xfs_quota report output.
_XFS_BLOCK_UNITS = (1024, 4096, 512, 1024**2)


@dataclass(frozen=True)
class QuotaCheckResult:
    passed: bool
    detail: str
    limit_bytes: int = 0

    def failure_message(self) -> str:
        return self.detail or "workspace project quota is unavailable"


@dataclass(frozen=True)
class MemberQuotaResult:
    """Result of provisioning or verifying one member workspace quota."""

    passed: bool
    detail: str
    project_id: int | None = None
    hard_limit_bytes: int = 0

    def failure_message(self) -> str:
        return self.detail or "member workspace quota is not enforced"


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


def _mount_for_quota(target: Path, timeout: int) -> tuple[str, str, str] | None:
    """Resolve the real filesystem mount that backs ``target``.

    The broker runs under ProtectSystem=strict with ReadWritePaths on the
    workspace root.  systemd implements that as a bind mount at the workspace
    path, so ``findmnt -T workspace`` reports the bind target instead of the
    ext4/XFS mountpoint.  repquota/xfs_quota then fail with "mountpoint not
    found".  Resolve through the underlying SOURCE device and select the
    shortest candidate that contains the requested path.
    """

    findmnt = shutil.which("findmnt")
    if findmnt is None:
        return None
    source_probe = _run([findmnt, "-no", "SOURCE", "-T", str(target)], timeout)
    if source_probe is None or source_probe.returncode != 0:
        return None
    sources = [line.strip() for line in source_probe.stdout.splitlines() if line.strip()]
    if not sources:
        return None
    # findmnt renders a bind mount whose root is a subdirectory as
    # "/dev/sdb1[/citespec-users]"; findmnt -S needs the bare device.
    source = sources[0].split("[", 1)[0].strip()
    # A bind mount has the same SOURCE as its underlying filesystem.  Ask for
    # every mount of that source and pick the shortest path that is an
    # ancestor of the requested target.
    mounts = _run([findmnt, "-no", "TARGET,FSTYPE,OPTIONS", "-S", source], timeout)
    if mounts is None or mounts.returncode != 0:
        return None
    target_str = str(target)
    candidates: list[tuple[str, str, str]] = []
    for line in mounts.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        mountpoint, fstype, options = parts
        target_path = Path(target_str)
        mount_path = Path(mountpoint)
        if target_path == mount_path or target_path.is_relative_to(mount_path):
            candidates.append((mountpoint, fstype, options))
    if not candidates:
        return None
    quota_candidates = [
        item
        for item in candidates
        if {"prjquota", "pquota"} & {option.strip() for option in item[2].split(",")}
    ]
    return min(quota_candidates or candidates, key=lambda item: len(item[0].rstrip("/")))


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
    mounted = _mount_for_quota(target, timeout)
    if mounted is None:
        return QuotaCheckResult(False, f"cannot resolve underlying quota mount for {root}")
    mountpoint, fstype, options = mounted
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


def _workspace_mount(target: Path, *, timeout: int) -> tuple[str, str, str] | MemberQuotaResult:
    """Return (mountpoint, fstype, options) or a failure result."""

    mounted = _mount_for_quota(target, timeout)
    if mounted is None:
        return MemberQuotaResult(
            False,
            f"cannot resolve underlying quota mount for {target}",
        )
    return mounted


def _parse_human_bytes(token: str) -> int | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGTPE]?)", token.strip())
    if match is None:
        return None
    value = float(match.group(1))
    return int(value * _HUMAN_UNITS[match.group(2)])


def _lsattr_project_id(path: Path, *, timeout: int) -> tuple[int | None, str]:
    """Return the project ID (and raw attribute string) of a path via lsattr."""

    lsattr = shutil.which("lsattr")
    if lsattr is None:
        return None, "lsattr is not installed"
    completed = _run([lsattr, "-p", "-d", str(path)], timeout)
    if completed is None or completed.returncode != 0 or not completed.stdout.strip():
        detail = completed.stderr.strip() if completed is not None else "lsattr failed"
        return None, detail
    fields = completed.stdout.strip().split(None, 2)
    if len(fields) < 2:
        return None, f"unexpected lsattr output: {completed.stdout.strip()}"
    try:
        return int(fields[0]), fields[1] if len(fields) > 1 else ""
    except ValueError:
        return None, f"unexpected lsattr output: {completed.stdout.strip()}"


def _verify_inheritance(path: Path, uid: int, *, timeout: int) -> str | None:
    """Prove new files actually inherit the project ID by probing one."""

    probe_path = path / ".citespec-quota-probe"
    try:
        fd = os.open(probe_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except OSError as exc:
        return f"cannot create quota inheritance probe file: {exc}"
    try:
        project_id, detail = _lsattr_project_id(probe_path, timeout=timeout)
        if project_id is None:
            return f"cannot read probe file project ID: {detail}"
        if project_id != uid:
            return (
                f"new files do not inherit project ID {uid} "
                f"(probe file has project ID {project_id})"
            )
    finally:
        try:
            os.unlink(probe_path)
        except OSError:
            logger.warning("could not remove quota probe file %s", probe_path)
    return None


def _verify_xfs_hard_limit(
    mountpoint: str,
    uid: int,
    limit_gb: int,
    *,
    timeout: int,
) -> tuple[int, str] | MemberQuotaResult:
    xfs_quota = shutil.which("xfs_quota")
    if xfs_quota is None:
        return MemberQuotaResult(False, "xfs_quota is not installed")
    completed = _run(
        [xfs_quota, "-x", "-c", "report -p -b", mountpoint],
        timeout,
    )
    if completed is None or completed.returncode != 0:
        detail = completed.stderr.strip() if completed is not None else "xfs_quota failed"
        return MemberQuotaResult(False, f"cannot read project quota report: {detail}")
    expected = limit_gb * 1024**3
    key = str(uid)
    row: list[str] | None = None
    for line in completed.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        label = fields[0].lstrip("#")
        if label == key:
            row = fields
            break
    if row is None:
        return MemberQuotaResult(
            False,
            f"project ID {uid} is missing from the XFS project quota report; "
            "run deploy/configure_quotas.sh for this member",
        )
    values: list[int] = []
    for token in row[1:]:
        if token.isdigit():
            values.append(int(token))
            continue
        parsed = _parse_human_bytes(token)
        if parsed is not None:
            values.append(parsed)
    hard = values[2] if len(values) >= 3 else 0
    for unit in _XFS_BLOCK_UNITS:
        if hard and hard * unit == expected:
            return hard * unit, "ok"
    return MemberQuotaResult(
        False,
        f"XFS project {uid} hard limit is {hard} blocks, expected {limit_gb} GiB; "
        "run deploy/configure_quotas.sh for this member",
    )


def _verify_ext4_hard_limit(
    mountpoint: str,
    uid: int,
    limit_gb: int,
    *,
    timeout: int,
) -> tuple[int, str] | MemberQuotaResult:
    repquota = shutil.which("repquota")
    if repquota is None:
        return MemberQuotaResult(False, "repquota is not installed")
    completed = _run([repquota, "-P", mountpoint], timeout)
    if completed is None or completed.returncode != 0:
        detail = completed.stderr.strip() if completed is not None else "repquota failed"
        return MemberQuotaResult(False, f"cannot read project quota report: {detail}")
    expected_kib = limit_gb * 1024 * 1024
    key = f"#{uid}"
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] != key:
            continue
        integers = [token for token in fields[2:] if token.isdigit()]
        if len(integers) < 3:
            continue
        _used, soft, hard = (int(token) for token in integers[:3])
        # The soft limit is advisory (configure_quotas.sh defaults to 90% of
        # the hard limit); the hard limit is the security property.
        if hard == expected_kib and 0 <= soft < hard:
            return hard * 1024, "ok"
        return MemberQuotaResult(
            False,
            f"ext4 project #{uid} hard limit is {hard} KiB, expected {limit_gb} GiB; "
            "run deploy/configure_quotas.sh for this member",
        )
    return MemberQuotaResult(
        False,
        f"project ID {uid} is missing from the ext4 project quota report; "
        "run deploy/configure_quotas.sh for this member",
    )


def verify_member_quota(
    workspace: str | Path,
    uid: int,
    limit_gb: int,
    *,
    timeout: int = 30,
) -> MemberQuotaResult:
    """Verify that one member directory actually carries its project quota.

    Unlike :func:`check_workspace_quota` — which only proves the filesystem is
    *capable* of project quotas — this inspects the member directory itself:
    its project ID, project inheritance for new files, and the hard limit as
    reported by the filesystem. It is the per-allocation gate required so a
    newly created workspace can never slip through unbounded.
    """

    if uid <= 0:
        return MemberQuotaResult(False, "member UID must be positive for project quotas")
    if limit_gb <= 0:
        return MemberQuotaResult(False, "limit_gb must be positive")
    path = Path(workspace)
    if path.is_symlink() or not path.is_dir():
        return MemberQuotaResult(False, f"workspace path is not a real directory: {path}")
    mounted = _workspace_mount(path, timeout=timeout)
    if isinstance(mounted, MemberQuotaResult):
        return mounted
    mountpoint, fstype, options = mounted
    option_set = {item.strip() for item in options.split(",")}
    if not {"prjquota", "pquota"} & option_set:
        return MemberQuotaResult(
            False,
            f"{mountpoint} is not mounted with prjquota/pquota",
        )
    if fstype not in {"xfs", "ext4", "ext3", "ext2"}:
        return MemberQuotaResult(
            False,
            f"unsupported workspace filesystem for quotas: {fstype}",
        )
    project_id, detail = _lsattr_project_id(path, timeout=timeout)
    if project_id is None:
        return MemberQuotaResult(False, f"cannot read workspace project ID: {detail}")
    if project_id != uid:
        return MemberQuotaResult(
            False,
            f"workspace project ID is {project_id}, expected {uid}; "
            "run deploy/configure_quotas.sh for this member",
        )
    inheritance_failure = _verify_inheritance(path, uid, timeout=timeout)
    if inheritance_failure:
        return MemberQuotaResult(False, inheritance_failure, project_id=project_id)
    if fstype == "xfs":
        verified = _verify_xfs_hard_limit(mountpoint, uid, limit_gb, timeout=timeout)
    else:
        verified = _verify_ext4_hard_limit(mountpoint, uid, limit_gb, timeout=timeout)
    if isinstance(verified, MemberQuotaResult):
        return verified
    hard_bytes, _detail = verified
    return MemberQuotaResult(
        True,
        f"{mountpoint} enforces a {limit_gb} GiB hard limit on project {uid}",
        project_id=project_id,
        hard_limit_bytes=hard_bytes,
    )


def provision_member_quota(
    workspace: str | Path,
    uid: int,
    limit_gb: int,
    *,
    timeout: int = 300,
) -> MemberQuotaResult:
    """Assign the project ID and hard limit for one member directory.

    Idempotent. Uses the same project-ID convention as
    ``deploy/configure_quotas.sh`` (project ID = member UID). Returns the
    verification result so callers never trust an unverified apply.
    """

    if uid <= 0:
        return MemberQuotaResult(False, "member UID must be positive for project quotas")
    if limit_gb <= 0:
        return MemberQuotaResult(False, "limit_gb must be positive")
    path = Path(workspace)
    if path.is_symlink() or not path.is_dir():
        return MemberQuotaResult(False, f"workspace path is not a real directory: {path}")
    mounted = _workspace_mount(path, timeout=timeout)
    if isinstance(mounted, MemberQuotaResult):
        return mounted
    mountpoint, fstype, _options = mounted
    soft_gb = max(1, limit_gb // 2)
    if fstype == "xfs":
        xfs_quota = shutil.which("xfs_quota")
        if xfs_quota is None:
            return MemberQuotaResult(False, "xfs_quota is not installed")
        completed = _run(
            [
                xfs_quota,
                "-x",
                "-c",
                f"project -s -p {path} {uid}",
                mountpoint,
            ],
            timeout,
        )
        if completed is None or completed.returncode != 0:
            detail = completed.stderr.strip() if completed is not None else "xfs_quota failed"
            return MemberQuotaResult(False, f"could not assign project ID: {detail}")
        completed = _run(
            [
                xfs_quota,
                "-x",
                "-c",
                f"limit -p bsoft={soft_gb}g bhard={limit_gb}g {uid}",
                mountpoint,
            ],
            timeout,
        )
        if completed is None or completed.returncode != 0:
            detail = completed.stderr.strip() if completed is not None else "xfs_quota failed"
            return MemberQuotaResult(False, f"could not set project quota: {detail}")
    elif fstype in {"ext4", "ext3", "ext2"}:
        chattr = shutil.which("chattr")
        find = shutil.which("find")
        setquota = shutil.which("setquota")
        if chattr is None or find is None or setquota is None:
            return MemberQuotaResult(False, "chattr, find, and setquota are required")
        completed = _run([chattr, "-R", "-p", str(uid), str(path)], timeout)
        if completed is None or completed.returncode != 0:
            detail = completed.stderr.strip() if completed is not None else "chattr failed"
            return MemberQuotaResult(False, f"could not assign project ID: {detail}")
        completed = _run(
            [find, str(path), "-type", "d", "-exec", chattr, "+P", "{}", "+"],
            timeout,
        )
        if completed is None or completed.returncode != 0:
            detail = completed.stderr.strip() if completed is not None else "find failed"
            return MemberQuotaResult(False, f"could not enable project inheritance: {detail}")
        soft_kib = soft_gb * 1024 * 1024
        hard_kib = limit_gb * 1024 * 1024
        completed = _run(
            [setquota, "-P", str(uid), str(soft_kib), str(hard_kib), "0", "0", mountpoint],
            timeout,
        )
        if completed is None or completed.returncode != 0:
            detail = completed.stderr.strip() if completed is not None else "setquota failed"
            return MemberQuotaResult(False, f"could not set project quota: {detail}")
    else:
        return MemberQuotaResult(
            False,
            f"unsupported workspace filesystem for quotas: {fstype}",
        )
    verified = verify_member_quota(path, uid, limit_gb, timeout=timeout)
    if verified.passed:
        return verified
    return MemberQuotaResult(
        False,
        f"quota provisioning did not verify: {verified.detail}",
        project_id=verified.project_id,
        hard_limit_bytes=verified.hard_limit_bytes,
    )
