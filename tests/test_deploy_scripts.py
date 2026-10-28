import os
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
    "verify_gpu_isolation.sh",
    "verify_monitor_only.sh",
    "verify_portal_network.sh",
    "enable_portal.sh",
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


def test_preflight_reports_nvidia_side_effects_and_wrapper_integrity():
    source = (ROOT / "deploy" / "preflight.sh").read_text(encoding="utf-8")
    assert "--skip-nvidia" in source
    assert "--probe-nvidia" in source
    assert "--nvidia-only" in source
    # A root nvidia-smi query can reset /dev/nvidia* before the wrapper exists,
    # so probing is opt-in and the default is static-only.
    assert "PROBE_NVIDIA=0" in source
    assert "PROBE_NVIDIA=1" in source
    assert "NVIDIA_ONLY=1" in source
    assert "nvidia_device_state" in source
    assert "NVIDIA_DEVICES_BEFORE" in source and "NVIDIA_DEVICES_AFTER" in source
    assert "nvidia-smi did not change /dev/nvidia* mode/ownership" in source
    assert "audit_wrapped_helper" in source
    assert "exec /usr/bin/nvidia-smi.real" in source
    assert "nvidia-modprobe.real" in source
    assert "NVIDIA_MODPROBE_REAL" in source
    assert "mode_has_special_bits" in source
    assert "has setuid/setgid bits" in source
    assert "NVIDIA chmod shim" in source
    # Development containers with sudo stay closed until the real-host test.
    assert "GPU_ISOLATION_MARKER" in source
    assert "passing deploy/verify_gpu_isolation.sh run" in source
    assert '! -f "$GPU_ISOLATION_MARKER"' in source


def test_nginx_examples_restrict_sources_and_bind_private_addresses():
    for name, port in (("portal", 8770), ("apply", 8765)):
        source = (ROOT / "deploy" / "nginx" / f"citespec-{name}.conf.example").read_text(
            encoding="utf-8"
        )
        assert "listen <host-private-ip>:443 ssl;" in source
        assert "allow <vpn-source-cidr>;" in source
        assert "deny all;" in source
        assert f"proxy_pass http://127.0.0.1:{port};" in source
        assert "X-Citespec-Client-IP $remote_addr;" in source
        assert "listen 0.0.0.0" not in source
        assert "listen [::]" not in source
        assert source.index("allow <vpn-source-cidr>;") < source.index("deny all;")


PORTAL_VHOST_GOOD = """server {
    listen 10.20.30.40:443 ssl;
    server_name citespec-portal.lab.example;
    allow 10.20.0.0/16;
    deny all;
    location / {
        proxy_pass http://127.0.0.1:8770;
        proxy_set_header X-Citespec-Client-IP $remote_addr;
    }
}
"""

PORTAL_VHOST_WILDCARD = """server {
    listen 0.0.0.0:443 ssl;
    server_name citespec-portal.lab.example;
    location / {
        proxy_pass http://127.0.0.1:8770;
        proxy_set_header X-Citespec-Client-IP $http_x_citespec_client_ip;
    }
}
"""

STUB_SS_WILDCARD = """#!/usr/bin/env bash
cat <<'EOF'
State Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0 4096 0.0.0.0:8770 0.0.0.0:*
EOF
"""


def _portal_network_env(tmp_path, wildcard, vhost=None):
    root = tmp_path / "nginx"
    root.mkdir()
    (root / "portal.conf").write_text(
        vhost or (PORTAL_VHOST_WILDCARD if wildcard else PORTAL_VHOST_GOOD),
        encoding="utf-8",
    )
    stub = tmp_path / "bin"
    stub.mkdir()
    ss = stub / "ss"
    ss.write_text(
        STUB_SS_WILDCARD if wildcard else "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
    )
    ss.chmod(0o755)
    systemctl = stub / "systemctl"
    systemctl.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    systemctl.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
    }


def test_verify_portal_network_accepts_a_private_restricted_vhost(tmp_path):
    env = _portal_network_env(tmp_path, wildcard=False)
    result = subprocess.run(
        [
            BASH,
            str(ROOT / "deploy" / "verify_portal_network.sh"),
            "--nginx-root",
            str(tmp_path / "nginx"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "binds a specific address" in result.stdout
    assert "allow rule(s) followed by deny all" in result.stdout
    assert "portal network audit passed" in result.stdout


def test_verify_portal_network_rejects_wildcard_and_missing_allow(tmp_path):
    env = _portal_network_env(tmp_path, wildcard=True)
    result = subprocess.run(
        [
            BASH,
            str(ROOT / "deploy" / "verify_portal_network.sh"),
            "--nginx-root",
            str(tmp_path / "nginx"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "binds a wildcard address" in output
    assert "must restrict sources" in output
    assert "exposed beyond loopback" in output
    assert "client-supplied X-Citespec-Client-IP" in output


def test_verify_portal_network_rejects_addressless_listen(tmp_path):
    vhost = PORTAL_VHOST_GOOD.replace("listen 10.20.30.40:443 ssl;", "listen 443 ssl;")
    env = _portal_network_env(tmp_path, wildcard=False, vhost=vhost)
    result = subprocess.run(
        [
            BASH,
            str(ROOT / "deploy" / "verify_portal_network.sh"),
            "--nginx-root",
            str(tmp_path / "nginx"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "omits a bind address" in result.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX sudo stub")
@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        # sudo 1.8 exits 0 for these lines; the exit status is not a signal.
        ("User citespec-portal is not allowed to run sudo on host.", 1),
        ("Sorry, user citespec-portal may not run sudo on host.", 1),
        (
            "User citespec-portal may run the following commands on host:\n    (ALL : ALL) ALL",
            0,
        ),
        (
            "User citespec-portal may run the following commands on host:\n"
            "    (root) NOPASSWD: /bin/systemctl",
            0,
        ),
    ],
)
def test_installer_reads_the_sudo_listing_not_the_exit_status(tmp_path, listing, expected):
    """Regression: any existing portal account was rejected on reinstall."""
    source = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert "if portal_account_has_sudo; then" in source
    start = source.index("portal_account_has_sudo() {")
    end = source.index("\n}\n", start) + len("\n}\n")
    helper = source[start:end]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "sudo"
    stub.write_text(f"#!/bin/sh\ncat <<'LISTING'\n{listing}\nLISTING\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)

    prelude = 'set -u\nPORTAL_USER=citespec-portal\nhave() { command -v "$1" >/dev/null 2>&1; }\n'
    script = prelude + helper + "\nportal_account_has_sudo\n"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, check=False, env=env
    )
    assert result.returncode == expected, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX venv layout")
def test_installer_rewrites_the_staged_venv_prefix(tmp_path):
    """Regression: a promoted staging venv kept dead shebangs in bin/."""
    source = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert 'rewrite_venv_prefix "$VENV_STAGING" "$VENV_TARGET"' in source
    assert source.index('rewrite_venv_prefix "$VENV_STAGING"') < source.index("\n    switch_venv\n")
    start = source.index("rewrite_venv_prefix() {")
    end = source.index("\n}\n", start) + len("\n}\n")
    helper = source[start:end]

    staging = tmp_path / ".staging-0.1.1-1"
    target = tmp_path / "venv-0.1.1-1"
    (target / "bin").mkdir(parents=True)
    entry_point = target / "bin" / "citespec-resource"
    entry_point.write_text(f"#!{staging}/bin/python\nprint('x')\n", encoding="utf-8")
    activate = target / "bin" / "activate"
    activate.write_text(f"VIRTUAL_ENV={staging}\n", encoding="utf-8")
    binary = target / "bin" / "compiled-helper"
    binary.write_bytes(b"\x00" + str(staging).encode() + b"\x00")

    script = (
        "set -u\n"
        'fail() { echo "FAIL: $1"; exit 1; }\n' + helper + "\n"
        f'rewrite_venv_prefix "{staging}" "{target}"\n'
    )
    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert entry_point.read_text(encoding="utf-8") == f"#!{target}/bin/python\nprint('x')\n"
    assert activate.read_text(encoding="utf-8") == f"VIRTUAL_ENV={target}\n"
    # Compiled helpers are not rewritten; the check must not corrupt binaries.
    assert binary.read_bytes() == b"\x00" + str(staging).encode() + b"\x00"


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
    # Rollback restores unit/timer enablement and active state, records the
    # NVIDIA wrappers, and runs on an interrupting signal too.
    assert "capture_unit_state" in source
    assert "restore_unit_state" in source
    assert "disable_new_units" in source
    assert "record_nvidia_state" in source
    assert "verify_nvidia_state" in source
    assert "trap 'on_signal 129' HUP" in source
    assert "trap 'on_signal 130' INT" in source
    assert "trap 'on_signal 143' TERM" in source


STUB_SYSTEMCTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
cmd=$1
shift
case "$cmd" in
    is-active)
        unit=${!#}
        grep -Fxq "$unit" "$STUB_ACTIVE" && exit 0
        exit 3
        ;;
    is-enabled)
        unit=${!#}
        grep -Fxq "$unit" "$STUB_ENABLED" && exit 0
        exit 1
        ;;
    stop)
        unit=${!#}
        grep -Fxv "$unit" "$STUB_ACTIVE" > "$STUB_ACTIVE.tmp" || true
        mv "$STUB_ACTIVE.tmp" "$STUB_ACTIVE"
        ;;
    start)
        unit=${!#}
        grep -Fxq "$unit" "$STUB_ACTIVE" || printf '%s\\n' "$unit" >> "$STUB_ACTIVE"
        ;;
    enable)
        unit=${!#}
        grep -Fxq "$unit" "$STUB_ENABLED" || printf '%s\\n' "$unit" >> "$STUB_ENABLED"
        ;;
    disable)
        unit=${!#}
        grep -Fxv "$unit" "$STUB_ENABLED" > "$STUB_ENABLED.tmp" || true
        mv "$STUB_ENABLED.tmp" "$STUB_ENABLED"
        ;;
esac
exit 0
"""

ROLLBACK_DRIVER = """set -u
source "$INSTALLER"
DRY_RUN=0
INSTALL_COMPLETE=0
ROLLBACK_ARMED=1
BACKUP_DIR="$TMP/backup"
mkdir -p "$BACKUP_DIR"
UNIT_NAMES=(unit-a.service unit-b.service)
UNIT_STOP_ORDER=(unit-b.service unit-a.service)
TIMER_NAMES=(timer-a.timer timer-b.timer)
printf 'unit-a.service\\ntimer-a.timer\\n' > "$STUB_ACTIVE"
printf 'unit-a.service\\ntimer-a.timer\\n' > "$STUB_ENABLED"
capture_unit_state
# The installer then enabled and started its own units and timers.
printf 'unit-a.service\\nunit-b.service\\ntimer-a.timer\\ntimer-b.timer\\n' > "$STUB_ACTIVE"
printf 'unit-a.service\\nunit-b.service\\ntimer-a.timer\\ntimer-b.timer\\n' > "$STUB_ENABLED"

UNIT_FILE="$TMP/etc/systemd/system/unit-b.service"
mkdir -p "$(dirname "$UNIT_FILE")"
backup_file "$UNIT_FILE"
printf '[Unit]\\nDescription=installed\\n' > "$UNIT_FILE"

NVIDIA_PATHS=("$TMP/usr/bin/nvidia-smi" "$TMP/usr/bin/nvidia-smi.real")
NVIDIA_STATE_FILE="$BACKUP_DIR/nvidia-state.tsv"
mkdir -p "$TMP/usr/bin"
printf '#!/bin/sh\\necho real\\n' > "$TMP/usr/bin/nvidia-smi"
chmod 0755 "$TMP/usr/bin/nvidia-smi"
record_nvidia_state
backup_file "$TMP/usr/bin/nvidia-smi"
backup_file "$TMP/usr/bin/nvidia-smi.real"
mv "$TMP/usr/bin/nvidia-smi" "$TMP/usr/bin/nvidia-smi.real"
printf '#!/bin/sh\\necho wrapper\\n' > "$TMP/usr/bin/nvidia-smi"
chmod 0755 "$TMP/usr/bin/nvidia-smi"
if [ "${TAMPER_BACKUP:-0}" = "1" ]; then
    printf 'tampered\\n' > "$BACKUP_DIR${TMP}/usr/bin/nvidia-smi"
fi
rollback 1
"""


def _rollback_env(tmp_path):
    stub = tmp_path / "bin"
    stub.mkdir()
    systemctl = stub / "systemctl"
    systemctl.write_text(STUB_SYSTEMCTL, encoding="utf-8")
    systemctl.chmod(0o755)
    udevadm = stub / "udevadm"
    udevadm.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    udevadm.chmod(0o755)
    log = tmp_path / "systemctl.log"
    env = {
        **os.environ,
        "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_ACTIVE": str(tmp_path / "active"),
        "STUB_ENABLED": str(tmp_path / "enabled"),
        "TMP": str(tmp_path),
        "INSTALLER": str(ROOT / "deploy" / "install_server.sh"),
        "CITESPEC_INSTALL_LIB_ONLY": "1",
    }
    return env, log


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="stub-based installer rollback uses POSIX stat/cp semantics",
)
def test_installer_rollback_restores_units_timers_and_nvidia_state(tmp_path):
    env, log = _rollback_env(tmp_path)
    driver = tmp_path / "rollback.sh"
    driver.write_text(ROLLBACK_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [BASH, str(driver)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 1, result.stderr
    nvidia = tmp_path / "usr/bin/nvidia-smi"
    assert nvidia.read_text(encoding="utf-8") == "#!/bin/sh\necho real\n"
    assert not (tmp_path / "usr/bin/nvidia-smi.real").exists()
    assert not (tmp_path / "etc/systemd/system/unit-b.service").exists()
    assert "rollback mismatch" not in result.stderr + result.stdout
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines.index("stop timer-b.timer") < lines.index("stop unit-b.service")
    assert "disable unit-b.service" in lines
    assert "disable timer-b.timer" in lines
    assert "daemon-reload" in lines
    assert "start unit-a.service" in lines
    assert "start timer-a.timer" in lines
    assert "start unit-b.service" not in lines
    assert "start timer-b.timer" not in lines


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="stub-based installer rollback uses POSIX stat/cp semantics",
)
def test_installer_rollback_reports_nvidia_state_mismatch(tmp_path):
    env, _ = _rollback_env(tmp_path)
    env["TAMPER_BACKUP"] = "1"
    driver = tmp_path / "rollback-tampered.sh"
    driver.write_text(ROLLBACK_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [BASH, str(driver)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 1
    assert "rollback mismatch" in result.stdout + result.stderr
    assert "state mismatches" in result.stdout + result.stderr


def test_preflight_fails_active_mode_when_gpu_compute_is_busy():
    source = (ROOT / "deploy/preflight.sh").read_text(encoding="utf-8")
    assert 'if [ "${MODE:-monitor}" = "active" ]' in source
    assert "active mode requires no GPU compute processes" in source
    assert "could not query GPU compute processes" in source


def test_installer_provisions_root_only_broker_admin_key():
    source = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert "server.admin_auth_key_file" in source
    assert "broker-admin.key" in source
    assert "server.admin_auth_key_file must be under /etc/citespec-resource" in source
    assert "must name a file, not a directory" in source
    assert "must not be a symbolic link" in source
    assert 'chown root:root "$ADMIN_AUTH_KEY_FILE"' in source
    assert 'chmod 0600 "$ADMIN_AUTH_KEY_FILE"' in source


def test_installer_overrides_every_unit_that_embeds_the_workspace_root():
    source = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert "SYSTEMD_WORKSPACE_ROOT=/home/data1/citespec-users" in source
    assert "WORKSPACE_DROPIN_ROOT=/etc/systemd/system" in source
    assert (
        "BROKER_WORKSPACE_DROPIN=$WORKSPACE_DROPIN_ROOT/citespec-resource-broker.service.d/10-workspace.conf"
        in source
    )
    assert (
        "LOG_WORKSPACE_DROPIN=$WORKSPACE_DROPIN_ROOT/citespec-resource-logrotate.service.d"
        in source
    )
    assert (
        "DISK_WORKSPACE_DROPIN=$WORKSPACE_DROPIN_ROOT/citespec-resource-disk-check.service.d"
        in source
    )
    assert (
        "PROVISION_WORKSPACE_DROPIN=$WORKSPACE_DROPIN_ROOT/citespec-resource-provision.service.d"
        in source
    )
    assert "install_workspace_dropins" in source
    assert "remove_workspace_dropins" in source
    assert "ReadWritePaths=$root" in source
    assert "ReadOnlyPaths=$root" in source
    assert "Environment=CITESPEC_RESOURCE_WORKSPACE_ROOT=$root" in source
    assert "--workspace-root $root" in source
    assert "--path $root" in source
    assert "does not match the static systemd workspace" not in source
    assert 'warn "systemd unit ReadWritePaths' not in source
    for name in (
        "citespec-resource-broker.service",
        "citespec-resource-provision.service",
        "citespec-resource-disk-check.service",
        "citespec-resource-logrotate.service",
    ):
        unit = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
        assert "/home/data1/citespec-users" in unit
    # A missing default path must not make a unit fail its sandbox setup.
    for name in (
        "citespec-resource-provision.service",
        "citespec-resource-disk-check.service",
        "citespec-resource-logrotate.service",
    ):
        unit = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
        assert "-/home/data1/citespec-users" in unit


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
        "citespec-resource-portal.service",
        "citespec-resource-disk-check.service",
        "citespec-resource-logrotate.service",
    ):
        text = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
        assert "OnFailure=citespec-resource-alert@%n.service" in text


def test_portal_deploy_assets_are_vpn_only_and_least_privilege():
    unit = (ROOT / "deploy/systemd/citespec-resource-portal.service").read_text(encoding="utf-8")
    assert "User=citespec-portal" in unit
    assert "Group=citespec-portal" in unit
    assert "SupplementaryGroups=citespec-cli" in unit
    assert (
        "ExecStart=/opt/citespec-resource/.venv/bin/python -m citespec_resource.portal.server"
    ) in unit
    assert "StateDirectory=citespec-resource-portal" in unit
    assert "StateDirectoryMode=0750" in unit
    assert "ReadWritePaths=/var/lib/citespec-resource-portal" in unit
    assert "ProtectSystem=strict" in unit
    assert "NoNewPrivileges=true" in unit
    assert "CapabilityBoundingSet=" in unit
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX" in unit

    portal_nginx = (ROOT / "deploy/nginx/citespec-portal.conf.example").read_text(encoding="utf-8")
    assert "server_name citespec-portal.<lab-domain>;" in portal_nginx
    assert "proxy_pass http://127.0.0.1:8770;" in portal_nginx
    assert "proxy_set_header X-Citespec-Client-IP $remote_addr;" in portal_nginx
    assert "listen <host-private-ip>:443 ssl;" in portal_nginx
    assert "listen 443 ssl;" not in portal_nginx
    assert "0.0.0.0:443" not in portal_nginx
    assert "listen 80" not in portal_nginx
    assert "client_max_body_size 64k;" in portal_nginx

    apply_nginx = (ROOT / "deploy/nginx/citespec-apply.conf.example").read_text(encoding="utf-8")
    assert "server_name citespec-apply.<lab-domain>;" in apply_nginx
    assert "proxy_pass http://127.0.0.1:8765;" in apply_nginx
    assert "proxy_set_header X-Citespec-Client-IP $remote_addr;" in apply_nginx
    assert "listen <host-private-ip>:443 ssl;" in apply_nginx
    assert "listen 443 ssl;" not in apply_nginx
    assert "0.0.0.0:443" not in apply_nginx
    assert "listen 80" not in apply_nginx

    installer = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert "--extra gui --extra portal" in installer
    assert "$REPO_ROOT[gui,portal]" in installer
    assert (
        "CITESPEC_RESOURCE_GROUPS=(citespec-members citespec-cli "
        "citespec-admins citespec-gpu citespec-portal)"
    ) in installer
    assert "PORTAL_SECRET_FILE=/etc/citespec-resource/portal-secret.key" in installer
    assert 'chown root:"$PORTAL_USER"' in installer
    assert "chmod 0640" in installer
    assert "init_intake_db" in installer
    assert "init_portal_db" in installer
    # The installer must never enable the Portal: enabling alone makes it start
    # on the next boot, before DNS/TLS/nginx/VPN acceptance.
    assert "systemctl enable citespec-resource-portal.service" not in installer
    assert "run systemctl start citespec-resource-portal.service" not in installer
    assert "portal unit installed but NOT enabled" in installer
    assert "ConditionPathExists=/etc/citespec-resource/portal-network-accepted" in unit
    assert "ENABLE_PORTAL_INSTALL" in installer
    assert "enable_portal.sh" in installer
    assert "validate_existing_portal_account" in installer
    assert "only its primary group and citespec-cli are allowed" in installer


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


def test_installer_uses_a_versioned_venv_and_atomic_switch():
    source = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert "VENVS_DIR=$PREFIX/venvs" in source
    assert 'VENV_TARGET="$VENVS_DIR/venv-$RELEASE_VERSION-' in source
    assert 'VENV_STAGING="$VENVS_DIR/.staging-$RELEASE_VERSION-' in source
    assert "switch_venv()" in source
    assert "restore_venv()" in source
    assert "cleanup_venv_staging()" in source
    assert 'mv -T -- "$tmp_link" "$VENV"' in source
    assert 'ln -s -- "$VENV_TARGET" "$tmp_link"' in source
    # The new environment must pass an import/entry-point/version smoke test
    # before it becomes visible through .venv.
    assert "\"$VENV_STAGING/bin/python\" -c 'import citespec_resource'" in source
    assert '"$VENV_STAGING/bin/citespec-resource" version' in source
    assert "pip check" in source
    assert 'mv -- "$VENV_STAGING" "$VENV_TARGET"' in source
    # A real directory is moved aside, not deleted, so rollback is possible.
    assert 'VENV_PREVIOUS="$VENVS_DIR/legacy-' in source


def test_installer_requires_a_maintenance_window_and_consistent_snapshot():
    source = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert "--maintenance-window" in source
    assert "existing Citespec units are active; re-run with --maintenance-window" in source
    assert "(no host changes were made)" in source
    assert "take_preupgrade_snapshot" in source
    assert "restore_preupgrade_snapshot" in source
    assert 'SNAPSHOT_PYTHON="$VENV/bin/python"' in source
    # The maintenance-window refusal must happen before any group, account, or
    # membership mutation, not after partial host changes.
    gate = source.index("(no host changes were made)")
    first_groupadd = source.index('run groupadd --system "$group"')
    first_useradd = source.index("run useradd --system --gid")
    first_usermod = source.index('run usermod -aG citespec-cli "$PORTAL_USER"')
    assert gate < first_groupadd < first_useradd < first_usermod
    # It also runs before the backup directory, rollback trap, and any service
    # stop, so a refused upgrade leaves running units alone.
    assert gate < source.index('BACKUP_DIR="$BACKUP_ROOT/')
    assert gate < source.index("ROLLBACK_ARMED=1")
    assert gate < source.index("trap on_exit EXIT")
    # Reuse backup.py instead of copying SQLite files while writers run.
    assert "-m citespec_resource.backup backup" in source
    assert "--no-quiesce" in source
    assert "--keep 0" in source
    assert "manifest.json" in source
    assert "-m citespec_resource.backup restore" in source
    assert "--yes" in source
    # Rollback puts the old interpreter back before restoring its files.
    rollback = source.index("restore_venv\n    cleanup_venv_staging")
    restore = source.index("restore_preupgrade_snapshot", rollback)
    assert rollback < restore


def test_installer_verifies_an_existing_portal_account_before_reuse():
    source = (ROOT / "deploy/install_server.sh").read_text(encoding="utf-8")
    assert "validate_existing_portal_account()" in source
    assert "validate_existing_portal_account\n" in source
    call = source.index(
        'if getent passwd "$PORTAL_USER" >/dev/null 2>&1; then\n'
        "    validate_existing_portal_account"
    )
    groupadd = source.index('run groupadd --system "$group"')
    usermod = source.index('run usermod -aG citespec-cli "$PORTAL_USER"')
    assert call < groupadd < usermod
    # The existing account must look like the system account the installer
    # would create; any human/privileged shape fails closed before mutation.
    for token in (
        "outside the system range",
        "interactive login shell",
        "primary group",
        "password status",
        "has sudo privileges",
        "only its primary group and citespec-cli are allowed",
        'if [ "$home" != "$PORTAL_STATE_DIR" ]',
        'id -nG "$PORTAL_USER"',
        'sudo -n -l -U "$PORTAL_USER"',
    ):
        assert token in source


def test_backup_service_can_checkpoint_every_live_database():
    unit = (ROOT / "deploy/systemd/citespec-resource-backup.service").read_text(encoding="utf-8")
    assert "ReadWritePaths=/var/backups/citespec-resource" in unit
    assert "ReadWritePaths=/var/lib/citespec-resource" in unit
    assert "ReadWritePaths=-/var/lib/citespec-resource-intake" in unit
    assert "ReadWritePaths=-/var/lib/citespec-resource-portal" in unit
    assert "ProtectSystem=strict" in unit
    assert "--keep 14" in unit


def test_portal_enablement_is_the_only_acceptance_gate():
    script = (ROOT / "deploy/enable_portal.sh").read_text(encoding="utf-8")
    assert "verify_portal_network.sh" in script
    assert "--acceptance" in script
    assert "portal-network-accepted" in script
    assert 'systemctl enable --now "$PORTAL_UNIT"' in script
    assert 'systemctl disable --now "$PORTAL_UNIT"' in script
    assert "refusing to enable the Portal without --yes" in script
    assert "[portal].enabled is false" in script
    # A failed re-run must not disable an already accepted Portal.
    assert "MARKER_PREEXISTED" in script
    assert "UNIT_WAS_ENABLED" in script
    assert "kept the pre-existing acceptance marker" in script
    # The marker is written only after the audit, and an enable failure rolls
    # both the marker and enablement back.
    audit_at = script.index('note "running fail-closed Portal network audit"')
    marker_at = script.index('install -o root -g root -m 0600 "$MARKER_TMP" "$MARKER"')
    enable_at = script.index('systemctl enable --now "$PORTAL_UNIT"')
    assert audit_at < marker_at < enable_at
    assert "cleanup_on_error" in script

    unit = (ROOT / "deploy/systemd/citespec-resource-portal.service").read_text(encoding="utf-8")
    assert "ConditionPathExists=/etc/citespec-resource/portal-network-accepted" in unit


def test_gpu_isolation_helper_is_fail_closed():
    script = (ROOT / "deploy/verify_gpu_isolation.sh").read_text(encoding="utf-8")
    assert "refusing to mutate a GPU device node without --confirm" in script
    assert "--write-marker requires --bystander" in script
    assert "citespec.profile" in script
    assert 'PROFILE" = "development"' in script
    assert "sudo -n chmod 0666" in script
    assert "sudo -n chown 1000:1000" in script
    assert "cannot see $DEVICE" in script
    assert "could not record host device state before the test" in script
    assert "restore_device_state" in script
    # The destructive window starts before the first possible chmod; any exit,
    # including an interrupt, restores and verifies the recorded device state.
    mutation = script.index("MUTATION_STARTED=1\n{")
    chmod = script.index("sudo -n chmod 0666")
    assert mutation < chmod
    assert 'if [ "$MUTATION_STARTED" -eq 1 ]' in script
    assert "restore_device_state_verified" in script
    assert "after-restored-interrupted.txt" in script
    assert "trap 'exit 130' INT" in script
    assert "trap 'exit 143' TERM" in script
    assert "container root could not change host device mode/owner/inode" in script
    assert "non-allocated member $BYSTANDER can read $DEVICE" in script
    assert 'install -o root -g root -m 0600 "$MARKER_TMP" "$MARKER"' in script


def _load_portal_audit():
    import importlib.util

    path = ROOT / "deploy" / "portal_network_audit.py"
    spec = importlib.util.spec_from_file_location("portal_network_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PORTAL_AUDIT_VHOST = """server {{
    listen 10.20.30.40:443 ssl;
    server_name citespec-portal.lab.example;
    allow 10.20.0.0/16;
    deny all;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_certificate {cert};
    ssl_certificate_key {key};
    location / {{
        proxy_pass http://127.0.0.1:8770;
        proxy_set_header X-Citespec-Client-IP $remote_addr;
    }}
}}
"""

STUB_NGINX = """#!/usr/bin/env bash
case "$1" in
    -t) exit 0 ;;
    -T) cat "$STUB_NGINX_CONFIG" ;;
esac
exit 0
"""

STUB_OPENSSL = """#!/usr/bin/env bash
if [ "$1" = "x509" ]; then
    case "$*" in
        *-pubkey*) printf 'PUBLIC\\n' ;;
    esac
    exit 0
fi
if [ "$1" = "pkey" ]; then
    case "$*" in
        *-pubout*) printf 'PUBLIC\\n' ;;
    esac
    exit 0
fi
exit 0
"""

STUB_SS_LOOPBACK = """#!/usr/bin/env bash
printf 'State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\\n'
printf 'LISTEN 0 4096 127.0.0.1:8770 0.0.0.0:*\\n'
"""


def _portal_audit_env(tmp_path, vhost):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "nginx"
    root.mkdir()
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    config = root / "site.conf"
    config.write_text(vhost.format(cert=cert, key=key), encoding="utf-8")
    stub = tmp_path / "bin"
    stub.mkdir()
    for name, body in (
        ("nginx", STUB_NGINX),
        ("openssl", STUB_OPENSSL),
        ("ss", STUB_SS_LOOPBACK),
        ("systemctl", "#!/usr/bin/env bash\nexit 1\n"),
    ):
        script = stub / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
        "STUB_NGINX_CONFIG": str(config),
    }


@pytest.mark.skipif(sys.platform != "linux", reason="stub-based nginx/openssl audit")
def test_portal_acceptance_audit_passes_only_for_a_complete_safe_vhost(tmp_path):
    script = ROOT / "deploy" / "portal_network_audit.py"

    good_env = _portal_audit_env(tmp_path / "good", PORTAL_AUDIT_VHOST)
    good = subprocess.run(
        [sys.executable, str(script), "--approved-cidr", "10.20.0.0/16"],
        capture_output=True,
        text=True,
        check=False,
        env=good_env,
    )
    assert good.returncode == 0, good.stdout + good.stderr
    assert "portal network acceptance audit passed" in good.stdout

    wildcard_env = _portal_audit_env(
        tmp_path / "wildcard",
        PORTAL_AUDIT_VHOST.replace("listen 10.20.30.40:443", "listen 0.0.0.0:443"),
    )
    wildcard = subprocess.run(
        [sys.executable, str(script), "--approved-cidr", "10.20.0.0/16"],
        capture_output=True,
        text=True,
        check=False,
        env=wildcard_env,
    )
    assert wildcard.returncode == 1, wildcard.stdout + wildcard.stderr
    assert "wildcard" in wildcard.stdout

    unapproved_env = _portal_audit_env(
        tmp_path / "unapproved",
        PORTAL_AUDIT_VHOST.replace("allow 10.20.0.0/16;", "allow 192.168.0.0/16;"),
    )
    unapproved = subprocess.run(
        [sys.executable, str(script), "--approved-cidr", "10.20.0.0/16"],
        capture_output=True,
        text=True,
        check=False,
        env=unapproved_env,
    )
    assert unapproved.returncode == 1, unapproved.stdout + unapproved.stderr
    assert "outside the approved campus/VPN ranges" in unapproved.stdout

    missing_cidr = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
        env=good_env,
    )
    assert missing_cidr.returncode == 1, missing_cidr.stdout + missing_cidr.stderr
    assert "no approved campus/VPN CIDR" in missing_cidr.stdout

    bad_tls_env = _portal_audit_env(
        tmp_path / "tls",
        PORTAL_AUDIT_VHOST.replace(
            "ssl_protocols TLSv1.2 TLSv1.3;", "ssl_protocols TLSv1 TLSv1.1;"
        ),
    )
    bad_tls = subprocess.run(
        [sys.executable, str(script), "--approved-cidr", "10.20.0.0/16"],
        capture_output=True,
        text=True,
        check=False,
        env=bad_tls_env,
    )
    assert bad_tls.returncode == 1, bad_tls.stdout + bad_tls.stderr
    assert "obsolete TLS protocols" in bad_tls.stdout


def test_portal_audit_rejects_unapproved_and_wildcard_sources():
    module = _load_portal_audit()
    module.FAIL = 0
    module.WARN = 0
    approved = [module.ipaddress.ip_network("10.20.0.0/16")]
    block = module.parse_config(PORTAL_VHOST_GOOD)[0]
    module._check_access(block.block, "portal", approved)
    assert module.FAIL == 0

    module.FAIL = 0
    outside = module.parse_config(
        PORTAL_VHOST_GOOD.replace("allow 10.20.0.0/16;", "allow 192.168.0.0/16;")
    )[0]
    module._check_access(outside.block, "portal", approved)
    assert module.FAIL == 1

    module.FAIL = 0
    module._check_listen(block, "portal", 443)
    assert module.FAIL == 0

    module.FAIL = 0
    wildcard = module.parse_config(PORTAL_VHOST_WILDCARD)[0]
    module._check_listen(wildcard, "portal", 443)
    assert module.FAIL == 1

    module.FAIL = 0
    module._check_access(
        module.parse_config(PORTAL_VHOST_GOOD.replace("allow 10.20.0.0/16;", "allow all;"))[
            0
        ].block,
        "portal",
        approved,
    )
    assert module.FAIL >= 1


def test_portal_audit_requires_a_usable_approved_cidr():
    module = _load_portal_audit()
    module.FAIL = 0
    module.WARN = 0
    assert module._approved_networks([], None) == []
    assert module.FAIL == 1

    module.FAIL = 0
    assert module._approved_networks(["0.0.0.0/0"], None) == []
    assert module.FAIL >= 1

    module.FAIL = 0
    assert len(module._approved_networks(["10.20.0.0/16"], None)) == 1
    assert module.FAIL == 0


def test_active_acceptance_help():
    result = subprocess.run(
        [sys.executable, str(ROOT / "deploy/acceptance_active.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage" in (result.stdout + result.stderr).lower()
