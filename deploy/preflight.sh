#!/usr/bin/env bash
# Read-only deployment preflight. This script must not change host state.
set -u
set -o pipefail

FAIL=0
WARN=0

section() { printf '\n== %s ==\n' "$1"; }
ok() { printf '[ok]   %s\n' "$1"; }
warn() { printf '[warn] %s\n' "$1"; WARN=$((WARN + 1)); }
fail() { printf '[fail] %s\n' "$1"; FAIL=$((FAIL + 1)); }
have() { command -v "$1" >/dev/null 2>&1; }

case "${1:-}" in
    -h|--help)
        printf 'usage: %s\n\nRead-only preflight for the udc-resource host.\n' "$0"
        exit 0
        ;;
    "") ;;
    *)
        printf 'unknown argument: %s\n' "$1" >&2
        exit 2
        ;;
esac

section "host"
if have uname; then
    ok "$(uname -srm)"
else
    fail "uname is not available"
fi
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    ok "os: ${PRETTY_NAME:-unknown}"
else
    warn "/etc/os-release is not readable"
fi
if [ "$(id -u)" -eq 0 ]; then
    ok "running as root; all checks available"
else
    warn "not running as root; some sshd/udev checks may be incomplete"
fi

section "python"
if have python3; then
    PY_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
    if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
        ok "python3 is $PY_VERSION"
    else
        warn "python3 is $PY_VERSION; install Python 3.12 via uv or deadsnakes"
    fi
    if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
        ok "python3 meets the preferred 3.12 baseline"
    fi
else
    fail "python3 is not installed"
fi
if have python3.12; then
    ok "python3.12: $(python3.12 --version 2>&1)"
else
    warn "python3.12 is not installed; install it via uv or deadsnakes"
fi
if have uv; then
    ok "uv: $(uv --version 2>/dev/null || true)"
else
    warn "uv is not installed; the installer must provide Python 3.12 another way"
fi

section "docker"
if have docker; then
    if docker version >/dev/null 2>&1; then
        ok "docker client/server reachable"
        ok "docker server: $(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
    else
        fail "docker is installed but the daemon is not reachable"
    fi
    RUNTIMES=$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)
    if printf '%s' "$RUNTIMES" | grep -q 'nvidia'; then
        ok "nvidia runtime is registered"
    else
        fail "nvidia runtime is not registered in Docker"
    fi
    if systemctl is-active --quiet docker 2>/dev/null; then
        ok "docker.service is active"
    else
        warn "docker.service is not active or systemd is unavailable"
    fi
    MANAGED=$(docker ps --format '{{.ID}} {{.Names}} {{.Label "udc.managed"}}' 2>/dev/null | grep -c 'true' || true)
    if [ "${MANAGED:-0}" -gt 0 ]; then
        warn "$MANAGED running container(s) already have udc.managed=true"
    else
        ok "no running containers have udc.managed=true"
    fi
else
    fail "docker is not installed"
fi

section "nvidia"
if have nvidia-smi; then
    if nvidia-smi --query-gpu=index,uuid,name,memory.total --format=csv,noheader; then
        ok "nvidia-smi query succeeded"
    else
        fail "nvidia-smi query failed"
    fi
    if nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader 2>/dev/null | grep -q .; then
        warn "one or more GPUs currently have compute processes; do not deploy during training"
    else
        ok "no compute processes currently reported"
    fi
else
    fail "nvidia-smi is not installed"
fi
if have nvidia-container-cli; then
    ok "nvidia-container-cli: $(nvidia-container-cli --version 2>/dev/null | head -n 1 || true)"
else
    warn "nvidia-container-cli is not installed"
fi

section "groups and users"
for group in udc-members udc-cli udc-admins udc-gpu; do
    if getent group "$group" >/dev/null 2>&1; then
        ok "group exists: $group"
    else
        warn "group does not exist yet: $group"
    fi
done
if getent group udc-gpu 2>/dev/null | awk -F: '$4 != "" {found=1} END {exit found ? 0 : 1}'; then
    fail "udc-gpu has members; ordinary members must not be in this group"
else
    ok "udc-gpu has no members"
fi

section "gpu device permissions"
if compgen -G '/dev/nvidia*' >/dev/null; then
    stat -c '%a %U %G %n' /dev/nvidia* 2>/dev/null || warn "could not stat /dev/nvidia*"
    if [ -e /dev/nvidia0 ]; then
        MODE=$(stat -c '%a' /dev/nvidia0 2>/dev/null || true)
        GROUP=$(stat -c '%G' /dev/nvidia0 2>/dev/null || true)
        if [ "$MODE" = "660" ] && [ "$GROUP" = "udc-gpu" ]; then
            ok "/dev/nvidia0 is 0660 and owned by group udc-gpu"
        else
            warn "/dev/nvidia0 mode/group is ${MODE:-unknown}/${GROUP:-unknown}; udev rule may not be installed"
        fi
    fi
else
    warn "no /dev/nvidia* devices found"
fi
if [ -r /etc/udev/rules.d/99-udc-gpu.rules ]; then
    ok "udev rule is installed"
else
    warn "udev rule is not installed yet"
fi

section "sshd"
if have sshd; then
    if sshd -t 2>/dev/null; then
        ok "sshd configuration test passed"
    else
        warn "sshd -t failed (root may be required, or the config is invalid)"
    fi
    if [ -r /etc/ssh/sshd_config.d/60-udc-resource.conf ]; then
        ok "udc-resource sshd drop-in is installed"
    else
        warn "udc-resource sshd drop-in is not installed yet"
    fi
else
    warn "sshd is not installed or not in PATH"
fi
if have ss; then
    if ss -ltn 2>/dev/null | grep -q 'udc-resource'; then
        warn "a TCP listener matching udc-resource was found; the broker must use a Unix socket only"
    else
        ok "no obvious TCP listener for udc-resource"
    fi
fi

section "filesystem and cgroups"
for path in /data1 /var/lib /run; do
    if [ -e "$path" ]; then
        ok "path exists: $path"
    else
        warn "path is missing: $path"
    fi
done
if have df; then
    df -h /data1 /var/lib 2>/dev/null || warn "could not read disk usage"
fi
if [ -r /sys/fs/cgroup/cgroup.controllers ]; then
    ok "cgroup v2 is available"
else
    warn "cgroup v2 was not detected"
fi
if [ -e /run/udc-resource/broker.sock ]; then
    warn "broker socket already exists; an implementation may already be running"
fi
if [ -e /var/lib/udc-resource/state.db ]; then
    warn "broker state database already exists"
fi

section "summary"
printf 'failures: %d, warnings: %d\n' "$FAIL" "$WARN"
if [ "$FAIL" -ne 0 ]; then
    printf 'preflight found blocking problems; do not deploy yet.\n'
    exit 1
fi
printf 'preflight passed; deployment still requires explicit approval.\n'
