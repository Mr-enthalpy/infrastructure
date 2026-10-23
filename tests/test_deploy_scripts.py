import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [
    "preflight.sh",
    "audit_legacy.sh",
    "install_server.sh",
    "create_user.sh",
    "verify_gpu_permissions.sh",
    "verify_monitor_only.sh",
    "check_disk.sh",
    "configure_quotas.sh",
]


def _bash() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")


def test_log_rotation_helper_compiles(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "py_compile",
            str(ROOT / "deploy" / "rotate_job_logs.py"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", SCRIPTS)
def test_shell_syntax(script):
    result = subprocess.run(
        [BASH, "-n", str(ROOT / "deploy" / script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", SCRIPTS)
def test_help_does_not_require_root(script):
    result = subprocess.run(
        [BASH, str(ROOT / "deploy" / script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in (result.stdout + result.stderr).lower()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux installer dry-run")
def test_install_server_dry_run_does_not_require_root():
    result = subprocess.run(
        [
            BASH,
            str(ROOT / "deploy/install_server.sh"),
            "--dry-run",
            "--skip-preflight",
            "--python",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "[dry-run]" in result.stdout


def test_installer_has_transactional_backup_and_rollback():
    source = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert "trap on_exit EXIT" in source
    assert "BACKUP_DIR" in source and "/var/backups/udc-resource" in source
    assert "cp -a --" in source
    assert "rollback" in source
    assert "systemd-analyze verify" in source
    assert "sshd" in source and "-t" in source
    assert "STATE_DB_BACKUP" in source and "source.backup" in source
    assert "udevadm verify" in source


def test_quota_helper_enables_ext4_project_inheritance():
    source = (ROOT / "deploy/configure_quotas.sh").read_text(encoding="utf-8")
    assert "chattr -p" in source
    assert "chattr +P" in source
    assert "setquota -P" in source


def test_gpu_verifier_checks_udev_acl_and_forbidden_groups():
    source = (ROOT / "deploy/verify_gpu_permissions.sh").read_text(encoding="utf-8")
    assert "udevadm info" in source
    assert "SUBSYSTEM=" in source
    assert "getfacl" in source
    for group in ("udc-gpu", "video", "render", "docker", "sudo", "udc-admins"):
        assert group in source
    assert "nvidia*" in source


def test_broker_runtime_directory_allows_member_traversal():
    unit = (ROOT / "deploy/systemd/udc-resource-broker.service").read_text(encoding="utf-8")
    assert "RuntimeDirectoryMode=0711" in unit
    assert "RuntimeDirectoryMode=0750" not in unit


def test_active_acceptance_python_syntax():
    path = ROOT / "deploy/acceptance_active.py"
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")


def test_active_acceptance_help():
    result = subprocess.run(
        [sys.executable, str(ROOT / "deploy/acceptance_active.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in (result.stdout + result.stderr).lower()
