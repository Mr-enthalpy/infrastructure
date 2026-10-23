#!/usr/bin/env bash
# Idempotent server installer for udc-resource.
#
# This script never starts GPU allocations and never touches existing
# containers. It installs the package, groups, udev rule, systemd unit, sshd
# drop-in, configuration, and SQLite state in monitor mode.
set -u
set -o pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

PREFIX=/opt/udc-resource
VENV=$PREFIX/.venv
CONFIG_DIR=/etc/udc-resource
CONFIG_FILE=$CONFIG_DIR/config.toml
STATE_DIR=/var/lib/udc-resource
SYSTEMD_UNIT=/etc/systemd/system/udc-resource-broker.service
UDEV_RULE=/etc/udev/rules.d/99-udc-gpu.rules
SSHD_DROPIN=/etc/ssh/sshd_config.d/60-udc-resource.conf
LOGIN_WRAPPER=/usr/local/bin/udc-resource-login
CLI_LINK=/usr/local/bin/udc-resource
UDC_GROUPS=(udc-members udc-cli udc-admins udc-gpu)

DRY_RUN=0
ASSUME_YES=0
SKIP_PREFLIGHT=0
START=0
ALLOW_ACTIVE=0
FORCE_CONFIG=0
AUDIT_FAILED=0
CONFIG_SOURCE=""
PYTHON_BIN=${PYTHON_BIN:-}

usage() {
    cat <<'EOF'
usage: install_server.sh [options]

Options:
  --dry-run             Print actions without changing the host.
  --yes                 Required for a real (non-dry-run) installation.
  --skip-preflight      Skip deploy/preflight.sh (not recommended).
  --start               Start the broker after installation (default: install only).
  --allow-active        Allow an active-mode config; the broker is still set to monitor.
  --force-config        Overwrite an existing /etc/udc-resource/config.toml.
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

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --yes) ASSUME_YES=1 ;;
        --skip-preflight) SKIP_PREFLIGHT=1 ;;
        --start) START=1 ;;
        --allow-active) ALLOW_ACTIVE=1 ;;
        --force-config) FORCE_CONFIG=1 ;;
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

if [ "$SKIP_PREFLIGHT" -eq 0 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        note "would run $SCRIPT_DIR/preflight.sh"
    else
        bash "$SCRIPT_DIR/preflight.sh" || fail "preflight failed"
    fi
else
    warn "preflight skipped by explicit request"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    note "would run $SCRIPT_DIR/audit_legacy.sh"
else
    if bash "$SCRIPT_DIR/audit_legacy.sh"; then
        ok "legacy audit passed"
    else
        AUDIT_FAILED=1
        warn "legacy audit found critical findings; install may continue, but --start is blocked"
    fi
fi

if [ -z "$CONFIG_SOURCE" ]; then
    if [ -f "$REPO_ROOT/lab/resources.local.toml" ]; then
        CONFIG_SOURCE=$REPO_ROOT/lab/resources.local.toml
    else
        CONFIG_SOURCE=$REPO_ROOT/lab/resources.example.toml
    fi
fi
[ -f "$CONFIG_SOURCE" ] || fail "config source not found: $CONFIG_SOURCE"

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
if [ "$CONFIG_MODE" = "active" ] && [ "$ALLOW_ACTIVE" -ne 1 ]; then
    fail "config is in active mode; pass --allow-active only after training and approval"
fi
if [ "$CONFIG_DEPLOYMENT_ALLOWED" = "true" ] && [ "$DRY_RUN" -eq 0 ]; then
    warn "config has deployment_allowed=true; broker mode will still be forced to monitor"
fi

note "repo: $REPO_ROOT"
note "config source: $CONFIG_SOURCE"
note "python: $PYTHON_BIN ($PY_VERSION)"
note "venv: $VENV"

for group in "${UDC_GROUPS[@]}"; do
    if getent group "$group" >/dev/null 2>&1; then
        ok "group exists: $group"
    else
        note "creating group: $group"
        run groupadd --system "$group"
    fi
done

if getent group udc-gpu 2>/dev/null | awk -F: '$4 != "" {found=1} END {exit found ? 0 : 1}'; then
    fail "udc-gpu has members; remove them before installing"
fi
ok "udc-gpu has no members"

for username in $(getent group udc-members 2>/dev/null | awk -F: '{print $4}' | tr ',' ' '); do
    [ -n "$username" ] || continue
    for forbidden in sudo docker udc-gpu video render udc-admins; do
        if id -nG "$username" 2>/dev/null | grep -qw "$forbidden"; then
            fail "member $username is in forbidden group $forbidden"
        fi
    done
done
for username in $(getent group udc-admins 2>/dev/null | awk -F: '{print $4}' | tr ',' ' '); do
    [ -n "$username" ] || continue
    if id -nG "$username" 2>/dev/null | grep -qw udc-members; then
        fail "admin $username is also in udc-members; ForceCommand would apply"
    fi
done

if [ "$DRY_RUN" -eq 0 ]; then
    run install -d -o root -g root -m 0755 "$PREFIX"
    if have uv; then
        note "installing package with uv sync --frozen --no-dev --no-editable"
        run env UV_PROJECT_ENVIRONMENT="$VENV" uv sync \
            --frozen --no-dev --no-editable --project "$REPO_ROOT" --python "$PYTHON_BIN"
    else
        warn "uv is not installed; falling back to pip (versions may drift)"
        if [ ! -x "$VENV/bin/python" ]; then
            run "$PYTHON_BIN" -m venv "$VENV"
        fi
        run "$VENV/bin/python" -m pip install --upgrade pip
        run "$VENV/bin/python" -m pip install "$REPO_ROOT"
    fi
    [ -x "$VENV/bin/python" ] || fail "venv was not created: $VENV"
    [ -x "$VENV/bin/udc-resource" ] || fail "udc-resource entry point is missing"
    [ -x "$VENV/bin/udc-resource-login" ] || fail "udc-resource-login entry point is missing"
else
    run_shell "install -d -o root -g root -m 0755 $PREFIX"
    if have uv; then
        run_shell "UV_PROJECT_ENVIRONMENT=$VENV uv sync --frozen --no-dev --no-editable --project $REPO_ROOT --python $PYTHON_BIN"
    else
        run_shell "$PYTHON_BIN -m venv $VENV"
        run_shell "$VENV/bin/python -m pip install --upgrade pip"
        run_shell "$VENV/bin/python -m pip install $REPO_ROOT"
    fi
fi

run install -d -o root -g root -m 0755 "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ] && [ "$FORCE_CONFIG" -ne 1 ]; then
    note "keeping existing config: $CONFIG_FILE (use --force-config to replace)"
else
    run install -o root -g root -m 0644 "$CONFIG_SOURCE" "$CONFIG_FILE"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    note "would validate $CONFIG_FILE and create its workspace_root"
else
    UDC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'from udc_resource.config import load_config; load_config(__import__("os").environ["UDC_RESOURCE_CONFIG"])' \
        || fail "installed config failed validation"
    EFFECTIVE_MODE=$(UDC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from udc_resource.config import load_config; print(load_config(os.environ["UDC_RESOURCE_CONFIG"]).policy.mode)')
    if [ "$EFFECTIVE_MODE" = "active" ] && [ "$ALLOW_ACTIVE" -ne 1 ]; then
        fail "installed config is active; pass --allow-active only after approval"
    fi
    ok "config validated"
    WORKSPACE_ROOT=$(UDC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from udc_resource.config import load_config; print(load_config(os.environ["UDC_RESOURCE_CONFIG"]).server.workspace_root)') \
        || fail "could not read workspace_root from config"
    run install -d -o root -g root -m 0755 "$WORKSPACE_ROOT"
    ok "workspace root ready: $WORKSPACE_ROOT"
    if [ "$WORKSPACE_ROOT" != "/data1/udc-users" ]; then
        warn "systemd unit ReadWritePaths expects /data1/udc-users; update the unit for $WORKSPACE_ROOT"
    fi
fi

run install -d -o root -g root -m 0750 "$STATE_DIR"
if [ "$DRY_RUN" -eq 1 ]; then
    note "would initialize SQLite; fresh install uses monitor, active leases keep management"
else
    MODE=$(umask 077; UDC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" - <<'PY'
import os

from udc_resource.config import load_config
from udc_resource.db import init_db
from udc_resource import store

config = load_config(os.environ["UDC_RESOURCE_CONFIG"])
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
    STATE_DB=$(UDC_RESOURCE_CONFIG="$CONFIG_FILE" "$VENV/bin/python" -c \
        'import os; from udc_resource.config import load_config; print(load_config(os.environ["UDC_RESOURCE_CONFIG"]).server.state_db)') \
        || fail "could not read state_db from config"
    [ -f "$STATE_DB" ] || fail "state database was not created: $STATE_DB"
    chmod 0600 "$STATE_DB" || fail "could not chmod $STATE_DB"
    case "$STATE_DB" in
        /var/lib/udc-resource/*) ;;
        *) warn "state_db is outside /var/lib/udc-resource; update systemd ReadWritePaths" ;;
    esac
    ok "SQLite initialized in ${MODE} mode"
fi

if [ "$DRY_RUN" -eq 0 ]; then
    have udevadm || fail "udevadm is required"
fi
run install -o root -g root -m 0644 "$SCRIPT_DIR/udev/99-udc-gpu.rules" "$UDEV_RULE"
run udevadm control --reload-rules
run udevadm trigger --subsystem-match=nvidia
ok "udev rule installed"

run install -o root -g root -m 0644 \
    "$SCRIPT_DIR/systemd/udc-resource-broker.service" "$SYSTEMD_UNIT"
if [ -d /run/udc-resource ]; then
    run chmod 0711 /run/udc-resource
fi
run systemctl daemon-reload
run systemctl enable udc-resource-broker.service
ok "systemd unit installed and enabled"

run install -d -m 0755 /usr/local/bin
run ln -sfn "$VENV/bin/udc-resource-login" "$LOGIN_WRAPPER"
run ln -sfn "$VENV/bin/udc-resource" "$CLI_LINK"
ok "CLI links installed"

if [ ! -r /etc/ssh/sshd_config ]; then
    warn "/etc/ssh/sshd_config is not readable; skipping sshd drop-in"
else
    if [ "$DRY_RUN" -eq 0 ] && ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
        fail "sshd_config does not include /etc/ssh/sshd_config.d/*.conf"
    fi
    run install -d -m 0755 /etc/ssh/sshd_config.d
    run install -o root -g root -m 0644 "$SCRIPT_DIR/sshd/60-udc-resource.conf" "$SSHD_DROPIN"
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

if [ "$START" -eq 1 ] && [ "$AUDIT_FAILED" -eq 1 ]; then
    fail "legacy audit failed; resolve critical findings before starting the broker"
fi
if [ "$START" -eq 1 ]; then
    run systemctl restart udc-resource-broker.service
    if [ "$DRY_RUN" -eq 0 ]; then
        sleep 1
        systemctl is-active --quiet udc-resource-broker.service \
            || fail "broker failed to start; inspect journalctl -u udc-resource-broker"
        ok "broker started (${MODE:-unknown} mode)"
    fi
else
    note "broker installed but not started; use: systemctl start udc-resource-broker"
    if [ "$DRY_RUN" -eq 0 ] && systemctl is-active --quiet udc-resource-broker.service; then
        warn "broker is already running; restart it to apply the installed files"
    fi
fi

printf '\nInstallation complete. Next steps:\n'
printf '  1. create member/admin accounts with deploy/create_user.sh\n'
printf '  2. keep policy.mode=monitor and run deploy/verify_monitor_only.sh\n'
printf '  3. do not switch to active mode until training ends and approval is recorded\n'
