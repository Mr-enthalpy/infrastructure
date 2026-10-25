from __future__ import annotations

import os
import subprocess

import pytest

from udc_resource import workspace_quota


def _which(name: str) -> str:
    return f"/usr/sbin/{name}"


def test_xfs_project_quota_support_passes_without_walking_usage(monkeypatch, tmp_path):
    monkeypatch.setattr(
        workspace_quota.shutil,
        "which",
        lambda name: _which(name) if name in {"findmnt", "xfs_quota"} else None,
    )

    def fake_run(command, timeout):
        if command[0].endswith("findmnt"):
            return subprocess.CompletedProcess(
                command,
                0,
                f"{tmp_path} xfs rw,relatime,prjquota\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(workspace_quota, "_run", fake_run)
    result = workspace_quota.check_workspace_quota(str(tmp_path), 200)
    assert result.passed is True
    assert "supports project quotas" in result.detail
    assert result.limit_bytes == 200 * 1024**3


def test_xfs_without_project_quota_mount_option_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        workspace_quota.shutil,
        "which",
        lambda name: _which(name) if name == "findmnt" else None,
    )
    monkeypatch.setattr(
        workspace_quota,
        "_run",
        lambda command, timeout: subprocess.CompletedProcess(
            command,
            0,
            f"{tmp_path} xfs rw,relatime\n",
            "",
        ),
    )
    result = workspace_quota.check_workspace_quota(str(tmp_path), 200)
    assert result.passed is False
    assert "prjquota" in result.detail


def test_unsupported_filesystem_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        workspace_quota.shutil,
        "which",
        lambda name: _which(name) if name == "findmnt" else None,
    )
    monkeypatch.setattr(
        workspace_quota,
        "_run",
        lambda command, timeout: subprocess.CompletedProcess(
            command,
            0,
            f"{tmp_path} overlay rw\n",
            "",
        ),
    )
    result = workspace_quota.check_workspace_quota(str(tmp_path), 200)
    assert result.passed is False
    assert "unsupported" in result.detail


def _member_quota_env(
    monkeypatch,
    tmp_path,
    *,
    fstype: str = "xfs",
    report: str = "",
    lsattr_rows: dict[str, str] | None = None,
    tools: tuple[str, ...] = ("findmnt", "lsattr", "xfs_quota"),
):
    lsattr_rows = lsattr_rows or {}

    def which(name: str):
        return f"/usr/sbin/{name}" if name in tools else None

    monkeypatch.setattr(workspace_quota.shutil, "which", which)

    def fake_run(command, timeout):
        if command[0].endswith("findmnt"):
            return subprocess.CompletedProcess(
                command,
                0,
                f"{tmp_path} {fstype} rw,relatime,prjquota\n",
                "",
            )
        if command[0].endswith("lsattr"):
            target = command[-1]
            row = lsattr_rows.get(target)
            if row is None:
                return subprocess.CompletedProcess(command, 1, "", "lsattr: no such file")
            return subprocess.CompletedProcess(command, 0, f"{row} {target}\n", "")
        if command[0].endswith("xfs_quota"):
            if "report" in " ".join(command):
                return subprocess.CompletedProcess(command, 0, report, "")
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(workspace_quota, "_run", fake_run)


def test_verify_member_quota_passes_on_xfs(monkeypatch, tmp_path):
    workspace = tmp_path / "alice"
    workspace.mkdir()
    limit_kib = 200 * 1024 * 1024
    _member_quota_env(
        monkeypatch,
        tmp_path,
        report=f"ProjectID       Used      Soft      Hard      Realtime\n"
        f"----------------------------------------------------------\n"
        f"#0              0         0         0         0\n"
        f"1000            4         0         {limit_kib}      00 [--------]\n",
        lsattr_rows={
            str(workspace): "1000 -P--------------e------",
            # The probe file is created during verification.
            str(workspace / ".udc-quota-probe"): "1000 --------------e------",
        },
    )
    result = workspace_quota.verify_member_quota(workspace, 1000, 200)
    assert result.passed is True
    assert result.project_id == 1000
    assert result.hard_limit_bytes == 200 * 1024**3


def test_verify_member_quota_fails_when_project_missing(monkeypatch, tmp_path):
    workspace = tmp_path / "alice"
    workspace.mkdir()
    _member_quota_env(
        monkeypatch,
        tmp_path,
        report="#0              0         0         0         0\n",
        lsattr_rows={str(workspace): "0 --------------e------"},
    )
    result = workspace_quota.verify_member_quota(workspace, 1000, 200)
    assert result.passed is False
    assert "expected 1000" in result.detail


def test_verify_member_quota_fails_on_wrong_hard_limit(monkeypatch, tmp_path):
    workspace = tmp_path / "alice"
    workspace.mkdir()
    _member_quota_env(
        monkeypatch,
        tmp_path,
        report=("1000            4         0         1024      00 [--------]\n"),
        lsattr_rows={
            str(workspace): "1000 -P--------------e------",
            str(workspace / ".udc-quota-probe"): "1000 --------------e------",
        },
    )
    result = workspace_quota.verify_member_quota(workspace, 1000, 200)
    assert result.passed is False
    assert "hard limit" in result.detail
    assert "configure_quotas.sh" in result.detail


def test_verify_member_quota_fails_when_new_files_do_not_inherit(monkeypatch, tmp_path):
    workspace = tmp_path / "alice"
    workspace.mkdir()
    limit_kib = 200 * 1024 * 1024
    _member_quota_env(
        monkeypatch,
        tmp_path,
        report=f"1000            4         0         {limit_kib}      00 [--------]\n",
        lsattr_rows={
            str(workspace): "1000 -P--------------e------",
            str(workspace / ".udc-quota-probe"): "0 --------------e------",
        },
    )
    result = workspace_quota.verify_member_quota(workspace, 1000, 200)
    assert result.passed is False
    assert "inherit" in result.detail
    assert not (workspace / ".udc-quota-probe").exists()


@pytest.mark.skipif(os.name != "posix", reason="symlink creation needs privileges on Windows")
def test_verify_member_quota_rejects_symlink(monkeypatch, tmp_path):
    workspace = tmp_path / "alice"
    workspace.mkdir()
    link = tmp_path / "alice-link"
    link.symlink_to(workspace, target_is_directory=True)
    result = workspace_quota.verify_member_quota(link, 1000, 200)
    assert result.passed is False
    assert "not a real directory" in result.detail


def test_verify_member_quota_ext4_via_repquota(monkeypatch, tmp_path):
    workspace = tmp_path / "alice"
    workspace.mkdir()
    hard_kib = 200 * 1024 * 1024
    soft_kib = 180 * 1024 * 1024
    tools = ("findmnt", "lsattr", "repquota")

    def which(name: str):
        return f"/usr/sbin/{name}" if name in tools else None

    monkeypatch.setattr(workspace_quota.shutil, "which", which)

    def fake_run(command, timeout):
        if command[0].endswith("findmnt"):
            return subprocess.CompletedProcess(
                command,
                0,
                f"{tmp_path} ext4 rw,relatime,prjquota\n",
                "",
            )
        if command[0].endswith("lsattr"):
            target = command[-1]
            if target == str(workspace):
                return subprocess.CompletedProcess(command, 0, f"1000 -P- {target}\n", "")
            if target == str(workspace / ".udc-quota-probe"):
                return subprocess.CompletedProcess(command, 0, f"1000 -- {target}\n", "")
            return subprocess.CompletedProcess(command, 1, "", "no such file")
        if command[0].endswith("repquota"):
            return subprocess.CompletedProcess(
                command,
                0,
                f"#1000 -- 1000 {soft_kib} {hard_kib} 10 0 0\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(workspace_quota, "_run", fake_run)
    result = workspace_quota.verify_member_quota(workspace, 1000, 200)
    assert result.passed is True
    assert result.hard_limit_bytes == 200 * 1024**3


def test_provision_member_quota_xfs_runs_project_and_limit(monkeypatch, tmp_path):
    workspace = tmp_path / "alice"
    workspace.mkdir()
    limit_kib = 200 * 1024 * 1024
    executed: list[list[str]] = []

    def which(name: str):
        return f"/usr/sbin/{name}" if name in {"findmnt", "lsattr", "xfs_quota"} else None

    monkeypatch.setattr(workspace_quota.shutil, "which", which)

    def fake_run(command, timeout):
        executed.append(command)
        if command[0].endswith("findmnt"):
            return subprocess.CompletedProcess(
                command,
                0,
                f"{tmp_path} xfs rw,relatime,prjquota\n",
                "",
            )
        if command[0].endswith("lsattr"):
            target = command[-1]
            return subprocess.CompletedProcess(command, 0, f"1000 -P- {target}\n", "")
        if command[0].endswith("xfs_quota"):
            if "report" in " ".join(command):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    f"1000            0         0         {limit_kib}      00 [--------]\n",
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(workspace_quota, "_run", fake_run)
    result = workspace_quota.provision_member_quota(workspace, 1000, 200)
    assert result.passed is True
    joined = [" ".join(command) for command in executed]
    assert any("project -s -p" in text and str(workspace) in text for text in joined)
    assert any("limit -p bsoft=100g bhard=200g 1000" in text for text in joined)


def test_provision_member_quota_fails_when_verification_still_fails(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "alice"
    workspace.mkdir()

    def which(name: str):
        return f"/usr/sbin/{name}" if name in {"findmnt", "lsattr", "xfs_quota"} else None

    monkeypatch.setattr(workspace_quota.shutil, "which", which)

    def fake_run(command, timeout):
        if command[0].endswith("findmnt"):
            return subprocess.CompletedProcess(
                command,
                0,
                f"{tmp_path} xfs rw,relatime,prjquota\n",
                "",
            )
        if command[0].endswith("lsattr"):
            target = command[-1]
            return subprocess.CompletedProcess(command, 0, f"0 ---- {target}\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(workspace_quota, "_run", fake_run)
    result = workspace_quota.provision_member_quota(workspace, 1000, 200)
    assert result.passed is False
    assert "did not verify" in result.detail


def test_provision_member_quota_ext4(monkeypatch, tmp_path):
    workspace = tmp_path / "alice"
    workspace.mkdir()
    hard_kib = 200 * 1024 * 1024
    executed: list[list[str]] = []
    tools = ("findmnt", "lsattr", "chattr", "find", "setquota", "repquota")

    def which(name: str):
        return f"/usr/sbin/{name}" if name in tools else None

    monkeypatch.setattr(workspace_quota.shutil, "which", which)

    def fake_run(command, timeout):
        executed.append(command)
        if command[0].endswith("findmnt"):
            return subprocess.CompletedProcess(
                command,
                0,
                f"{tmp_path} ext4 rw,relatime,prjquota\n",
                "",
            )
        if command[0].endswith("lsattr"):
            target = command[-1]
            return subprocess.CompletedProcess(command, 0, f"1000 -P- {target}\n", "")
        if command[0].endswith("repquota"):
            return subprocess.CompletedProcess(
                command,
                0,
                f"#1000 -- 0 0 {hard_kib} 0 0 0\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(workspace_quota, "_run", fake_run)
    result = workspace_quota.provision_member_quota(workspace, 1000, 200)
    assert result.passed is True
    joined = [" ".join(command) for command in executed]
    assert any("chattr -R -p 1000" in text for text in joined)
    assert any("setquota -P 1000" in text and str(hard_kib) in text for text in joined)
