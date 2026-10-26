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
    "alert.sh",
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
    assert "BACKUP_DIR" in source and "/var/backups/citespec-resource" in source
    assert "cp -a --" in source
    assert "rollback" in source
    assert "systemd-analyze verify" in source
    assert "sshd" in source and "-t" in source
    assert "STATE_DB_BACKUP" in source and "source.backup" in source
    assert "udevadm verify" in source


def test_quota_helper_enables_ext4_project_inheritance():
    source = (ROOT / "deploy/configure_quotas.sh").read_text(encoding="utf-8")
    assert "chattr -R -p" in source
    assert "chattr +P" in source
    assert "setquota -P" in source


def test_gpu_verifier_checks_udev_acl_and_forbidden_groups():
    source = (ROOT / "deploy/verify_gpu_permissions.sh").read_text(encoding="utf-8")
    assert "udevadm info" in source
    assert "SUBSYSTEM=" in source
    assert "getfacl" in source
    for group in ("citespec-gpu", "video", "render", "docker", "sudo", "citespec-admins"):
        assert group in source
    assert "nvidia*" in source
    assert "--json" in source
    assert "effective_members citespec-members" in source


def test_member_ssh_policy_is_key_only_and_uses_root_owned_shell():
    sshd = (ROOT / "deploy/sshd/60-citespec-resource.conf").read_text(encoding="utf-8")
    for line in (
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PubkeyAuthentication yes",
        "AuthenticationMethods publickey",
        "PermitUserRC no",
        "ForceCommand /usr/local/bin/citespec-resource-login",
    ):
        assert line in sshd
    shell = (ROOT / "deploy/member-shell.sh").read_text(encoding="utf-8")
    assert "exec /usr/local/bin/citespec-resource-login" in shell
    installer = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert "MEMBER_SHELL_INSTALL" in installer
    assert "member-shell.sh" in installer
    assert "GPU_VERIFY_INSTALL" in installer
    create_user = (ROOT / "deploy/create_user.sh").read_text(encoding="utf-8")
    assert "MEMBER_SHELL=/usr/local/libexec/citespec-resource/member-shell" in create_user
    assert "useradd --no-log-init --create-home --shell" in create_user


def test_create_user_provisions_and_verifies_workspace_quota():
    source = (ROOT / "deploy/create_user.sh").read_text(encoding="utf-8")
    assert "--no-workspace" in source
    assert "--limit-gb" in source
    assert "provision_workspace" in source
    assert "verify_workspace_quota" in source
    # The provisioning transaction must query the filesystem back instead of
    # trusting the apply commands' exit codes.
    assert "lsattr -p -d" in source
    assert "report -p -b" in source
    assert "repquota -P" in source


def test_audit_checks_effective_member_ssh_policy():
    source = (ROOT / "deploy/audit_legacy.sh").read_text(encoding="utf-8")
    assert "sshd -T -C" in source or '"-T" "-C"' in source or "-T -C" in source
    assert "forcecommand|/usr/local/bin/citespec-resource-login" in source
    assert "permituserrc|no" in source
    assert "authenticationmethods|publickey" in source
    assert "MEMBER_SHELL" in source


def test_preflight_blocks_development_profile_without_overlay_xfs_quota():
    source = (ROOT / "deploy/preflight.sh").read_text(encoding="utf-8")
    assert "--config" in source
    assert "container_profile" in source
    assert "overlay2" in source
    assert "prjquota|pquota" in source
    assert "xfs_quota" in source
    assert "development profile requires" in source


def test_installer_enables_timers_by_default_with_opt_out():
    source = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert "ENABLE_TIMERS=1" in source
    assert "--no-timers" in source
    assert "systemctl enable --now citespec-resource-logrotate.timer" in source
    assert "systemctl enable --now citespec-resource-disk-check.timer" in source
    assert "systemctl enable --now citespec-resource-backup.timer" in source
    assert "citespec-resource-alert@.service" in source
    assert "ALERT_LOG_DIR" in source


def test_backup_and_alert_units_are_installed_and_failure_wired():
    backup_unit = (ROOT / "deploy/systemd/citespec-resource-backup.service").read_text(
        encoding="utf-8"
    )
    assert "OnFailure=citespec-resource-alert@%n.service" in backup_unit
    assert "citespec_resource.backup" in backup_unit
    assert "/var/backups/citespec-resource" in backup_unit
    assert "User=root" in backup_unit
    assert "ProtectSystem=strict" in backup_unit
    timer = (ROOT / "deploy/systemd/citespec-resource-backup.timer").read_text(encoding="utf-8")
    assert "Persistent=true" in timer
    assert "citespec-resource-backup.service" in timer
    alert_unit = (ROOT / "deploy/systemd/citespec-resource-alert@.service").read_text(
        encoding="utf-8"
    )
    assert "ExecStart=/usr/local/sbin/citespec-alert %i" in alert_unit
    assert "ReadWritePaths=/var/log/citespec-resource" in alert_unit
    for name in (
        "citespec-resource-broker.service",
        "citespec-resource-intake.service",
        "citespec-resource-disk-check.service",
        "citespec-resource-logrotate.service",
    ):
        text = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
        assert "OnFailure=citespec-resource-alert@%n.service" in text


def test_development_container_image_has_sshd_sudo_and_bootstrap():
    dockerfile = (ROOT / "deploy/docker/Dockerfile.base").read_text(encoding="utf-8")
    for package in ("sudo", "openssh-server", "openssh-client"):
        assert package in dockerfile
    assert "NOPASSWD" in dockerfile
    assert "sshd_config" in dockerfile
    assert "bootstrap.sh" in dockerfile
    sshd = (ROOT / "deploy/docker/sshd_config").read_text(encoding="utf-8")
    assert "PasswordAuthentication no" in sshd
    assert "PermitUserRC no" in sshd
    assert "AuthenticationMethods publickey" in sshd
    bootstrap = (ROOT / "deploy/docker/bootstrap.sh").read_text(encoding="utf-8")
    assert "apt-get install" in bootstrap
    assert "pip install" in bootstrap


def test_broker_runtime_directory_allows_member_traversal():
    unit = (ROOT / "deploy/systemd/citespec-resource-broker.service").read_text(encoding="utf-8")
    assert "RuntimeDirectoryMode=0711" in unit
    assert "RuntimeDirectoryMode=0750" not in unit
    assert "ProtectHome=read-only" in unit


def test_provisioning_service_allows_request_bursts():
    unit = (ROOT / "deploy/systemd/citespec-resource-provision.service").read_text(encoding="utf-8")
    assert "StartLimitIntervalSec=0" in unit


def test_provisioning_spool_has_atomic_claim_and_recovery_paths():
    path_unit = (ROOT / "deploy/systemd/citespec-resource-provision.path").read_text(
        encoding="utf-8"
    )
    assert "requests/*.json" in path_unit
    assert "processing/*.json" in path_unit
    installer = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert "PROVISION_SPOOL_DIR/processing" in installer
    assert "-m 0700" in installer


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
