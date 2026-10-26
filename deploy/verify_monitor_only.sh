#!/usr/bin/env bash
# Read-only monitor-only acceptance and security checks.
#
# Run this on the server as root after installing and starting the broker in
# monitor mode. It never stops containers or changes host state.
set -u
set -o pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MEMBER=""
DURATION=600
SKIP_SNAPSHOT=0
CONFIG=${CITESPEC_RESOURCE_CONFIG:-/etc/citespec-resource/config.toml}
VENV=${CITESPEC_RESOURCE_VENV:-/opt/citespec-resource/.venv}
CLI=${CITESPEC_RESOURCE_CLI:-/usr/local/bin/citespec-resource}
LOGIN_WRAPPER=${CITESPEC_RESOURCE_LOGIN:-/usr/local/bin/citespec-resource-login}
GPU_COUNT=8

FAIL=0
WARN=0

usage() {
    cat <<'EOF'
usage: verify_monitor_only.sh --member USER [options]

Options:
  --member USER       Member account used for CLI/socket security checks.
  --duration SECONDS  Docker/GPU snapshot window (default: 600; 0 for immediate).
  --skip-snapshot     Skip the Docker/GPU before/after comparison.
  --config PATH       Broker config (default: /etc/citespec-resource/config.toml).
  --venv PATH         Broker venv (default: /opt/citespec-resource/.venv).
  --cli PATH          CLI executable (default: /usr/local/bin/citespec-resource).
  -h, --help          Show this help.

This script is read-only. It does not stop containers, change modes, or modify
users/groups. It checks the ForceCommand, socket permissions, Docker/GPU
denials, broker mode, and that Docker/GPU state is unchanged over the window.
EOF
}

fail() { printf '[fail] %s\n' "$1"; FAIL=$((FAIL + 1)); }
warn() { printf '[warn] %s\n' "$1"; WARN=$((WARN + 1)); }
ok() { printf '[ok]   %s\n' "$1"; }
section() { printf '\n== %s ==\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

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

while [ "$#" -gt 0 ]; do
    case "$1" in
        --member)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --member value\n' >&2; exit 2; }
            MEMBER=$1
            ;;
        --duration)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --duration value\n' >&2; exit 2; }
            DURATION=$1
            ;;
        --skip-snapshot) SKIP_SNAPSHOT=1 ;;
        --config)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --config value\n' >&2; exit 2; }
            CONFIG=$1
            ;;
        --venv)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --venv value\n' >&2; exit 2; }
            VENV=$1
            ;;
        --cli)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --cli value\n' >&2; exit 2; }
            CLI=$1
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

[ -n "$MEMBER" ] || { usage; exit 2; }
[ "$(id -u)" -eq 0 ] || { printf 'run as root\n' >&2; exit 1; }
[[ "$DURATION" =~ ^[0-9]+$ ]] || { printf 'duration must be a non-negative integer\n' >&2; exit 2; }

member_run() {
    su -s /bin/bash "$MEMBER" -c "$1"
}

snapshot() {
    docker ps -a --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Label "citespec.managed"}}|{{.Label "citespec.allocation"}}' 2>/dev/null | sort
    nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory,process_name --format=csv,noheader,nounits 2>/dev/null | sort
}

section "prerequisites"
getent passwd "$MEMBER" >/dev/null 2>&1 || { printf '[fail] unknown member: %s\n' "$MEMBER"; exit 1; }
ok "member exists: $MEMBER"
for cmd in docker nvidia-smi systemctl ss su ps; do
    if have "$cmd"; then ok "command available: $cmd"; else fail "command missing: $cmd"; fi
done
for group in citespec-members citespec-cli citespec-admins citespec-gpu; do
    if getent group "$group" >/dev/null 2>&1; then
        ok "group exists: $group"
    else
        fail "group missing: $group"
    fi
done
[ -x "$CLI" ] || fail "CLI is not executable: $CLI"
[ -x "$LOGIN_WRAPPER" ] || fail "login wrapper is not executable: $LOGIN_WRAPPER"
[ -f "$CONFIG" ] || fail "config not found: $CONFIG"
[ -x "$VENV/bin/python" ] || fail "venv python not found: $VENV/bin/python"
if [ "$FAIL" -ne 0 ]; then
    printf '\nPrerequisites failed; no acceptance checks were run.\n'
    exit 1
fi

section "legacy access audit"
AUDIT_SCRIPT=/usr/local/libexec/citespec-resource/audit_legacy.sh
[ -x "$AUDIT_SCRIPT" ] || AUDIT_SCRIPT="$SCRIPT_DIR/audit_legacy.sh"
AUDIT_ARGS=(--production)
CONFIG_ALLOW_ROOT_KEYS=$(CITESPEC_RESOURCE_CONFIG="$CONFIG" "$VENV/bin/python" -c \
    'import os; from citespec_resource.config import load_config; print("true" if load_config(os.environ["CITESPEC_RESOURCE_CONFIG"]).policy.allow_root_ssh_keys else "false")' \
    2>/dev/null || printf 'false')
if [ "$CONFIG_ALLOW_ROOT_KEYS" = "true" ]; then
    AUDIT_ARGS+=(--allow-root-ssh-keys)
fi
if bash "$AUDIT_SCRIPT" "${AUDIT_ARGS[@]}"; then
    ok "legacy audit passed"
else
    fail "legacy audit found critical findings"
fi

section "member groups"
MEMBER_GROUPS=$(id -nG "$MEMBER")
for required in citespec-members citespec-cli; do
    if printf '%s\n' "$MEMBER_GROUPS" | tr ' ' '\n' | grep -qx "$required"; then
        ok "member is in required group: $required"
    else
        fail "member is missing required group: $required"
    fi
done
for forbidden in sudo docker citespec-gpu video render citespec-admins; do
    if printf '%s\n' "$MEMBER_GROUPS" | tr ' ' '\n' | grep -qx "$forbidden"; then
        fail "member is in forbidden group: $forbidden"
    else
        ok "member is not in forbidden group: $forbidden"
    fi
done
GPU_MEMBERS=$(effective_members citespec-gpu | tr '\n' ' ')
if [ -n "$GPU_MEMBERS" ]; then
    fail "citespec-gpu has effective members: $GPU_MEMBERS"
else
    ok "citespec-gpu has no effective members"
fi

section "broker and socket"
if systemctl is-active --quiet citespec-resource-broker.service; then
    ok "broker service is active"
else
    fail "broker service is not active"
fi
MAINPID=$(systemctl show -p MainPID --value citespec-resource-broker.service 2>/dev/null || true)
if [ -n "$MAINPID" ] && [ "$MAINPID" -gt 0 ] 2>/dev/null; then
    ok "broker main pid: $MAINPID"
    if [ "$(ps -o user= -p "$MAINPID" 2>/dev/null | tr -d ' ')" = "root" ]; then
        ok "broker runs as root"
    else
        fail "broker is not running as root"
    fi
else
    fail "could not determine broker main pid"
fi

CONFIG_VALUES=$(CITESPEC_RESOURCE_CONFIG="$CONFIG" "$VENV/bin/python" - <<'PY'
import os
from citespec_resource.config import load_config

config = load_config(os.environ["CITESPEC_RESOURCE_CONFIG"])
print(config.server.broker_socket)
print(config.server.state_db)
print(config.hardware.gpu_count)
PY
) || { printf '[fail] could not parse config\n'; exit 1; }
mapfile -t CONFIG_LINES <<< "$CONFIG_VALUES"
SOCKET=${CONFIG_LINES[0]:-}
DB_PATH=${CONFIG_LINES[1]:-}
GPU_COUNT=${CONFIG_LINES[2]:-8}
[ -n "$SOCKET" ] || fail "broker socket path is empty"
[ -n "$DB_PATH" ] || fail "state database path is empty"

section "all GPU device permissions"
if bash "$SCRIPT_DIR/verify_gpu_permissions.sh" --member "$MEMBER" --expected-gpus "$GPU_COUNT"; then
    ok "all GPU and NVIDIA device permissions verified"
else
    fail "GPU device permission verification failed"
fi
if [ -S "$SOCKET" ]; then
    ok "broker socket exists: $SOCKET"
else
    fail "broker socket is missing: $SOCKET"
fi
if [ -e "$SOCKET" ]; then
    SOCKET_META=$(stat -c '%a %U %G' "$SOCKET" 2>/dev/null || true)
    if [ "$SOCKET_META" = "660 root citespec-cli" ]; then
        ok "socket permissions: $SOCKET_META"
    else
        fail "socket permissions are $SOCKET_META (expected 660 root citespec-cli)"
    fi
fi
SOCKET_DIR=$(dirname "$SOCKET")
if [ -d "$SOCKET_DIR" ]; then
    DIR_META=$(stat -c '%a %U %G' "$SOCKET_DIR" 2>/dev/null || true)
    if [ "$DIR_META" = "711 root root" ]; then
        ok "socket parent permissions: $DIR_META"
    else
        fail "socket parent permissions are $DIR_META (expected 711 root root)"
    fi
    if member_run "test -x $(printf '%q' "$SOCKET_DIR")"; then
        ok "member can traverse socket parent"
    else
        fail "member cannot traverse socket parent: $SOCKET_DIR"
    fi
else
    fail "socket parent directory is missing: $SOCKET_DIR"
fi
if [ -f "$DB_PATH" ]; then
    DB_META=$(stat -c '%a %U %G' "$DB_PATH" 2>/dev/null || true)
    if [ "$DB_META" = "600 root root" ]; then
        ok "state database permissions: $DB_META"
    else
        warn "state database permissions are $DB_META (expected 600 root root)"
    fi
else
    fail "state database is missing: $DB_PATH"
fi
SS_OUT=$(ss -lxnp 2>/dev/null || true)
case "$SS_OUT" in
    *"$SOCKET"*) ok "Unix socket listener is present" ;;
    *) fail "Unix socket listener was not found in ss output" ;;
esac
if [ -n "$MAINPID" ] && ss -ltnp 2>/dev/null | grep -Fq "pid=$MAINPID,"; then
    fail "broker main pid has a TCP listener"
else
    ok "broker main pid has no TCP listener"
fi

section "sshd ForceCommand"
SSHD_BIN=$(command -v sshd 2>/dev/null || true)
[ -n "$SSHD_BIN" ] || SSHD_BIN=/usr/sbin/sshd
if [ -x "$SSHD_BIN" ]; then
    SSHD_T=$("$SSHD_BIN" -T -C "user=$MEMBER,host=localhost,addr=127.0.0.1" 2>/dev/null || true)
    if printf '%s\n' "$SSHD_T" | grep -qiE '^forcecommand[[:space:]]+/usr/local/bin/citespec-resource-login$'; then
        ok "ForceCommand is active for $MEMBER"
    else
        fail "ForceCommand is not active for $MEMBER"
    fi
    if printf '%s\n' "$SSHD_T" | grep -qiE '^allowtcpforwarding[[:space:]]+no$'; then
        ok "AllowTcpForwarding is disabled"
    else
        fail "AllowTcpForwarding is not disabled"
    fi
    if printf '%s\n' "$SSHD_T" | grep -qiE '^allowstreamlocalforwarding[[:space:]]+no$'; then
        ok "AllowStreamLocalForwarding is disabled"
    else
        fail "AllowStreamLocalForwarding is not disabled"
    fi
    if printf '%s\n' "$SSHD_T" | grep -qiE '^allowagentforwarding[[:space:]]+no$'; then
        ok "AllowAgentForwarding is disabled"
    else
        fail "AllowAgentForwarding is not disabled"
    fi
    if printf '%s\n' "$SSHD_T" | grep -qiE '^permittunnel[[:space:]]+no$'; then
        ok "PermitTunnel is disabled"
    else
        fail "PermitTunnel is not disabled"
    fi
    if printf '%s\n' "$SSHD_T" | grep -qiE '^x11forwarding[[:space:]]+no$'; then
        ok "X11Forwarding is disabled"
    else
        fail "X11Forwarding is not disabled"
    fi
else
    fail "sshd binary not found"
fi

section "member CLI and denial checks"
LIST_JSON=$(member_run "$CLI list --json" 2>/dev/null) || LIST_JSON=""
if [ -n "$LIST_JSON" ] && printf '%s' "$LIST_JSON" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
    ok "member can call the broker CLI"
else
    fail "member cannot call the broker CLI"
fi
if printf '%s' "$LIST_JSON" | grep -q '"mode"[[:space:]]*:[[:space:]]*"monitor"'; then
    ok "broker is in monitor mode"
else
    fail "broker is not in monitor mode"
fi
if printf '%s' "$LIST_JSON" | grep -qE '"status"[[:space:]]*:[[:space:]]*"(pending|active)"'; then
    fail "active/pending leases exist; monitor-only acceptance requires an idle deployment"
else
    ok "no active/pending leases"
fi
if printf '%s' "$LIST_JSON" | grep -q '"unmanaged"'; then
    ok "unmanaged resources are reported"
else
    warn "unmanaged section is missing from list output"
fi
if member_run 'docker ps' >/dev/null 2>&1; then
    fail "member can run docker ps"
else
    ok "member cannot run docker ps"
fi
if [ -e /dev/nvidia0 ]; then
    if member_run 'test -r /dev/nvidia0'; then
        fail "member can read /dev/nvidia0"
    else
        ok "member cannot read /dev/nvidia0"
    fi
else
    fail "/dev/nvidia0 is missing"
fi
if member_run 'sudo -n true' >/dev/null 2>&1; then
    fail "member has passwordless sudo"
else
    ok "member has no passwordless sudo"
fi
if systemctl --help 2>&1 | grep -q -- '--dry-run'; then
    # systemctl --dry-run only validates unit names; it does not perform the
    # D-Bus/polkit authorization check and returns success for unprivileged
    # users on Ubuntu.  Do not treat that as a real stop permission.  The
    # member's actual SSH entry point is the ForceCommand wrapper, which
    # rejects systemctl before execution (checked separately by the CLI
    # denial tests).
    if member_run 'systemctl --dry-run stop citespec-resource-broker' >/dev/null 2>&1; then
        warn "systemctl --dry-run is not an authorization probe; relying on ForceCommand to deny systemctl"
    else
        ok "member cannot request broker stop"
    fi
else
    warn "systemctl --dry-run is unavailable; skipped stop-permission probe"
fi

section "managed containers"
MANAGED_COUNT=$(docker ps -a --filter label=citespec.managed=true --format '{{.ID}}' 2>/dev/null | wc -l | tr -d ' ')
if [ "${MANAGED_COUNT:-0}" -eq 0 ]; then
    ok "no managed containers exist"
else
    warn "$MANAGED_COUNT managed container(s) already exist; monitor-only should normally start empty"
fi

if [ "$SKIP_SNAPSHOT" -eq 0 ]; then
    section "monitor-only snapshot"
    BEFORE=$(snapshot)
    printf 'watching Docker/GPU state for %s seconds...\n' "$DURATION"
    sleep "$DURATION"
    AFTER=$(snapshot)
    if [ "$BEFORE" = "$AFTER" ]; then
        ok "Docker and GPU state did not change"
    else
        fail "Docker or GPU state changed during the monitor window"
    fi
fi

section "summary"
printf 'failures: %d, warnings: %d\n' "$FAIL" "$WARN"
if [ "$FAIL" -ne 0 ]; then
    printf 'monitor-only acceptance FAILED\n'
    exit 1
fi
printf 'monitor-only acceptance PASSED\n'
