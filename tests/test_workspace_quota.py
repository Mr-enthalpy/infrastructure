from __future__ import annotations

import subprocess

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
