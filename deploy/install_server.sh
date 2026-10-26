#!/usr/bin/env bash
# Idempotent server installer for citespec-resource.
#
# This script never starts GPU allocations and never touches existing
# containers. It installs the package, groups, udev rule, systemd unit, sshd
# drop-in, configuration, and SQLite state. A fresh database starts in monitor
# mode; existing active leases are preserved only after explicit approval.
set -u
set -o pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

PREFIX=/opt/citespec-resource
VENV=$PREFIX/.venv
CONFIG_DIR=/etc/citespec-resource
CONFIG_FILE=$CONFIG_DIR/config.toml
STATE_DIR=/var/lib/citespec-resource
SYSTEMD_UNIT=/etc/systemd/system/citespec-resource-broker.service
UDEV_RULE=/etc/udev/rules.d/99-citespec-gpu.rules
SSHD_DROPIN=/etc/ssh/sshd_config.d/60-citespec-resource.conf
ROTATE_SCRIPT=/usr/local/sbin/citespec-rotate-job-logs
ROTATE_UNIT=/etc/systemd/system/citespec-resource-logrotate.service
ROTATE_TIMER=/etc/systemd/system/citespec-resource-logrotate.timer
DISK_SCRIPT=/usr/local/sbin/citespec-check-disk
DISK_UNIT=/etc/systemd/system/citespec-resource-disk-check.service
DISK_TIMER=/etc/systemd/system/citespec-resource-disk-check.timer
ALERT_SCRIPT=/usr/local/sbin/citespec-alert
ALERT_UNIT=/etc/systemd/system/citespec-resource-alert@.service
BACKUP_UNIT=/etc/systemd/system/citespec-resource-backup.service
BACKUP_TIMER=/etc/systemd/system/citespec-resource-backup.timer
ALERT_LOG_DIR=/var/log/citespec-resource
INTAKE_UNIT=/etc/systemd/system/citespec-resource-intake.service
PROVISION_UNIT=/etc/systemd/system/citespec-resource-provision.service
PROVISION_PATH=/etc/systemd/system/citespec-resource-provision.path
CENTRAL_KEY_DIR=/etc/citespec-resource/authorized-keys.d
PROVISION_SPOOL_DIR=/var/lib/citespec-resource/provision
INTAKE_STATE_DIR=/var/lib/citespec-resource-intake
INTAKE_USER=citespec-intake
BANNER_FILE=/etc/ssh/citespec-resource-banner.txt
LEGACY_LOGROTATE=/etc/logrotate.d/citespec-resource
LOGIN_WRAPPER=/usr/local/bin/citespec-resource-login
CLI_LINK=/usr/local/bin/citespec-resource
LIBEXEC_DIR=/usr/local/libexec/citespec-resource
AUDIT_INSTALL=$LIBEXEC_DIR/audit_legacy.sh
GPU_VERIFY_INSTALL=$LIBEXEC_DIR/verify_gpu_permissions.sh
MEMBER_SHELL_INSTALL=$LIBEXEC_DIR/member-shell
CREATE_USER_INSTALL=$LIBEXEC_DIR/create_user.sh
PROVISION_EXECUTOR_INSTALL=$LIBEXEC_DIR/provision_executor.py
GPU_FIX_INSTALL=$LIBEXEC_DIR/fix_gpu_permissions.sh
GPU_SHIM_DIR=/usr/local/lib/citespec-resource
GPU_SHIM="$GPU_SHIM_DIR/libcitespec-nvidia-chmod.so"
GPU_SHIM_SRC="$GPU_SHIM_DIR/citespec_nvidia_chmod_shim.c"
GPU_PERM_UNIT=/etc/systemd/system/citespec-gpu-permissions.service
NVIDIA_SMI=/usr/bin/nvidia-smi
NVIDIA_SMI_REAL=/usr/bin/nvidia-smi.real
NVIDIA_MODPROBE=/usr/bin/nvidia-modprobe
NVIDIA_MODPROBE_REAL=/usr/bin/nvidia-modprobe.real
CITESPEC_RESOURCE_GROUPS=(citespec-members citespec-cli citespec-admins citespec-gpu)

DRY_RUN=0
ASSUME_YES=0
SKIP_PREFLIGHT=0
START=0
ALLOW_ACTIVE=0
FORCE_CONFIG=0
ENABLE_TIMERS=1
AUDIT_FAILED=0
CONFIG_SOURCE=""
PYTHON_BIN=${PYTHON_BIN:-}
BACKUP_ROOT=/var/backups/citespec-resource
BACKUP_DIR=""
ROLLBACK_ARMED=0
INSTALL_COMPLETE=0
BROKER_WAS_ACTIVE=0
BROKER_WAS_ENABLED=0
BROKER_START_ATTEMPTED=0
BACKED_UP=()
STATE_DB=""
STATE_DB_BACKUP=""
STATE_DB_EXISTED=0

usage() {
    cat <<'EOF'
usage: install_server.sh [options]

Options:
  --dry-run             Print actions without changing the host.
  --yes                 Required for a real (non-dry-run) installation.
  --skip-preflight      Skip deploy/preflight.sh (not recommended).
  --start               Start the broker after installation (default: install only).
  --allow-active        Allow an active-mode config after training and approval.
                        A fresh database still starts in monitor; existing active
                        leases are preserved only when deployment_allowed=true.
  --force-config        Overwrite an existing /etc/citespec-resource/config.toml.
  --no-timers           Do not enable the disk-alert, backup, and log-rotation timers.
  --config-source PATH  TOML config to install (default: lab/resources.local.toml
                        if present, otherwise lab/resources.example.toml).
  --python PATH         Python >= 3.11 used to create the venv.
  -h, --help            Show this help.

The installer is idempotent and refuses to run without --yes. It does not
create member accounts; use deploy/create_user.sh for that.
EOF
}

fail() { printf '[fail] %s\n' "$1" >&2; exit 1; }
warn() { printf '[warn] %s\n' "$1"; }
note() { printf '[info] %s\n' "$1"; }
ok() { printf '[ok]   %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

# List human members whose effective group set includes GROUP.  Supplementary
# group lists omit users whose primary group is the target group, so always ask
# the account database with id -G instead of parsing /etc/group field 4.
effective_members() {
    local group=$1 gid user uid
    gid=$(getent group "$group" 2>/dev/null | awk -F: 'NR == 1 {print $3}')
    [ -n "$gid" ] || return 0
    getent passwd | while IFS=: read -r user _ uid _; do
        [ "$uid" -ge 1000 ] || continue
        if id -G "$user" 2>/dev/null | tr ' ' '\n' | grep -qx "$gid"; then
            printf '%s\n' "$user"
        fi
    done
}

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[dry-run] %s\n' "$*"
        return 0
    fi
    if ! "$@"; then
        fail "command failed: $*"
    fi
}

run_shell() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[dry-run] %s\n' "$*"
        return 0
    fi
    if ! bash -c "$*"; then
        fail "command failed: $*"
    fi
}

backup_file() {
    local path=$1
    local rel
    [ "$DRY_RUN" -eq 0 ] || return 0
    [ "$ROLLBACK_ARMED" -eq 1 ] || return 0
    case " ${BACKED_UP[*]} " in
        *" $path "*) return 0 ;;
    esac
    rel=${path#/}
    mkdir -p "$BACKUP_DIR/$(dirname "$rel")" || fail "could not create backup directory"
    if [ -e "$path" ] || [ -L "$path" ]; then
        cp -a -- "$path" "$BACKUP_DIR/$rel" || fail "could not back up $path"
    else
        : > "$BACKUP_DIR/$rel.absent" || fail "could not record absent file $path"
    fi
    BACKED_UP+=("$path")
}

install_file() {
    local mode=$1
    local source=$2
    local destination=$3
    backup_file "$destination"
    if [ "$DRY_RUN" -eq 0 ] && [ -L "$destination" ]; then
        # install(1) follows a destination symlink; remove the link first so
        # rollback restores the link without having modified its target.
        run rm -f -- "$destination"
    fi
    run install -o root -g root -m "$mode" "$source" "$destination"
}

reload_sshd() {
    if systemctl list-unit-files --type=service 2>/dev/null | grep -q '^ssh\.service'; then
        systemctl reload ssh 2>/dev/null || true
    else
        systemctl reload sshd 2>/dev/null || true
    fi
}

rollback() {
    local code=${1:-$?}
    local path
    local rel
    if [ "$INSTALL_COMPLETE" -eq 1 ] || [ "$ROLLBACK_ARMED" -eq 0 ]; then
        return "$code"
    fi
    trap - EXIT
    printf '\n[rollback] installation failed (exit %s); restoring previous files\n' "$code" >&2
    if [ "$BROKER_START_ATTEMPTED" -eq 1 ]; then
        systemctl stop citespec-resource-broker.service 2>/dev/null || true
    fi
    for path in "${BACKED_UP[@]}"; do
        rel=${path#/}
        if [ -f "$BACKUP_DIR/$rel.absent" ]; then
            rm -f -- "$path" || true
        elif [ -e "$BACKUP_DIR/$rel" ] || [ -L "$BACKUP_DIR/$rel" ]; then
            mkdir -p -- "$(dirname "$path")" || true
            cp -a -- "$BACKUP_DIR/$rel" "$path" || true
        fi
    done
    BROKER_ACTIVE_NOW=0
    if systemctl is-active --quiet citespec-resource-broker.service 2>/dev/null; then
        BROKER_ACTIVE_NOW=1
    fi
    if [ -n "$STATE_DB" ] && [ "$BROKER_ACTIVE_NOW" -eq 0 ]; then
        if [ "$STATE_DB_EXISTED" -eq 1 ] && [ -f "$STATE_DB_BACKUP" ]; then
            rm -f -- "$STATE_DB-wal" "$STATE_DB-shm" || true
            cp -a -- "$STATE_DB_BACKUP" "$STATE_DB" || true
        elif [ "$STATE_DB_EXISTED" -eq 0 ]; then
            rm -f -- "$STATE_DB" "$STATE_DB-wal" "$STATE_DB-shm" || true
        fi
    elif [ -n "$STATE_DB" ]; then
        warn "broker is still running; leaving SQLite state in place during rollback"
    fi
    systemctl daemon-reload 2>/dev/null || true
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --action=change --sysname-match='nvidia*' 2>/dev/null || true
    reload_sshd
    if [ "$BROKER_WAS_ENABLED" -eq 0 ]; then
        systemctl disable citespec-resource-broker.service 2>/dev/null || true
    fi
    if [ "$START" -eq 1 ] && [ "$BROKER_WAS_ACTIVE" -eq 1 ]; then
        systemctl restart citespec-resource-broker.service 2>/dev/null || true
    fi
    printf '[rollback] restored %s path(s); backup: %s\n' "${#BACKED_UP[@]}" "$BACKUP_DIR" >&2
    exit "$code"
}

on_exit() {
    local code=$?
    if [ "$code" -ne 0 ]; then
        rollback "$code"
    fi
    exit "$code"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --yes) ASSUME_YES=1 ;;
        --skip-preflight) SKIP_PREFLIGHT=1 ;;
        --start) START=1 ;;
        --allow-active) ALLOW_ACTIVE=1 ;;
        --force-config) FORCE_CONFIG=1 ;;
        --no-timers) ENABLE_TIMERS=0 ;;
        --config-source)
            shift
            [ "$#" -gt 0 ] || fail "--config-source requires a path"
            CONFIG_SOURCE=$1
            ;;
        --python)
            shift
            [ "$#" -gt 0 ] || fail "--python requires a path"
            PYTHON_BIN=$1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'unknown argument: %s\n' "$1" >&2
            usage
            exit 2
            ;;
    esac
    shift
done

if [ "$DRY_RUN" -eq 0 ]; then
    [ "$(id -u)" -eq 0 ] || fail "run as root, or use --dry-run"
    [ "$ASSUME_YES" -eq 1 ] || fail "refusing to modify the host without --yes"
fi

[ -f "$REPO_ROOT/pyproject.toml" ] || fail "pyproject.toml not found at $REPO_ROOT"
[ -f "$REPO_ROOT/uv.lock" ] || fail "uv.lock not found at $REPO_ROOT"
[ -f "$SCRIPT_DIR/preflight.sh" ] || fail "preflight.sh not found"

if [ -z "$CONFIG_SOURCE" ]; then
    if [ -f "$REPO_ROOT/lab/resources.local.toml" ]; then
        CONFIG_SOURCE=$REPO_ROOT/lab/resources.local.toml
    else
        CONFIG_SOURCE=$REPO_ROOT/lab/resources.example.toml
    fi
fi
[ -f "$CONFIG_SOURCE" ] || fail "config source not found: $CONFIG_SOURCE"

if [ "$SKIP_PREFLIGHT" -eq 0 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        note "would run $SCRIPT_DIR/preflight.sh --config $CONFIG_SOURCE"
    else
        bash "$SCRIPT_DIR/preflight.sh" --config "$CONFIG_SOURCE" || fail "preflight failed"
    fi
else
    warn "preflight skipped by explicit request"
fi

if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.12 python3.11 python3; do
        if have "$candidate"; then
            PYTHON_BIN=$candidate
            break
        fi
    done
fi
[ -n "$PYTHON_BIN" ] || fail "Python >= 3.11 is required; install Python 3.12 first"
have "$PYTHON_BIN" || fail "Python not found: $PYTHON_BIN"
PY_VERSION=$("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    fail "Python $PY_VERSION is too old; Python >= 3.11 is required"
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    warn "Python $PY_VERSION is supported, but 3.12 is the deployment baseline"
fi

CONFIG_MODE=$("$PYTHON_BIN" - "$CONFIG_SOURCE" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    data = tomllib.load(handle)
print(data.get("policy", {}).get("mode", "monitor"))
PY
) || fail "could not parse config: $CONFIG_SOURCE"
CONFIG_DEPLOYMENT_ALLOWED=$("$PYTHON_BIN" - "$CONFIG_SOURCE" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    data = tomllib.load(handle)
print("true" if data.get("policy", {}).get("deployment_allowed", False) else "false")
PY
) || fail "could not parse config: $CONFIG_SOURCE"
CONFIG_ALLOW_ROOT_KEYS=$("$PYTHON_BIN" - "$CONFIG_SOURCE" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    data = tomllib.load(handle)
print("true" if data.get("policy", {}).get("allow_root_ssh_keys", False) else "false")
PY
) || fail "could not parse config: $CONFIG_SOURCE"
if [ "$CONFIG_MODE" = "active" ] && [ "$ALLOW_ACTIVE" -ne 1 ]; then
    fail "config is in active mode; pass --allow-active only after training and approval"
fi
if [ "$CONFIG_DEPLOYMENT_ALLOWED" = "true" ] && [ "$DRY_RUN" -eq 0 ]; then
    warn "config has deployment_allowed=true; starting the broker still requires training completion and explicit approval"
fi

if [ "$DRY_RUN" -eq 0 ]; then
    if systemctl is-active --quiet citespec-resource-broker.service 2>/dev/null; then
        BROKER_WAS_ACTIVE=1
    fi
    if systemctl is-enabled --quiet citespec-resource-broker.service 2>/dev/null; then
        BROKER_WAS_ENABLED=1
    fi
    install -d -o root -g root -m 0700 "$BACKUP_ROOT" \
        || fail "could not secure backup root: $BACKUP_ROOT"
    BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)-$$"
    mkdir -p "$BACKUP_DIR" || fail "could not create backup directory: $BACKUP_DIR"
    chmod 0700 "$BACKUP_DIR" || fail "could not secure backup directory: $BACKUP_DIR"
    ROLLBACK_ARMED=1
    trap on_exit EXIT
    note "transactional backup: $BACKUP_DIR"
else
    note "dry-run: no rollback backup is created"
fi

note "repo: $REPO_ROOT"
note "config source: $CONFIG_SOURCE"
note "python: $PYTHON_BIN ($PY_VERSION)"
note "venv: $VENV"

for group in "${CITESPEC_RESOURCE_GROUPS[@]}"; do
    if getent group "$group" >/dev/null 2>&1; then
        ok "group exists: $group"
    else
        note "creating group: $group"
        run groupadd --system "$group"
    fi
done

if getent group "$INTAKE_USER" >/dev/null 2>&1; then
    ok "group exists: $INTAKE_USER"
else
    note "creating group: $INTAKE_USER"
    run groupadd --system "$INTAKE_USER"
fi
if getent passwd "$INTAKE_USER" >/dev/null 2>&1; then
    ok "account exists: $INTAKE_USER"
else
    note "creating low-privilege intake account: $INTAKE_USER"
    run useradd --system --gid "$INTAKE_USER" --home-dir "$INTAKE_STATE_DIR" \
        --no-create-home --shell /usr/sbin/nologin "$INTAKE_USER"
fi

if [ -n "$(effective_members citespec-gpu)" ]; then
    fail "citespec-gpu has effective members; remove them before installing"
fi
ok "citespec-gpu has no effective members"

while IFS= read -r username; do
    [ -n "$username" ] || continue
    for forbidden in sudo docker citespec-gpu video render citespec-admins; do
        if id -nG "$username" 2>/dev/null | tr ' ' '\n' | grep -qx "$forbidden"; then
            fail "member $username is in forbidden group $forbidden"
        fi
    done
done < <(effective_members citespec-members)
while IFS= read -r username; do
    [ -n "$username" ] || continue
    if id -nG "$username" 2>/dev/null | tr ' ' '\n' | grep -qx citespec-members; then
        fail "admin $username is also in citespec-members; ForceCommand would apply"
    fi
done < <(effective_members citespec-admins)

if [ "$DRY_RUN" -eq 0 ]; then
    run install -d -o root -g root -m 0755 "$PREFIX"
    if have uv; then
        note "installing package with uv sync --frozen --no-dev --no-editable --extra gui"
        run env UV_PROJECT_ENVIRONMENT="$VENV" uv sync \
            --frozen --no-dev --no-editable --extra gui --project "$REPO_ROOT" --python "$PYTHON_BIN"
    else
        warn "uv is not installed; falling back to pip (versions may drift)"
        if [ ! -x "$VENV/bin/python" ]; then
            run "$PYTHON_BIN" -m venv "$VENV"
        fi
        run "$VENV/bin/python" -m pip install --upgrade pip
        run "$VENV/bin/python" -m pip install "$REPO_ROOT[gui]"
    fi
    [ -x "$VENV/bin/python" ] || fail "venv was not created: $VENV"
    [ -x "$VENV/bin/citespec-resource" ] || fail "citespec-resource entry point is missing"
    [ -x "$VENV/bin/citespec-resource-login" ] || fail "citespec-resource-login entry point is missing"
else
    run_shell "install -d -o root -g root -m 0755 $PREFIX"
    if have uv; then
        run_shell "UV_PROJECT_ENVIRONMENT=$VENV uv sync --frozen --no-dev --no-editable --extra gui --project $REPO_ROOT --python $PYTHON_BIN"
    else
        run_shell "$PYTHON_BIN -m venv $VENV"
        run_shell "$VENV/bin/python -m pip install --upgrade pip"
        run_shell "$VENV/bin/python -m pip install $REPO_ROOT[gui]"
    fi
fi

run install -d -o root -g root -m 0755 "$LIBEXEC_DIR"
install_file 0755 "$SCRIPT_DIR/audit_legacy.sh" "$AUDIT_INSTALL"
ok "legacy audit installed: $AUDIT_INSTALL"
install_file 0755 "$SCRIPT_DIR/verify_gpu_permissions.sh" "$GPU_VERIFY_INSTALL"
ok "GPU permission verifier installed: $GPU_VERIFY_INSTALL"
install_file 0755 "$SCRIPT_DIR/member-shell.sh" "$MEMBER_SHELL_INSTALL"
ok "root-owned member shell installed: $MEMBER_SHELL_INSTALL"
install_file 0755 "$SCRIPT_DIR/create_user.sh" "$CREATE_USER_INSTALL"
ok "member account helper installed: $CREATE_USER_INSTALL"
install_file 0755 "$SCRIPT_DIR/membership/provision_executor.py" "$PROVISION_EXECUTOR_INSTALL"
ok "root provisioning executor installed: $PROVISION_EXECUTOR_INSTALL"
install_file 0755 "$SCRIPT_DIR/fix_gpu_permissions.sh" "$GPU_FIX_INSTALL"
ok "GPU device permission fixer installed: $GPU_FIX_INSTALL"

# NVIDIA's nodes on this host are not udev-managed and nvidia-smi chmods them
# back to 0666 root:root on every invocation.  Install the narrow LD_PRELOAD
# shim and wrapper, plus a boot-time fixer, so members never receive host GPU
# device access.  The real helper is kept for a missing-node fallback; its
# setuid/setgid bits are removed because members must not create host nodes.
run install -d -o root -g root -m 0755 "$GPU_SHIM_DIR"
install_file 0644 "$SCRIPT_DIR/nvidia/citespec_nvidia_chmod_shim.c" "$GPU_SHIM_SRC"
if [ "$DRY_RUN" -eq 0 ]; then
    CC_BIN=$(command -v gcc 2>/dev/null || command -v cc 2>/dev/null || true)
    [ -n "$CC_BIN" ] || fail "gcc or cc is required to build the NVIDIA chmod shim"
    backup_file "$GPU_SHIM"
    "$CC_BIN" -shared -fPIC -O2 -Wall -Wextra \
        -o "$GPU_SHIM" "$GPU_SHIM_SRC" -ldl \
        || fail "could not build NVIDIA chmod shim"
    chmod 0644 "$GPU_SHIM" || fail "could not secure NVIDIA chmod shim"
    ok "NVIDIA chmod shim built: $GPU_SHIM"
else
    note "would build NVIDIA chmod shim: $GPU_SHIM"
fi
if [ "$DRY_RUN" -eq 0 ] && [ ! -e "$NVIDIA_SMI_REAL" ] && [ -e "$NVIDIA_SMI" ]; then
    backup_file "$NVIDIA_SMI"
    run mv "$NVIDIA_SMI" "$NVIDIA_SMI_REAL"
fi
install_file 0755 "$SCRIPT_DIR/nvidia/nvidia-smi-wrapper.sh" "$NVIDIA_SMI"
ok "nvidia-smi wrapper installed: $NVIDIA_SMI"
if [ "$DRY_RUN" -eq 0 ] && [ ! -e "$NVIDIA_MODPROBE_REAL" ] && [ -e "$NVIDIA_MODPROBE" ]; then
    backup_file "$NVIDIA_MODPROBE"
    run mv "$NVIDIA_MODPROBE" "$NVIDIA_MODPROBE_REAL"
fi
if [ "$DRY_RUN" -eq 0 ] && [ -e "$NVIDIA_MODPROBE_REAL" ]; then
    run chmod u-s,g-s "$NVIDIA_MODPROBE_REAL"
fi
install_file 0755 "$SCRIPT_DIR/nvidia/nvidia-modprobe-wrapper.sh" "$NVIDIA_MODPROBE"
ok "nvidia-modprobe wrapper installed: $NVIDIA_MODPROBE"

run install -d -o root -g root -m 0755 "$CENTRAL_KEY_DIR"
ok "central member key directory ready: $CENTRAL_KEY_DIR"
run install -d -o root -g root -m 0750 "$PROVISION_SPOOL_DIR"
run install -d -o root -g root -m 0750 "$PROVISION_SPOOL_DIR/requests"
run install -d -o root -g root -m 0750 "$PROVISION_SPOOL_DIR/results"
ok "provisioning spool ready: $PROVISION_SPOOL_DIR"
for member in $(effective_members citespec-members); do
    current_shell=$(getent passwd "$member" | cut -d: -f7)
    if [ "$current_shell" = "$MEMBER_SHELL_INSTALL" ]; then
        ok "member $member already uses the restricted login shell"
    else
        warn "member $member still uses $current_shell; migrate it explicitly (admin members import-legacy, or deploy/create_user.sh --yes --ssh-key <pub> $member) before active mode"
    fi
done

run install -d -o root -g root -m 0755 "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ] && [ "$FORCE_CONFIG" -ne 1 ]; then
    note "keeping existing config: $CONFIG_FILE (use --force-config to replace)"
else
    install_file 0644 "$CONFIG_SOURCE" "$CONFIG_FILE"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    note "would validate $CONFIG_FILE and create its workspace_root"
else
    CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'from citespec_resource.config import load_config; load_config(__import__("os").environ["CITESPEC_RESOURCE_CONFIG"])' \
        || fail "installed config failed validation"
    EFFECTIVE_MODE=$(CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from citespec_resource.config import load_config; print(load_config(os.environ["CITESPEC_RESOURCE_CONFIG"]).policy.mode)')
    if [ "$EFFECTIVE_MODE" = "active" ] && [ "$ALLOW_ACTIVE" -ne 1 ]; then
        fail "installed config is active; pass --allow-active only after approval"
    fi
    ok "config validated"
    WORKSPACE_ROOT=$(CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from citespec_resource.config import load_config; print(load_config(os.environ["CITESPEC_RESOURCE_CONFIG"]).server.workspace_root)') \
        || fail "could not read workspace_root from config"
    run install -d -o root -g root -m 0755 "$WORKSPACE_ROOT"
    ok "workspace root ready: $WORKSPACE_ROOT"
    if [ "$WORKSPACE_ROOT" != "/home/data1/citespec-users" ]; then
        warn "systemd unit ReadWritePaths expects /home/data1/citespec-users; update the unit for $WORKSPACE_ROOT"
    fi
fi

run install -d -o root -g root -m 0750 "$STATE_DIR"
run install -d -o root -g root -m 0700 "$BACKUP_ROOT"
run install -d -o root -g root -m 0750 "$ALERT_LOG_DIR"
run install -d -o "$INTAKE_USER" -g "$INTAKE_USER" -m 0700 "$INTAKE_STATE_DIR"
ok "intake state directory ready: $INTAKE_STATE_DIR (owned by $INTAKE_USER)"
if [ "$DRY_RUN" -eq 1 ]; then
    note "would initialize SQLite; fresh install uses monitor, active leases keep management"
else
    STATE_DB=$(CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from citespec_resource.config import load_config; print(load_config(os.environ["CITESPEC_RESOURCE_CONFIG"]).server.state_db)') \
        || fail "could not read state_db from config"
    if [ -f "$STATE_DB" ]; then
        STATE_DB_EXISTED=1
        STATE_DB_BACKUP="$BACKUP_DIR/state.db"
        "$VENV/bin/python" - "$STATE_DB" "$STATE_DB_BACKUP" <<'PY' || fail "could not back up SQLite state"
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
destination = sqlite3.connect(sys.argv[2])
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
PY
        chmod 0600 "$STATE_DB_BACKUP" || fail "could not secure SQLite backup"
    fi
    MODE=$(umask 077; CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" - <<'PY'
import os

from citespec_resource.config import load_config
from citespec_resource.db import init_db
from citespec_resource import store

config = load_config(os.environ["CITESPEC_RESOURCE_CONFIG"])
connection = init_db(config.server.state_db)
leases = store.active_lease_count(connection)
current = store.get_mode(connection, config.policy.mode)
if leases:
    target = current if current in {"active", "maintenance"} else "maintenance"
    if target == "active" and not config.policy.deployment_allowed:
        target = "maintenance"
else:
    target = "monitor"
if target != current:
    store.set_mode(connection, target, actor_uid=0)
connection.close()
print(target)
PY
    ) || fail "SQLite initialization failed"
    [ -f "$STATE_DB" ] || fail "state database was not created: $STATE_DB"
    chmod 0600 "$STATE_DB" || fail "could not chmod $STATE_DB"
    case "$STATE_DB" in
        /var/lib/citespec-resource/*) ;;
        *) warn "state_db is outside /var/lib/citespec-resource; update systemd ReadWritePaths" ;;
    esac
    ok "SQLite initialized in ${MODE} mode"
fi

if [ "$DRY_RUN" -eq 0 ]; then
    have udevadm || fail "udevadm is required"
fi
install_file 0644 "$SCRIPT_DIR/udev/99-citespec-gpu.rules" "$UDEV_RULE"
if [ "$DRY_RUN" -eq 0 ] && udevadm verify --help >/dev/null 2>&1; then
    udevadm verify "$UDEV_RULE" || fail "udev rule validation failed"
    ok "udev rule validated"
fi
run udevadm control --reload-rules
run udevadm trigger --action=change --sysname-match='nvidia*'
ok "udev rule installed and nvidia* device nodes re-triggered"

install_file 0644 \
    "$SCRIPT_DIR/systemd/citespec-resource-broker.service" "$SYSTEMD_UNIT"
install_file 0644 \
    "$SCRIPT_DIR/systemd/citespec-resource-intake.service" "$INTAKE_UNIT"
install_file 0644 \
    "$SCRIPT_DIR/systemd/citespec-resource-provision.service" "$PROVISION_UNIT"
install_file 0644 \
    "$SCRIPT_DIR/systemd/citespec-resource-provision.path" "$PROVISION_PATH"
install_file 0644 \
    "$SCRIPT_DIR/systemd/citespec-gpu-permissions.service" "$GPU_PERM_UNIT"
if [ "$DRY_RUN" -eq 0 ] && have systemd-analyze; then
    systemd-analyze verify "$SYSTEMD_UNIT" "$INTAKE_UNIT" "$PROVISION_UNIT" \
        "$PROVISION_PATH" "$GPU_PERM_UNIT" || fail "systemd unit validation failed"
    ok "systemd units validated"
fi
if [ -d /run/citespec-resource ]; then
    run chmod 0711 /run/citespec-resource
fi
run systemctl daemon-reload
run systemctl enable citespec-resource-broker.service
run systemctl enable citespec-resource-intake.service
run systemctl enable citespec-resource-provision.path
run systemctl enable citespec-gpu-permissions.service
ok "broker, intake, provisioning, and GPU-permission units installed and enabled"
run "$GPU_FIX_INSTALL"
ok "current GPU device permissions applied"

run install -d -m 0755 /usr/local/bin
backup_file "$LOGIN_WRAPPER"
backup_file "$CLI_LINK"
run ln -sfn "$VENV/bin/citespec-resource-login" "$LOGIN_WRAPPER"
run ln -sfn "$VENV/bin/citespec-resource" "$CLI_LINK"
ok "CLI links installed"

install_file 0755 "$SCRIPT_DIR/rotate_job_logs.py" "$ROTATE_SCRIPT"
install_file 0644 "$SCRIPT_DIR/systemd/citespec-resource-logrotate.service" "$ROTATE_UNIT"
install_file 0644 "$SCRIPT_DIR/systemd/citespec-resource-logrotate.timer" "$ROTATE_TIMER"
install_file 0755 "$SCRIPT_DIR/check_disk.sh" "$DISK_SCRIPT"
install_file 0644 "$SCRIPT_DIR/systemd/citespec-resource-disk-check.service" "$DISK_UNIT"
install_file 0644 "$SCRIPT_DIR/systemd/citespec-resource-disk-check.timer" "$DISK_TIMER"
install_file 0755 "$SCRIPT_DIR/alert.sh" "$ALERT_SCRIPT"
install_file 0644 "$SCRIPT_DIR/systemd/citespec-resource-alert@.service" "$ALERT_UNIT"
install_file 0644 "$SCRIPT_DIR/systemd/citespec-resource-backup.service" "$BACKUP_UNIT"
install_file 0644 "$SCRIPT_DIR/systemd/citespec-resource-backup.timer" "$BACKUP_TIMER"
if [ "$DRY_RUN" -eq 0 ] && have systemd-analyze; then
    systemd-analyze verify "$ROTATE_UNIT" "$ROTATE_TIMER" "$DISK_UNIT" "$DISK_TIMER" \
        "$ALERT_UNIT" "$BACKUP_UNIT" "$BACKUP_TIMER" \
        || fail "log rotation, disk alert, and backup unit validation failed"
fi
if [ -e "$LEGACY_LOGROTATE" ]; then
    backup_file "$LEGACY_LOGROTATE"
    run rm -f -- "$LEGACY_LOGROTATE"
    warn "removed legacy copytruncate rule: $LEGACY_LOGROTATE"
fi
run systemctl daemon-reload

if [ ! -r /etc/ssh/sshd_config ]; then
    warn "/etc/ssh/sshd_config is not readable; skipping sshd drop-in"
else
    if [ "$DRY_RUN" -eq 0 ] && ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
        fail "sshd_config does not include /etc/ssh/sshd_config.d/*.conf"
    fi
    run install -d -m 0755 /etc/ssh/sshd_config.d
    # Render the member-visible banner from the installed config instead of
    # leaving literal {guide_url}/{apply_url} placeholders on a real host.
    if [ "$DRY_RUN" -eq 0 ]; then
        BANNER_TMP=$(mktemp "${TMPDIR:-/tmp}/citespec-banner.XXXXXX") || fail "could not create banner temp file"
        CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" - > "$BANNER_TMP" <<'PY' || fail "could not render SSH banner"
import os

from citespec_resource.config import load_config

config = load_config(os.environ["CITESPEC_RESOURCE_CONFIG"])
onboarding = config.onboarding
contact = f"（{onboarding.support_contact}）" if onboarding.support_contact else ""
print("本服务器已启用个人账号与限时工作环境，旧共享账号不再用于科研登录。")
print(f"新使用方式：{onboarding.guide_url or '请联系资源管理员获取使用指南'}")
print(f"申请个人账号：{onboarding.apply_url or '请联系资源管理员申请'}")
print("已有账号：使用个人 SSH 密钥登录；不会为旧共享账号自动开通权限。")
print(f"如有疑问，请联系：{onboarding.support_label or '资源管理员'}{contact}")
PY
        install_file 0644 "$BANNER_TMP" "$BANNER_FILE"
        rm -f -- "$BANNER_TMP"
        ok "member SSH banner installed: $BANNER_FILE"
    else
        note "would render and install $BANNER_FILE"
    fi
    install_file 0644 "$SCRIPT_DIR/sshd/60-citespec-resource.conf" "$SSHD_DROPIN"
    if [ "$DRY_RUN" -eq 0 ]; then
        SSHD_BIN=$(command -v sshd 2>/dev/null || true)
        [ -n "$SSHD_BIN" ] || SSHD_BIN=/usr/sbin/sshd
        [ -x "$SSHD_BIN" ] || fail "sshd binary not found"
        run "$SSHD_BIN" -t || fail "sshd -t failed; drop-in not reloaded"
        if systemctl list-unit-files --type=service 2>/dev/null | grep -q '^ssh\.service'; then
            run systemctl reload ssh
        else
            run systemctl reload sshd
        fi
        ok "sshd drop-in installed and reloaded"
    else
        note "would run sshd -t and reload the SSH service"
    fi
    warn "keep an existing admin SSH session open until a new member login is verified"
fi

# Run the installed, root-owned audit after all access-control files exist but
# before the broker can be started. This is the pre-activation gate; an active
# config is never allowed to commit with critical legacy access still present.
if [ "$DRY_RUN" -eq 1 ]; then
    note "would run $AUDIT_INSTALL --production"
else
    AUDIT_ARGS=(--production)
    if [ "$CONFIG_ALLOW_ROOT_KEYS" = "true" ]; then
        AUDIT_ARGS+=(--allow-root-ssh-keys)
    fi
    if bash "$AUDIT_INSTALL" "${AUDIT_ARGS[@]}"; then
        ok "legacy audit passed"
    else
        AUDIT_FAILED=1
        warn "legacy audit found critical findings; active install/start is blocked"
    fi
fi
if [ "$CONFIG_MODE" = "active" ] && [ "$AUDIT_FAILED" -eq 1 ]; then
    fail "active config requires a passing legacy audit; revoke legacy access"
fi
if [ "$START" -eq 1 ] && [ "$AUDIT_FAILED" -eq 1 ]; then
    fail "legacy audit failed; resolve critical findings before starting the broker"
fi
if [ "$START" -eq 1 ]; then
    BROKER_START_ATTEMPTED=1
    run systemctl restart citespec-resource-broker.service
    run systemctl restart citespec-resource-intake.service
    run systemctl restart citespec-resource-provision.path
    if [ "$DRY_RUN" -eq 0 ]; then
        sleep 1
        systemctl is-active --quiet citespec-resource-broker.service \
            || fail "broker failed to start; inspect journalctl -u citespec-resource-broker"
        systemctl is-active --quiet citespec-resource-intake.service \
            || fail "intake failed to start; inspect journalctl -u citespec-resource-intake"
        ok "broker and intake started (${MODE:-unknown} mode)"
    fi
else
    note "broker installed but not started; use: systemctl start citespec-resource-broker"
    if [ "$DRY_RUN" -eq 0 ] && systemctl is-active --quiet citespec-resource-broker.service; then
        warn "broker is already running; restart it to apply the installed files"
    fi
fi

if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$ENABLE_TIMERS" -eq 1 ]; then
        note "would enable and start log-rotation, disk-alert, and state-backup timers"
    else
        note "would leave log-rotation, disk-alert, and state-backup timers disabled"
    fi
elif [ "$ENABLE_TIMERS" -eq 1 ]; then
    # Enable timers only after the pre-activation audit and any broker start
    # attempt have succeeded, so a failed active install leaves no timer links.
    run systemctl enable --now citespec-resource-logrotate.timer
    run systemctl enable --now citespec-resource-disk-check.timer
    run systemctl enable --now citespec-resource-backup.timer
    ok "job-log rotation, disk-alert, and state-backup timers enabled"
else
    note "timers left disabled by --no-timers"
fi

if [ "$DRY_RUN" -eq 0 ]; then
    INSTALL_COMPLETE=1
    ok "installation committed; backup retained at $BACKUP_DIR"
fi

printf '\nInstallation complete. Next steps:\n'
printf '  1. create member/admin accounts with deploy/create_user.sh\n'
printf '  2. keep policy.mode=monitor and run deploy/verify_monitor_only.sh\n'
printf '  3. do not switch to active mode until training ends and approval is recorded\n'
