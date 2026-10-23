import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "rotate_job_logs.py"


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative rotation is Linux-only")
def test_rotation_keeps_active_log_and_shifts_archives(tmp_path: Path):
    workspace = tmp_path / "workspace"
    logs = workspace / "alice" / ".udc-logs"
    logs.mkdir(parents=True)
    log = logs / "train.log"
    log.write_bytes(b"abcdefgh")

    def rotate() -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "--workspace-root",
                str(workspace),
                "--max-bytes",
                "4",
                "--keep",
                "2",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    rotate()
    assert log.read_bytes() == b""
    assert (logs / "train.log.1").read_bytes() == b"abcdefgh"

    log.write_bytes(b"ijklm")
    rotate()
    assert log.read_bytes() == b""
    assert (logs / "train.log.1").read_bytes() == b"ijklm"
    assert (logs / "train.log.2").read_bytes() == b"abcdefgh"


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative rotation is Linux-only")
def test_rotation_skips_symlinked_log_directory(tmp_path: Path):
    workspace = tmp_path / "workspace"
    user = workspace / "alice"
    user.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "train.log").write_bytes(b"secret" * 10)
    (user / ".udc-logs").symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--workspace-root",
            str(workspace),
            "--max-bytes",
            "4",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert (outside / "train.log").read_bytes() == b"secret" * 10
