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
VENVS_DIR=$PREFIX/venvs
VENV=$PREFIX/.venv
VENV_STAGING=""
VENV_TARGET=""
VENV_PREVIOUS=""
VENV_PREVIOUS_LINK=""
VENV_PREVIOUS_KIND="none"
VENV_SWITCHED=0
CONFIG_DIR=/etc/citespec-resource
CONFIG_FILE=$CONFIG_DIR/config.toml
STATE_DIR=/var/lib/citespec-resource
# The static unit templates below default to the amax workspace.  A host with a
# different server.workspace_root gets generated drop-ins for every unit that
# names the path; the installer no longer rejects a different root.
SYSTEMD_WORKSPACE_ROOT=/home/data1/citespec-users
# Every unit that names the workspace is overridden when server.workspace_root
# differs from the static amax path.  The broker alone is not enough: the
# log-rotation, disk-check, and provisioning units also embed the path in
# ExecStart/Environment and in their sandbox path lists.
WORKSPACE_DROPIN_ROOT=/etc/systemd/system
BROKER_WORKSPACE_DROPIN=$WORKSPACE_DROPIN_ROOT/citespec-resource-broker.service.d/10-workspace.conf
LOG_WORKSPACE_DROPIN=$WORKSPACE_DROPIN_ROOT/citespec-resource-logrotate.service.d/10-workspace.conf
DISK_WORKSPACE_DROPIN=$WORKSPACE_DROPIN_ROOT/citespec-resource-disk-check.service.d/10-workspace.conf
PROVISION_WORKSPACE_DROPIN=$WORKSPACE_DROPIN_ROOT/citespec-resource-provision.service.d/10-workspace.conf
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
PORTAL_UNIT=/etc/systemd/system/citespec-resource-portal.service
PROVISION_UNIT=/etc/systemd/system/citespec-resource-provision.service
PROVISION_PATH=/etc/systemd/system/citespec-resource-provision.path
CENTRAL_KEY_DIR=/etc/citespec-resource/authorized-keys.d
PROVISION_SPOOL_DIR=/var/lib/citespec-resource/provision
INTAKE_STATE_DIR=/var/lib/citespec-resource-intake
INTAKE_USER=citespec-intake
PORTAL_STATE_DIR=/var/lib/citespec-resource-portal
PORTAL_USER=citespec-portal
PORTAL_SECRET_FILE=/etc/citespec-resource/portal-secret.key
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
PORTAL_AUDIT_INSTALL=$LIBEXEC_DIR/verify_portal_network.sh
PORTAL_AUDIT_PY_INSTALL=$LIBEXEC_DIR/portal_network_audit.py
ENABLE_PORTAL_INSTALL=$LIBEXEC_DIR/enable_portal.sh
GPU_ISOLATION_INSTALL=$LIBEXEC_DIR/verify_gpu_isolation.sh
GPU_SHIM_DIR=/usr/local/lib/citespec-resource
GPU_SHIM="$GPU_SHIM_DIR/libcitespec-nvidia-chmod.so"
GPU_SHIM_SRC="$GPU_SHIM_DIR/citespec_nvidia_chmod_shim.c"
GPU_PERM_UNIT=/etc/systemd/system/citespec-gpu-permissions.service
NVIDIA_SMI=/usr/bin/nvidia-smi
NVIDIA_SMI_REAL=/usr/bin/nvidia-smi.real
NVIDIA_MODPROBE=/usr/bin/nvidia-modprobe
NVIDIA_MODPROBE_REAL=/usr/bin/nvidia-modprobe.real
CITESPEC_RESOURCE_GROUPS=(citespec-members citespec-cli citespec-admins citespec-gpu citespec-portal)

DRY_RUN=0
ASSUME_YES=0
SKIP_PREFLIGHT=0
START=0
ALLOW_ACTIVE=0
MAINTENANCE_WINDOW=0
SERVICES_STOPPED=0
FORCE_CONFIG=0
ENABLE_TIMERS=1
AUDIT_FAILED=0
CONFIG_SOURCE=""
PYTHON_BIN=${PYTHON_BIN:-}
BACKUP_ROOT=/var/backups/citespec-resource
BACKUP_DIR=""
ROLLBACK_ARMED=0
INSTALL_COMPLETE=0
BACKED_UP=()
STATE_DB=""
STATE_DB_BACKUP=""
STATE_DB_EXISTED=0
SNAPSHOT_DIR=""
# Unit/timer state recorded before the installer touches anything.  Rollback
# restores both enablement and active state, not just the broker.
UNIT_NAMES=(
    citespec-resource-provision.path
    citespec-resource-provision.service
    citespec-resource-intake.service
    citespec-resource-broker.service
    citespec-gpu-permissions.service
    citespec-resource-portal.service
)
# Stop public entry points before the executor spool; timers are stopped first
# so nothing is triggered while the rollback is restoring unit files.
UNIT_STOP_ORDER=(
    citespec-resource-broker.service
    citespec-resource-portal.service
    citespec-resource-intake.service
    citespec-resource-provision.path
    citespec-resource-provision.service
    citespec-gpu-permissions.service
)
TIMER_NAMES=(
    citespec-resource-logrotate.timer
    citespec-resource-disk-check.timer
    citespec-resource-backup.timer
)
UNIT_WAS_ACTIVE=()
UNIT_WAS_ENABLED=()
TIMER_WAS_ACTIVE=()
TIMER_WAS_ENABLED=()
NVIDIA_PATHS=(
    /usr/bin/nvidia-smi
    /usr/bin/nvidia-smi.real
    /usr/bin/nvidia-modprobe
    /usr/bin/nvidia-modprobe.real
)
NVIDIA_STATE_FILE=""
ROLLBACK_MISMATCH=0

usage() {
    cat <<'EOF'
usage: install_server.sh [options]

Options:
  --dry-run             Print actions without changing the host.
  --yes                 Required for a real (non-dry-run) installation.
  --skip-preflight      Skip deploy/preflight.sh (not recommended).
  --start               Start the broker and intake after installation (default:
                        install only). The portal is never auto-started.
  --allow-active        Allow an active-mode config after training and approval.
                        A fresh database still starts in monitor; existing active
                        leases are preserved only when deployment_allowed=true.
  --maintenance-window  Required when an existing Citespec unit is active. Stops
                        the managed writers before replacing the Python venv and
                        taking the database snapshot; they remain stopped unless
                        --start is also given.
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

contains() {
    local needle=$1
    shift
    local item
    for item in "$@"; do
        [ "$item" = "$needle" ] && return 0
    done
    return 1
}

unit_is_active() {
    systemctl is-active --quiet "$1" >/dev/null 2>&1
}

unit_is_enabled() {
    systemctl is-enabled --quiet "$1" >/dev/null 2>&1
}

# Record the state rollback must return to before the first host change.  An
# installer that only restores files leaves newly enabled or newly started
# units behind; capture both axes for every unit and timer it manages.
capture_unit_state() {
    local unit
    UNIT_WAS_ACTIVE=()
    UNIT_WAS_ENABLED=()
    TIMER_WAS_ACTIVE=()
    TIMER_WAS_ENABLED=()
    for unit in "${UNIT_NAMES[@]}"; do
        if unit_is_active "$unit"; then
            UNIT_WAS_ACTIVE+=("$unit")
        fi
        if unit_is_enabled "$unit"; then
            UNIT_WAS_ENABLED+=("$unit")
        fi
    done
    for unit in "${TIMER_NAMES[@]}"; do
        if unit_is_active "$unit"; then
            TIMER_WAS_ACTIVE+=("$unit")
        fi
        if unit_is_enabled "$unit"; then
            TIMER_WAS_ENABLED+=("$unit")
        fi
    done
}

stop_managed_units() {
    local unit
    for unit in "${TIMER_NAMES[@]}"; do
        if unit_is_active "$unit"; then
            systemctl stop "$unit" >/dev/null 2>&1 \
                || warn "rollback could not stop $unit"
        fi
    done
    for unit in "${UNIT_STOP_ORDER[@]}"; do
        if unit_is_active "$unit"; then
            systemctl stop "$unit" >/dev/null 2>&1 \
                || warn "rollback could not stop $unit"
        fi
    done
}

# Remove enablement that the installer added while the new unit files still
# exist; otherwise daemon-reload leaves dangling .wants symlinks behind.
disable_new_units() {
    local unit
    for unit in "${UNIT_NAMES[@]}"; do
        if ! contains "$unit" "${UNIT_WAS_ENABLED[@]}" && unit_is_enabled "$unit"; then
            systemctl disable "$unit" >/dev/null 2>&1 \
                || warn "rollback could not remove enablement for $unit"
        fi
    done
    for unit in "${TIMER_NAMES[@]}"; do
        if ! contains "$unit" "${TIMER_WAS_ENABLED[@]}" && unit_is_enabled "$unit"; then
            systemctl disable "$unit" >/dev/null 2>&1 \
                || warn "rollback could not remove enablement for $unit"
        fi
    done
}

# Start/enable exactly the units that were active/enabled before the installer
# ran.  Called after daemon-reload restores the old unit files, so a
# pre-existing unit gets its old ExecStart back.
restore_unit_state() {
    local unit
    for unit in "${UNIT_NAMES[@]}"; do
        if contains "$unit" "${UNIT_WAS_ENABLED[@]}"; then
            if ! unit_is_enabled "$unit"; then
                systemctl enable "$unit" >/dev/null 2>&1 \
                    || warn "rollback could not re-enable $unit"
            fi
        fi
    done
    for unit in "${TIMER_NAMES[@]}"; do
        if contains "$unit" "${TIMER_WAS_ENABLED[@]}"; then
            if ! unit_is_enabled "$unit"; then
                systemctl enable "$unit" >/dev/null 2>&1 \
                    || warn "rollback could not re-enable $unit"
            fi
        fi
    done
    for unit in "${UNIT_WAS_ACTIVE[@]}"; do
        systemctl start "$unit" >/dev/null 2>&1 \
            || warn "rollback could not restart $unit"
    done
    for unit in "${TIMER_WAS_ACTIVE[@]}"; do
        systemctl start "$unit" >/dev/null 2>&1 \
            || warn "rollback could not restart $unit"
    done
}

sha256_file() {
    if have sha256sum; then
        sha256sum -- "$1" 2>/dev/null | awk '{print $1}'
    elif have shasum; then
        shasum -a 256 -- "$1" 2>/dev/null | awk '{print $1}'
    else
        printf 'unavailable\n'
    fi
}

# Capture every NVIDIA helper path, including the ".real" targets the installer
# may create.  Mode %a keeps setuid/setgid bits, and the digest catches a
# restore that silently replaced the original binary.
record_nvidia_state() {
    local path
    [ -n "${NVIDIA_STATE_FILE:-}" ] || return 0
    : > "$NVIDIA_STATE_FILE" || fail "could not create NVIDIA state record"
    for path in "${NVIDIA_PATHS[@]}"; do
        if [ -e "$path" ] || [ -L "$path" ]; then
            printf '%s\texists\t%s\t%s\t%s\n' \
                "$path" \
                "$(stat -c '%a' "$path" 2>/dev/null || printf '?')" \
                "$(stat -c '%u:%g' "$path" 2>/dev/null || printf '?')" \
                "$(sha256_file "$path")" >> "$NVIDIA_STATE_FILE"
        else
            printf '%s\tabsent\t-\t-\t-\n' "$path" >> "$NVIDIA_STATE_FILE"
        fi
    done
}

verify_nvidia_state() {
    local path expected mode owner digest current_mode current_owner current_digest
    [ -n "${NVIDIA_STATE_FILE:-}" ] || return 0
    [ -f "$NVIDIA_STATE_FILE" ] || return 0
    while IFS=$'\t' read -r path expected mode owner digest; do
        [ -n "$path" ] || continue
        if [ "$expected" = "absent" ]; then
            if [ -e "$path" ] || [ -L "$path" ]; then
                warn "rollback mismatch: $path should not exist"
                ROLLBACK_MISMATCH=1
            fi
            continue
        fi
        if [ ! -e "$path" ]; then
            warn "rollback mismatch: $path is missing"
            ROLLBACK_MISMATCH=1
            continue
        fi
        current_mode=$(stat -c '%a' "$path" 2>/dev/null || printf '?')
        current_owner=$(stat -c '%u:%g' "$path" 2>/dev/null || printf '?')
        current_digest=$(sha256_file "$path")
        if [ "$current_mode" != "$mode" ] || [ "$current_owner" != "$owner" ]; then
            warn "rollback mismatch: $path is $current_owner $current_mode, expected $owner $mode"
            ROLLBACK_MISMATCH=1
        fi
        if [ "$digest" != "unavailable" ] && [ "$current_digest" != "$digest" ]; then
            warn "rollback mismatch: $path content differs from the pre-install binary"
            ROLLBACK_MISMATCH=1
        fi
    done < "$NVIDIA_STATE_FILE"
}

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

write_workspace_dropin() {
    local destination=$1
    shift
    local tmp
    tmp=$(mktemp "${TMPDIR:-/tmp}/citespec-workspace.XXXXXX") \
        || fail "could not create workspace drop-in temp file: $destination"
    {
        printf '[Service]\n'
        printf '%s\n' "$@"
    } > "$tmp" || fail "could not write workspace drop-in: $destination"
    install_file 0644 "$tmp" "$destination"
    rm -f -- "$tmp"
}

# The static units hard-code the amax workspace.  When this host uses another
# server.workspace_root, override every place the path appears: the sandbox
# path lists, the log-rotation and disk-check ExecStart, and the provisioning
# executor's managed-root variable.
install_workspace_dropins() {
    local root=$1
    local dropin
    for dropin in "$BROKER_WORKSPACE_DROPIN" "$LOG_WORKSPACE_DROPIN" \
                  "$DISK_WORKSPACE_DROPIN" "$PROVISION_WORKSPACE_DROPIN"; do
        run install -d -o root -g root -m 0755 "$(dirname "$dropin")"
    done
    write_workspace_dropin "$BROKER_WORKSPACE_DROPIN" \
        "ReadWritePaths=$root"
    write_workspace_dropin "$LOG_WORKSPACE_DROPIN" \
        "ExecStart=" \
        "ExecStart=/usr/local/sbin/citespec-rotate-job-logs --workspace-root $root --max-bytes 104857600 --keep 3" \
        "ReadWritePaths=" \
        "ReadWritePaths=$root"
    write_workspace_dropin "$DISK_WORKSPACE_DROPIN" \
        "ExecStart=" \
        "ExecStart=/usr/local/sbin/citespec-check-disk --path $root --warn-percent 85 --crit-percent 95 --min-free-gb 200" \
        "ReadOnlyPaths=" \
        "ReadOnlyPaths=$root"
    write_workspace_dropin "$PROVISION_WORKSPACE_DROPIN" \
        "Environment=CITESPEC_RESOURCE_WORKSPACE_ROOT=$root" \
        "ReadWritePaths=" \
        "ReadWritePaths=/etc /home $root /var/lib/citespec-resource/provision"
}

remove_workspace_dropins() {
    local dropin
    for dropin in "$BROKER_WORKSPACE_DROPIN" "$LOG_WORKSPACE_DROPIN" \
                  "$DISK_WORKSPACE_DROPIN" "$PROVISION_WORKSPACE_DROPIN"; do
        if [ -e "$dropin" ] || [ -L "$dropin" ]; then
            note "removing obsolete workspace drop-in: $dropin"
            backup_file "$dropin"
            run rm -f -- "$dropin"
        fi
    done
}

restore_venv() {
    [ "$VENV_SWITCHED" -eq 1 ] || return 0
    printf '[rollback] restoring previous Python environment\n' >&2
    case "$VENV_PREVIOUS_KIND" in
        link)
            ln -sfn -- "$VENV_PREVIOUS_LINK" "$VENV" \
                || warn "could not restore the previous .venv symlink"
            ;;
        dir)
            rm -f -- "$VENV" || true
            mv -- "$VENV_PREVIOUS" "$VENV" \
                || warn "could not restore the previous .venv directory"
            ;;
        none)
            rm -f -- "$VENV" || true
            ;;
    esac
    # The newly built environment is no longer referenced; keep the previous
    # one (restored above) and discard only the failed release.
    if [ -n "$VENV_TARGET" ] && [ -d "$VENV_TARGET" ]; then
        rm -rf -- "$VENV_TARGET" || true
    fi
    VENV_SWITCHED=0
}

cleanup_venv_staging() {
    if [ -n "$VENV_STAGING" ] && [ -d "$VENV_STAGING" ]; then
        rm -rf -- "$VENV_STAGING" || true
    fi
    # A failure between promoting the staging directory and switching .venv
    # leaves an unreferenced release directory behind.  It can never be the
    # active environment in that state, so discard it.
    if [ "$VENV_SWITCHED" -eq 0 ] && [ -n "$VENV_TARGET" ] && [ -d "$VENV_TARGET" ]; then
        rm -rf -- "$VENV_TARGET" || true
    fi
}

# Replace /opt/citespec-resource/.venv only after the new environment passed its
# smoke test.  A real directory is moved aside; an existing symlink target is
# recorded and recreated on rollback.  Services must already be stopped.
switch_venv() {
    local tmp_link=$VENV.tmp-$$
    if [ -L "$VENV" ]; then
        VENV_PREVIOUS_LINK=$(readlink "$VENV") \
            || fail "could not read the existing .venv symlink"
        VENV_PREVIOUS_KIND=link
    elif [ -d "$VENV" ]; then
        VENV_PREVIOUS="$VENVS_DIR/legacy-$(date +%Y%m%d%H%M%S)-$$"
        mv -- "$VENV" "$VENV_PREVIOUS" \
            || fail "could not move the previous Python environment aside"
        VENV_PREVIOUS_KIND=dir
        VENV_SWITCHED=1
    else
        VENV_PREVIOUS_KIND=none
    fi
    if ! ln -s -- "$VENV_TARGET" "$tmp_link"; then
        restore_venv
        fail "could not create the new .venv symlink"
    fi
    if ! mv -T -- "$tmp_link" "$VENV"; then
        rm -f -- "$tmp_link" || true
        restore_venv
        fail "could not activate the new Python environment"
    fi
    VENV_SWITCHED=1
}

# Read server.state_db from an already-installed config without importing the
# package (the old venv may predate this release).
existing_state_db() {
    local config=$1
    [ -f "$config" ] || return 0
    "$PYTHON_BIN" - "$config" <<'PY'
import sys
import tomllib

try:
    with open(sys.argv[1], "rb") as handle:
        data = tomllib.load(handle)
except (OSError, tomllib.TOMLDecodeError):
    raise SystemExit(0)
value = data.get("server", {}).get("state_db", "")
print(value if isinstance(value, str) else "")
PY
}

# One consistent, verified snapshot of every database and the spool, created
# with the same code path as the scheduled backup.  Writers must already be
# stopped; --no-quiesce prevents this invocation from restarting them.
take_preupgrade_snapshot() {
    local state_db=$1
    local parent="$BACKUP_DIR/snapshots"
    local output
    [ -n "$state_db" ] && [ -f "$state_db" ] || return 0
    [ -n "${SNAPSHOT_PYTHON:-}" ] && [ -x "$SNAPSHOT_PYTHON" ] \
        || fail "cannot take a consistent pre-upgrade snapshot: installed Python environment is missing"

    run install -d -o root -g root -m 0700 "$parent"
    note "taking consistent pre-upgrade snapshot (state, intake, portal, spool)"
    output=$(PYTHONPATH="$REPO_ROOT/src" "$SNAPSHOT_PYTHON" -m citespec_resource.backup backup \
        --config "$CONFIG_FILE" \
        --backup-root "$parent" \
        --keep 0 \
        --no-quiesce \
        --json) || fail "pre-upgrade snapshot failed; refusing to continue"
    SNAPSHOT_DIR=$(printf '%s' "$output" | "$PYTHON_BIN" -c \
        'import json, sys; print(json.load(sys.stdin)["backup"])') \
        || fail "could not parse pre-upgrade snapshot output"
    [ -f "$SNAPSHOT_DIR/manifest.json" ] \
        || fail "pre-upgrade snapshot has no manifest: $SNAPSHOT_DIR"
    ok "pre-upgrade snapshot: $SNAPSHOT_DIR"
}

restore_preupgrade_snapshot() {
    [ -n "${SNAPSHOT_DIR:-}" ] || return 0
    [ -f "$SNAPSHOT_DIR/manifest.json" ] || {
        warn "pre-upgrade snapshot is missing its manifest: $SNAPSHOT_DIR"
        return 0
    }
    printf '[rollback] restoring databases and spool from %s\n' "$SNAPSHOT_DIR" >&2
    # Use the restored (old) environment for its dependencies; the source tree
    # supplies this release's restore implementation.
    PYTHONPATH="$REPO_ROOT/src" "$VENV/bin/python" -m citespec_resource.backup restore \
        "$SNAPSHOT_DIR" \
        --config "$CONFIG_FILE" \
        --yes \
        --json >/dev/null \
        || warn "could not restore the pre-upgrade snapshot automatically; restore manually with: PYTHONPATH=$REPO_ROOT/src $VENV/bin/python -m citespec_resource.backup restore $SNAPSHOT_DIR --config $CONFIG_FILE --yes"
}

rollback() {
    local code=${1:-$?}
    local path
    local rel
    local broker_active=0
    if [ "$INSTALL_COMPLETE" -eq 1 ] || [ "$ROLLBACK_ARMED" -eq 0 ]; then
        return "$code"
    fi
    trap - EXIT INT TERM HUP
    printf '\n[rollback] installation failed (exit %s); restoring previous state\n' "$code" >&2
    # Timers first, then every service this installer may have started.  Without
    # this a newly enabled timer/service keeps running against restored files.
    stop_managed_units
    disable_new_units
    # Put the old interpreter back before restoring files that its service
    # units point at.  A failed upgrade must never leave old units running a
    # partially updated environment.
    restore_venv
    cleanup_venv_staging
    for path in "${BACKED_UP[@]}"; do
        rel=${path#/}
        if [ -f "$BACKUP_DIR/$rel.absent" ]; then
            rm -f -- "$path" || true
        elif [ -e "$BACKUP_DIR/$rel" ] || [ -L "$BACKUP_DIR/$rel" ]; then
            mkdir -p -- "$(dirname "$path")" || true
            cp -a -- "$BACKUP_DIR/$rel" "$path" || true
        fi
    done
    if unit_is_active citespec-resource-broker.service; then
        broker_active=1
    fi
    if [ -n "$SNAPSHOT_DIR" ]; then
        if [ "$broker_active" -eq 0 ]; then
            restore_preupgrade_snapshot
        else
            warn "broker is still running; leaving the consistent snapshot unrestored"
        fi
    elif [ -n "$STATE_DB" ] && [ "$broker_active" -eq 0 ]; then
        if [ "$STATE_DB_EXISTED" -eq 1 ] && [ -f "$STATE_DB_BACKUP" ]; then
            rm -f -- "$STATE_DB-wal" "$STATE_DB-shm" || true
            cp -a -- "$STATE_DB_BACKUP" "$STATE_DB" || true
        elif [ "$STATE_DB_EXISTED" -eq 0 ]; then
            rm -f -- "$STATE_DB" "$STATE_DB-wal" "$STATE_DB-shm" || true
        fi
    elif [ -n "$STATE_DB" ]; then
        warn "broker is still running; leaving SQLite state in place during rollback"
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    restore_unit_state
    verify_nvidia_state
    udevadm control --reload-rules >/dev/null 2>&1 || true
    udevadm trigger --action=change --sysname-match='nvidia*' >/dev/null 2>&1 || true
    reload_sshd
    if [ "$ROLLBACK_MISMATCH" -ne 0 ]; then
        warn "rollback completed with state mismatches; inspect $BACKUP_DIR before retrying"
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

# An interrupted installer must roll back just like a failed command: TERM/INT/
# HUP arrive during package sync or unit replacement, not only as a command
# failure.  128+signal preserves the conventional exit status.
on_signal() {
    local code=$1
    trap - INT TERM HUP
    rollback "$code"
    exit "$code"
}

# Test hook: source this file with CITESPEC_INSTALL_LIB_ONLY=1 to exercise the
# rollback helpers without touching the host.  Real runs continue below.
if [ "${CITESPEC_INSTALL_LIB_ONLY:-0}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --yes) ASSUME_YES=1 ;;
        --skip-preflight) SKIP_PREFLIGHT=1 ;;
        --start) START=1 ;;
        --allow-active) ALLOW_ACTIVE=1 ;;
        --maintenance-window) MAINTENANCE_WINDOW=1 ;;
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
CONFIG_PORTAL_ENABLED=$("$PYTHON_BIN" - "$CONFIG_SOURCE" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    data = tomllib.load(handle)
print("true" if data.get("portal", {}).get("enabled", False) else "false")
PY
) || fail "could not parse config: $CONFIG_SOURCE"
if [ "$CONFIG_MODE" = "active" ] && [ "$ALLOW_ACTIVE" -ne 1 ]; then
    fail "config is in active mode; pass --allow-active only after training and approval"
fi
if [ "$CONFIG_DEPLOYMENT_ALLOWED" = "true" ] && [ "$DRY_RUN" -eq 0 ]; then
    warn "config has deployment_allowed=true; starting the broker still requires training completion and explicit approval"
fi

if [ "$DRY_RUN" -eq 0 ]; then
    install -d -o root -g root -m 0700 "$BACKUP_ROOT" \
        || fail "could not secure backup root: $BACKUP_ROOT"
    BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)-$$"
    mkdir -p "$BACKUP_DIR" || fail "could not create backup directory: $BACKUP_DIR"
    chmod 0700 "$BACKUP_DIR" || fail "could not secure backup directory: $BACKUP_DIR"
    capture_unit_state
    NVIDIA_STATE_FILE="$BACKUP_DIR/nvidia-state.tsv"
    record_nvidia_state
    ROLLBACK_ARMED=1
    trap on_exit EXIT
    trap 'on_signal 129' HUP
    trap 'on_signal 130' INT
    trap 'on_signal 143' TERM
    note "transactional backup: $BACKUP_DIR"
    note "unit rollback state: ${UNIT_WAS_ACTIVE[*]:-none active}; ${UNIT_WAS_ENABLED[*]:-none enabled}"
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

if getent passwd "$PORTAL_USER" >/dev/null 2>&1; then
    ok "account exists: $PORTAL_USER"
else
    note "creating low-privilege portal account: $PORTAL_USER"
    run useradd --system --gid "$PORTAL_USER" --groups citespec-cli \
        --home-dir "$PORTAL_STATE_DIR" --no-create-home \
        --shell /usr/sbin/nologin "$PORTAL_USER"
fi
if id -nG "$PORTAL_USER" 2>/dev/null | tr ' ' '\n' | grep -qx citespec-cli; then
    ok "$PORTAL_USER may connect to the broker socket via citespec-cli"
else
    note "adding $PORTAL_USER to citespec-cli for broker socket access"
    run usermod -aG citespec-cli "$PORTAL_USER"
fi
if id -nG "$PORTAL_USER" 2>/dev/null | tr ' ' '\n' | grep -qx citespec-admins; then
    fail "$PORTAL_USER must not be a member of citespec-admins"
fi
PORTAL_GROUP_MEMBERS=$(getent group "$PORTAL_USER" 2>/dev/null | cut -d: -f4 || true)
if [ -n "$PORTAL_GROUP_MEMBERS" ]; then
    fail "group $PORTAL_USER has human members: $PORTAL_GROUP_MEMBERS; remove them before installing"
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
    # An upgrade replaces the interpreter and migrates databases.  Refuse to do
    # that behind the back of a running broker: the operator must acknowledge a
    # maintenance window, and every writer is stopped before the snapshot.
    if [ "${#UNIT_WAS_ACTIVE[@]}" -gt 0 ] || [ "${#TIMER_WAS_ACTIVE[@]}" -gt 0 ]; then
        if [ "$MAINTENANCE_WINDOW" -ne 1 ]; then
            fail "existing Citespec units are active; re-run with --maintenance-window to stop writers and take a consistent snapshot"
        fi
        note "maintenance window: stopping managed writers before replacing the environment"
        stop_managed_units
        SERVICES_STOPPED=1
    fi
    if [ -f "$CONFIG_FILE" ]; then
        EXISTING_STATE_DB=$(existing_state_db "$CONFIG_FILE") \
            || fail "could not read server.state_db from $CONFIG_FILE"
        if [ -n "$EXISTING_STATE_DB" ] && [ -f "$EXISTING_STATE_DB" ]; then
            SNAPSHOT_PYTHON="$VENV/bin/python"
            take_preupgrade_snapshot "$EXISTING_STATE_DB"
        fi
    fi

    run install -d -o root -g root -m 0755 "$PREFIX"
    run install -d -o root -g root -m 0755 "$VENVS_DIR"
    RELEASE_VERSION=$("$PYTHON_BIN" - "$REPO_ROOT/pyproject.toml" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
PY
    ) || fail "could not read the project version"
    VENV_TARGET="$VENVS_DIR/venv-$RELEASE_VERSION-$(date +%Y%m%d%H%M%S)-$$"
    VENV_STAGING="$VENVS_DIR/.staging-$RELEASE_VERSION-$(date +%Y%m%d%H%M%S)-$$"
    if have uv; then
        note "installing package into $VENV_STAGING with uv sync --frozen --no-dev --no-editable --extra gui --extra portal"
        run env UV_PROJECT_ENVIRONMENT="$VENV_STAGING" uv sync \
            --frozen --no-dev --no-editable --extra gui --extra portal --project "$REPO_ROOT" --python "$PYTHON_BIN"
    else
        warn "uv is not installed; falling back to pip (versions may drift)"
        run "$PYTHON_BIN" -m venv "$VENV_STAGING"
        run "$VENV_STAGING/bin/python" -m pip install --upgrade pip
        run "$VENV_STAGING/bin/python" -m pip install "$REPO_ROOT[gui,portal]"
    fi
    # Smoke-test the release before it becomes visible through .venv.  A failed
    # dependency sync must leave the previous environment and services intact.
    [ -x "$VENV_STAGING/bin/python" ] || fail "venv was not created: $VENV_STAGING"
    "$VENV_STAGING/bin/python" -c 'import citespec_resource' \
        || fail "new Python environment cannot import citespec_resource"
    [ -x "$VENV_STAGING/bin/citespec-resource" ] || fail "citespec-resource entry point is missing"
    [ -x "$VENV_STAGING/bin/citespec-resource-login" ] || fail "citespec-resource-login entry point is missing"
    "$VENV_STAGING/bin/citespec-resource" version >/dev/null \
        || fail "new Python environment failed its version smoke test"
    if [ -x "$VENV_STAGING/bin/python" ] && "$VENV_STAGING/bin/python" -m pip --version >/dev/null 2>&1; then
        "$VENV_STAGING/bin/python" -m pip check >/dev/null \
            || fail "new Python environment has an inconsistent dependency set"
    fi
    mv -- "$VENV_STAGING" "$VENV_TARGET" \
        || fail "could not promote the new Python environment"
    VENV_STAGING=""
    switch_venv
    ok "activated Python environment: $VENV_TARGET"
    [ -x "$VENV/bin/python" ] || fail "venv was not activated: $VENV"
    [ -x "$VENV/bin/citespec-resource" ] || fail "citespec-resource entry point is missing"
    [ -x "$VENV/bin/citespec-resource-login" ] || fail "citespec-resource-login entry point is missing"
else
    run_shell "install -d -o root -g root -m 0755 $PREFIX"
    run_shell "install -d -o root -g root -m 0755 $VENVS_DIR"
    if have uv; then
        run_shell "UV_PROJECT_ENVIRONMENT=$VENVS_DIR/venv-<version> uv sync --frozen --no-dev --no-editable --extra gui --extra portal --project $REPO_ROOT --python $PYTHON_BIN"
        run_shell "ln -sfn $VENVS_DIR/venv-<version> $VENV"
    else
        run_shell "$PYTHON_BIN -m venv $VENVS_DIR/venv-<version>"
        run_shell "$VENVS_DIR/venv-<version>/bin/python -m pip install $REPO_ROOT[gui,portal]"
        run_shell "ln -sfn $VENVS_DIR/venv-<version> $VENV"
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
install_file 0755 "$SCRIPT_DIR/verify_gpu_isolation.sh" "$GPU_ISOLATION_INSTALL"
ok "GPU isolation verifier installed: $GPU_ISOLATION_INSTALL"
install_file 0755 "$SCRIPT_DIR/verify_portal_network.sh" "$PORTAL_AUDIT_INSTALL"
install_file 0644 "$SCRIPT_DIR/portal_network_audit.py" "$PORTAL_AUDIT_PY_INSTALL"
install_file 0755 "$SCRIPT_DIR/enable_portal.sh" "$ENABLE_PORTAL_INSTALL"
ok "Portal network acceptance tooling installed: $ENABLE_PORTAL_INSTALL"

# NVIDIA's nodes on this host are not udev-managed and nvidia-smi chmods them
# back to 0666 root:root on every invocation.  Install the narrow LD_PRELOAD
# shim and wrapper, plus a boot-time fixer, so members never receive host GPU
# device access.  The real helper is kept for a missing-node fallback; its
# setuid/setgid bits are removed because members must not create host nodes.
run install -d -o root -g root -m 0755 "$GPU_SHIM_DIR"
install_file 0644 "$SCRIPT_DIR/nvidia/citespec_nvidia_chmod_shim.c" "$GPU_SHIM_SRC"
if [ "$DRY_RUN" -eq 0 ]; then
    # Record the .real targets before the move so rollback removes one that did
    # not exist before, or restores the exact binary that did.
    backup_file "$NVIDIA_SMI_REAL"
    backup_file "$NVIDIA_MODPROBE_REAL"
fi
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
run install -d -o root -g root -m 0700 "$PROVISION_SPOOL_DIR/processing"
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
    note "would validate $CONFIG_FILE, create its workspace_root, and install workspace unit drop-ins if it differs"
else
    CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'from citespec_resource.config import load_config; load_config(__import__("os").environ["CITESPEC_RESOURCE_CONFIG"])' \
        || fail "installed config failed validation"
    EFFECTIVE_MODE=$(CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from citespec_resource.config import load_config; print(load_config(os.environ["CITESPEC_RESOURCE_CONFIG"]).policy.mode)')
    if [ "$EFFECTIVE_MODE" = "active" ] && [ "$ALLOW_ACTIVE" -ne 1 ]; then
        fail "installed config is active; pass --allow-active only after approval"
    fi
    CONFIG_PORTAL_ENABLED=$(CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from citespec_resource.config import load_config; print("true" if load_config(os.environ["CITESPEC_RESOURCE_CONFIG"]).portal.enabled else "false")') \
        || fail "could not read portal.enabled from config"
    ok "config validated"
    WORKSPACE_ROOT=$(CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from citespec_resource.config import load_config; print(load_config(os.environ["CITESPEC_RESOURCE_CONFIG"]).server.workspace_root)') \
        || fail "could not read workspace_root from config"
    WORKSPACE_MATCH=$(CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" - "$SYSTEMD_WORKSPACE_ROOT" <<'PY'
import os
import sys
from pathlib import Path

from citespec_resource.config import load_config

actual = Path(load_config(os.environ["CITESPEC_RESOURCE_CONFIG"]).server.workspace_root)
expected = Path(sys.argv[1])
if actual.exists() and expected.exists():
    same = os.path.samefile(actual, expected)
else:
    same = actual.resolve() == expected.resolve()
print("true" if same else "false")
PY
) || fail "could not validate workspace_root against the systemd unit path"
    if [ "$WORKSPACE_MATCH" != "true" ]; then
        note "workspace root '$WORKSPACE_ROOT' differs from the static systemd path; installing unit drop-ins"
        install_workspace_dropins "$WORKSPACE_ROOT"
        ok "workspace systemd drop-ins installed for broker, logrotate, disk-check, and provision"
    else
        remove_workspace_dropins
    fi
    run install -d -o root -g root -m 0755 "$WORKSPACE_ROOT"
    ok "workspace root ready: $WORKSPACE_ROOT"
fi

run install -d -o root -g root -m 0750 "$STATE_DIR"
run install -d -o root -g root -m 0700 "$BACKUP_ROOT"
run install -d -o root -g root -m 0750 "$ALERT_LOG_DIR"
run install -d -o "$INTAKE_USER" -g "$INTAKE_USER" -m 0700 "$INTAKE_STATE_DIR"
ok "intake state directory ready: $INTAKE_STATE_DIR (owned by $INTAKE_USER)"
run install -d -o "$PORTAL_USER" -g "$PORTAL_USER" -m 0750 "$PORTAL_STATE_DIR"
ok "portal state directory ready: $PORTAL_STATE_DIR (owned by $PORTAL_USER)"

# The portal secret encrypts TOTP seeds at rest.  root owns it so the service
# cannot rewrite its own key material; the portal group has read-only access.
if [ "$DRY_RUN" -eq 1 ]; then
    note "would create/secure $PORTAL_SECRET_FILE (root:$PORTAL_USER 0640)"
else
    backup_file "$PORTAL_SECRET_FILE"
    if [ ! -f "$PORTAL_SECRET_FILE" ]; then
        (umask 077; "$VENV/bin/python" - "$PORTAL_SECRET_FILE" <<'PY'
import base64
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if path.exists():
    raise SystemExit(0)
path.write_text(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii") + "\n", encoding="ascii")
PY
        ) || fail "could not create portal secret: $PORTAL_SECRET_FILE"
    fi
    run chown root:"$PORTAL_USER" "$PORTAL_SECRET_FILE"
    run chmod 0640 "$PORTAL_SECRET_FILE"
    ok "portal secret ready: $PORTAL_SECRET_FILE (root:$PORTAL_USER 0640)"
fi

# Broker administrator TOTP secrets are encrypted with this root-only key.  The
# portal service must never be able to read or replace it.  Preserve an existing
# key on reinstall; rotating it would make enrolled administrator credentials
# undecryptable.
if [ "$DRY_RUN" -eq 1 ]; then
    note "would create/secure ${ADMIN_AUTH_KEY_FILE:-/etc/citespec-resource/broker-admin.key} (root:root 0600)"
else
    ADMIN_AUTH_KEY_FILE=$(CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from citespec_resource.config import load_config; print(load_config(os.environ["CITESPEC_RESOURCE_CONFIG"]).server.admin_auth_key_file)') \
        || fail "could not read server.admin_auth_key_file from config"
    case "$ADMIN_AUTH_KEY_FILE" in
        /etc/citespec-resource/*) ;;
        *) fail "server.admin_auth_key_file must be under /etc/citespec-resource" ;;
    esac
    if [ -d "$ADMIN_AUTH_KEY_FILE" ]; then
        fail "server.admin_auth_key_file must name a file, not a directory: $ADMIN_AUTH_KEY_FILE"
    fi
    if [ -L "$ADMIN_AUTH_KEY_FILE" ]; then
        fail "server.admin_auth_key_file must not be a symbolic link: $ADMIN_AUTH_KEY_FILE"
    fi
    ADMIN_AUTH_KEY_DIR=$(dirname "$ADMIN_AUTH_KEY_FILE")
    if [ ! -d "$ADMIN_AUTH_KEY_DIR" ]; then
        run install -d -o root -g root -m 0700 "$ADMIN_AUTH_KEY_DIR"
    fi
    backup_file "$ADMIN_AUTH_KEY_FILE"
    if [ ! -f "$ADMIN_AUTH_KEY_FILE" ]; then
        (umask 077; "$VENV/bin/python" - "$ADMIN_AUTH_KEY_FILE" <<'PY'
import base64
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if path.exists():
    raise SystemExit(0)
path.write_text(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii") + "\n", encoding="ascii")
PY
        ) || fail "could not create broker administrator key: $ADMIN_AUTH_KEY_FILE"
    fi
    run chown root:root "$ADMIN_AUTH_KEY_FILE"
    run chmod 0600 "$ADMIN_AUTH_KEY_FILE"
    ok "broker administrator key ready: $ADMIN_AUTH_KEY_FILE (root:root 0600)"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    note "would initialize the intake and portal SQLite databases"
else
    INTAKE_DB=$(CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from citespec_resource.config import load_config; print(load_config(os.environ["CITESPEC_RESOURCE_CONFIG"]).onboarding.intake_db)') \
        || fail "could not read onboarding.intake_db from config"
    run install -d -o "$INTAKE_USER" -g "$INTAKE_USER" -m 0700 "$(dirname "$INTAKE_DB")"
    backup_file "$INTAKE_DB"
    backup_file "$INTAKE_DB-wal"
    backup_file "$INTAKE_DB-shm"
    (umask 077; "$VENV/bin/python" - "$INTAKE_DB" <<'PY'
import sys

from citespec_resource.enrollment import store

connection = store.init_intake_db(sys.argv[1])
connection.close()
PY
    ) || fail "intake SQLite initialization failed"
    [ -f "$INTAKE_DB" ] || fail "intake database was not created: $INTAKE_DB"
    run chown "$INTAKE_USER":"$INTAKE_USER" "$INTAKE_DB"
    run chmod 0600 "$INTAKE_DB"
    for suffix in -wal -shm; do
        if [ -f "$INTAKE_DB$suffix" ]; then
            run chown "$INTAKE_USER":"$INTAKE_USER" "$INTAKE_DB$suffix"
            run chmod 0600 "$INTAKE_DB$suffix"
        fi
    done
    ok "intake SQLite initialized: $INTAKE_DB"

    PORTAL_DB=$(CITESPEC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from citespec_resource.config import load_config; print(load_config(os.environ["CITESPEC_RESOURCE_CONFIG"]).portal.db_path)') \
        || fail "could not read portal.db_path from config"
    run install -d -o "$PORTAL_USER" -g "$PORTAL_USER" -m 0750 "$(dirname "$PORTAL_DB")"
    backup_file "$PORTAL_DB"
    backup_file "$PORTAL_DB-wal"
    backup_file "$PORTAL_DB-shm"
    (umask 077; "$VENV/bin/python" - "$PORTAL_DB" <<'PY'
import sys

from citespec_resource.portal import store

connection = store.init_portal_db(sys.argv[1])
connection.close()
PY
    ) || fail "portal SQLite initialization failed"
    [ -f "$PORTAL_DB" ] || fail "portal database was not created: $PORTAL_DB"
    run chown "$PORTAL_USER":"$PORTAL_USER" "$PORTAL_DB"
    run chmod 0600 "$PORTAL_DB"
    for suffix in -wal -shm; do
        if [ -f "$PORTAL_DB$suffix" ]; then
            run chown "$PORTAL_USER":"$PORTAL_USER" "$PORTAL_DB$suffix"
            run chmod 0600 "$PORTAL_DB$suffix"
        fi
    done
    case "$PORTAL_DB" in
        /var/lib/citespec-resource-portal/*) ;;
        *) warn "portal.db_path is outside $PORTAL_STATE_DIR; update systemd ReadWritePaths" ;;
    esac
    ok "portal SQLite initialized: $PORTAL_DB"
fi

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
    "$SCRIPT_DIR/systemd/citespec-resource-portal.service" "$PORTAL_UNIT"
install_file 0644 \
    "$SCRIPT_DIR/systemd/citespec-resource-provision.service" "$PROVISION_UNIT"
install_file 0644 \
    "$SCRIPT_DIR/systemd/citespec-resource-provision.path" "$PROVISION_PATH"
install_file 0644 \
    "$SCRIPT_DIR/systemd/citespec-gpu-permissions.service" "$GPU_PERM_UNIT"
if [ "$DRY_RUN" -eq 0 ] && have systemd-analyze; then
    systemd-analyze verify "$SYSTEMD_UNIT" "$INTAKE_UNIT" "$PORTAL_UNIT" \
        "$PROVISION_UNIT" "$PROVISION_PATH" "$GPU_PERM_UNIT" \
        || fail "systemd unit validation failed"
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
if [ "$CONFIG_PORTAL_ENABLED" = "true" ]; then
    # Never enable the unit here: enablement alone makes it start on the next
    # boot, before DNS/TLS/nginx/VPN acceptance.  The unit also has a
    # ConditionPathExists gate; deploy/enable_portal.sh writes the marker only
    # after a fail-closed audit.
    note "portal unit installed but NOT enabled; run $ENABLE_PORTAL_INSTALL --yes --approved-cidr <CIDR> after DNS/VPN/TLS acceptance"
else
    note "portal unit installed but left disabled because [portal].enabled=false"
fi
ok "broker, intake, provisioning, GPU-permission, and portal units installed"
run "$GPU_FIX_INSTALL"
ok "current GPU device permissions applied"
if [ "$DRY_RUN" -eq 0 ]; then
    # Safe probe point: the wrapper/shim exist and the broker-only policy has
    # been applied.  The probe fails if nvidia-smi changes device mode/owner.
    bash "$SCRIPT_DIR/preflight.sh" --probe-nvidia --nvidia-only \
        || fail "post-install NVIDIA probe failed; do not start the broker"
    ok "post-install NVIDIA probe passed"
fi

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
    if [ "$CONFIG_PORTAL_ENABLED" = "true" ]; then
        note "portal remains disabled until the network acceptance gate is passed"
    fi
else
    note "broker installed but not started; use: systemctl start citespec-resource-broker"
    if [ "$SERVICES_STOPPED" -eq 1 ]; then
        note "previously active Citespec units remain stopped; start them explicitly after verification"
    fi
    if [ "$CONFIG_PORTAL_ENABLED" = "true" ]; then
        note "portal is not enabled; run $ENABLE_PORTAL_INSTALL after network acceptance"
    else
        note "portal disabled; use: systemctl start citespec-resource-portal after enabling [portal]"
    fi
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
printf '  4. install the nginx vhost and start the portal only after DNS/VPN/TLS acceptance\n'
