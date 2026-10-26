#!/usr/bin/env bash
# Read-only deployment preflight. This script must not change host state.
set -u
set -o pipefail

FAIL=0
WARN=0
CONFIG_FILE=""

section() { printf '\n== %s ==\n' "$1"; }
ok() { printf '[ok]   %s\n' "$1"; }
warn() { printf '[warn] %s\n' "$1"; WARN=$((WARN + 1)); }
fail() { printf '[fail] %s\n' "$1"; FAIL=$((FAIL + 1)); }
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
        -h|--help)
            printf 'usage: %s [--config PATH]\n\nRead-only preflight for the citespec-resource host.\n' "$0"
            exit 0
            ;;
        --config)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --config value\n' >&2; exit 2; }
            CONFIG_FILE=$1
            ;;
        *)
            printf 'unknown argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
    shift
done

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

section "configuration profile"
CONTAINER_PROFILE=""
WRITABLE_LAYER_GB=0
MODE=""
WORKSPACE_ROOT=""
MAX_WORKSPACE_GB=0
WORKSPACE_QUOTA_REQUIRED=false
VSCODE_ENABLED=false
CONFIG_PYTHON=""
for candidate in python3.12 python3.11 python3; do
    if have "$candidate" && "$candidate" -c 'import tomllib' >/dev/null 2>&1; then
        CONFIG_PYTHON=$candidate
        break
    fi
done
if [ -n "$CONFIG_FILE" ]; then
    if [ ! -r "$CONFIG_FILE" ]; then
        fail "configuration is not readable: $CONFIG_FILE"
    elif [ -z "$CONFIG_PYTHON" ]; then
        fail "Python 3.11+ with tomllib is required to inspect $CONFIG_FILE"
    else
        PROFILE_DATA=$(
            "$CONFIG_PYTHON" - "$CONFIG_FILE" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    data = tomllib.load(handle)
policy = data.get("policy", {})
server = data.get("server", {})
print(f"container_profile={policy.get('container_profile', 'restricted')}")
print(f"writable_layer_gb={policy.get('writable_layer_gb', 0)}")
print(f"mode={policy.get('mode', 'monitor')}")
print(f"workspace_root={server.get('workspace_root', '/data1/citespec-users')}")
print(f"max_workspace_gb={policy.get('max_workspace_gb', 0)}")
print(
    "workspace_quota_required="
    + ("true" if policy.get("workspace_quota_required", False) else "false")
)
print(f"vscode_enabled={'true' if policy.get('vscode_enabled', False) else 'false'}")
PY
        ) || fail "could not parse configuration: $CONFIG_FILE"
        while IFS='=' read -r key value; do
            case "$key" in
                container_profile) CONTAINER_PROFILE=$value ;;
                writable_layer_gb) WRITABLE_LAYER_GB=$value ;;
                mode) MODE=$value ;;
                workspace_root) WORKSPACE_ROOT=$value ;;
                max_workspace_gb) MAX_WORKSPACE_GB=$value ;;
                workspace_quota_required) WORKSPACE_QUOTA_REQUIRED=$value ;;
                vscode_enabled) VSCODE_ENABLED=$value ;;
            esac
        done <<< "$PROFILE_DATA"
        ok "mode: ${MODE:-unknown}"
        ok "container profile: ${CONTAINER_PROFILE:-restricted}"
        if [ "$CONTAINER_PROFILE" = "development" ]; then
            ok "writable-layer quota request: ${WRITABLE_LAYER_GB} GiB"
        fi
    fi
else
    warn "no --config supplied; skipping profile-specific storage/quota checks"
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
    MANAGED=$(docker ps --format '{{.ID}} {{.Names}} {{.Label "citespec.managed"}}' 2>/dev/null | grep -c 'true' || true)
    if [ "${MANAGED:-0}" -gt 0 ]; then
        warn "$MANAGED running container(s) already have citespec.managed=true"
    else
        ok "no running containers have citespec.managed=true"
    fi
    if [ "$CONTAINER_PROFILE" = "development" ]; then
        if [ "${WRITABLE_LAYER_GB:-0}" -le 0 ]; then
            fail "development profile requires a positive writable_layer_gb"
        fi
        DRIVER=$(docker info --format '{{.Driver}}' 2>/dev/null || true)
        DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
        if [ "$DRIVER" = "overlay2" ]; then
            ok "Docker storage driver: overlay2"
        else
            fail "development profile requires the overlay2 driver, found: ${DRIVER:-unknown}"
        fi
        if [ -z "$DOCKER_ROOT" ]; then
            fail "could not determine DockerRootDir"
        elif ! have findmnt; then
            fail "findmnt is required to verify DockerRootDir filesystem"
        else
            DOCKER_FS=$(findmnt -no FSTYPE,OPTIONS -T "$DOCKER_ROOT" 2>/dev/null || true)
            DOCKER_FSTYPE=$(printf '%s' "$DOCKER_FS" | awk '{print $1}')
            DOCKER_OPTIONS=$(printf '%s' "$DOCKER_FS" | awk '{print $2}')
            if [ "$DOCKER_FSTYPE" = "xfs" ]; then
                if printf '%s' ",$DOCKER_OPTIONS," | grep -Eq ',(prjquota|pquota),'; then
                    ok "DockerRootDir XFS has project quotas enabled: $DOCKER_ROOT"
                else
                    fail "DockerRootDir XFS is not mounted with prjquota/pquota: $DOCKER_ROOT"
                fi
            else
                fail "development profile requires XFS for DockerRootDir, found: ${DOCKER_FSTYPE:-unknown}"
            fi
            if have xfs_quota; then
                ok "xfs_quota is available"
            else
                fail "xfs_quota is required for writable-layer quotas"
            fi
        fi
        if [ -z "$WORKSPACE_ROOT" ]; then
            fail "development profile workspace root is not configured"
        elif ! have findmnt; then
            fail "findmnt is required to verify the workspace no-suid mount"
        elif [ "${WORKSPACE_ROOT#/}" = "$WORKSPACE_ROOT" ] \
            || [ "$WORKSPACE_ROOT" = "/" ] \
            || [ "${WORKSPACE_ROOT#//}" != "$WORKSPACE_ROOT" ]; then
            fail "development profile workspace root must be an absolute, non-root path: $WORKSPACE_ROOT"
        elif case "$WORKSPACE_ROOT" in *"/../"*|*/..) true ;; *) false ;; esac; then
            fail "development profile workspace root must not contain '..': $WORKSPACE_ROOT"
        else
            # The installer creates the workspace root after preflight, so check
            # the nearest existing ancestor rather than requiring the leaf first.
            WORKSPACE_CHECK=$WORKSPACE_ROOT
            while [ ! -e "$WORKSPACE_CHECK" ] && [ "$WORKSPACE_CHECK" != "/" ]; do
                WORKSPACE_CHECK=$(dirname "$WORKSPACE_CHECK")
            done
            if [ ! -e "$WORKSPACE_CHECK" ]; then
                fail "cannot resolve workspace filesystem for: $WORKSPACE_ROOT"
            else
                WORKSPACE_OPTS=$(findmnt -no OPTIONS -T "$WORKSPACE_CHECK" 2>/dev/null || true)
                if printf '%s' ",$WORKSPACE_OPTS," | grep -q ',nosuid,'; then
                    ok "workspace filesystem is mounted nosuid: $WORKSPACE_CHECK"
                else
                    fail "workspace filesystem must be mounted nosuid so container root cannot create host setuid files: $WORKSPACE_CHECK"
                fi
            fi
        fi
        if [ "$VSCODE_ENABLED" = "true" ]; then
            warn "VS Code tunneling requires the container image with sshd and bootstrap.sh"
        fi
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
for tool in stat su getent udevadm getfacl; do
    if have "$tool"; then
        ok "GPU audit tool available: $tool"
    else
        fail "$tool is required for the GPU permission audit"
    fi
done
if have nvidia-container-cli; then
    ok "nvidia-container-cli: $(nvidia-container-cli --version 2>/dev/null | head -n 1 || true)"
else
    warn "nvidia-container-cli is not installed"
fi

section "groups and users"
for group in citespec-members citespec-cli citespec-admins citespec-gpu; do
    if getent group "$group" >/dev/null 2>&1; then
        ok "group exists: $group"
    else
        warn "group does not exist yet: $group"
    fi
done
GPU_MEMBERS=$(effective_members citespec-gpu | tr '\n' ' ')
if [ -n "$GPU_MEMBERS" ]; then
    fail "citespec-gpu has effective members ($GPU_MEMBERS); ordinary members must not be in this group"
else
    ok "citespec-gpu has no effective members"
fi

section "gpu device permissions"
if compgen -G '/dev/nvidia*' >/dev/null; then
    for device in /dev/nvidia*; do
        [ -e "$device" ] || continue
        MODE=$(stat -c '%a' "$device" 2>/dev/null || true)
        GROUP=$(stat -c '%G' "$device" 2>/dev/null || true)
        if [ "$MODE" = "660" ] && [ "$GROUP" = "citespec-gpu" ]; then
            ok "$device is 0660 and owned by group citespec-gpu"
        else
            warn "$device mode/group is ${MODE:-unknown}/${GROUP:-unknown}; udev rule may not be installed"
        fi
    done
else
    warn "no /dev/nvidia* devices found"
fi
if [ -r /etc/udev/rules.d/99-citespec-gpu.rules ]; then
    if grep -Fq 'KERNEL=="nvidia*"' /etc/udev/rules.d/99-citespec-gpu.rules; then
        ok "udev rule matches all nvidia* device names"
    else
        warn "udev rule exists but does not use the KERNEL-based nvidia* match"
    fi
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
    if [ -r /etc/ssh/sshd_config.d/60-citespec-resource.conf ]; then
        ok "citespec-resource sshd drop-in is installed"
    else
        warn "citespec-resource sshd drop-in is not installed yet"
    fi
else
    warn "sshd is not installed or not in PATH"
fi
if have ss; then
    if ss -ltn 2>/dev/null | grep -q 'citespec-resource'; then
        warn "a TCP listener matching citespec-resource was found; the broker must use a Unix socket only"
    else
        ok "no obvious TCP listener for citespec-resource"
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
if [ "${MAX_WORKSPACE_GB:-0}" -gt 0 ] || [ "$WORKSPACE_QUOTA_REQUIRED" = "true" ]; then
    QUOTA_TARGET=${WORKSPACE_ROOT:-/data1/citespec-users}
    while [ ! -e "$QUOTA_TARGET" ] && [ "$QUOTA_TARGET" != "/" ]; do
        QUOTA_TARGET=$(dirname "$QUOTA_TARGET")
    done
    if ! have findmnt; then
        fail "findmnt is required to verify workspace project quotas"
    else
        WS_FS=$(findmnt -no FSTYPE,OPTIONS -T "$QUOTA_TARGET" 2>/dev/null || true)
        WS_FSTYPE=$(printf '%s' "$WS_FS" | awk '{print $1}')
        WS_OPTIONS=$(printf '%s' "$WS_FS" | awk '{print $2}')
        if [ "$WS_FSTYPE" = "xfs" ] && printf '%s' ",$WS_OPTIONS," | grep -Eq ',(prjquota|pquota),'; then
            if have xfs_quota; then
                ok "workspace filesystem supports project quotas: $QUOTA_TARGET"
            else
                fail "xfs_quota is missing for workspace project quotas"
            fi
        elif [ "$WS_FSTYPE" = "ext4" ] && printf '%s' ",$WS_OPTIONS," | grep -Eq ',(prjquota|pquota),'; then
            if have repquota && have setquota && have chattr; then
                ok "workspace filesystem supports ext4 project quotas: $QUOTA_TARGET"
            else
                fail "repquota/setquota/chattr are required for ext4 project quotas"
            fi
        else
            if [ "$MODE" = "active" ]; then
                fail "active mode requires project quotas on $QUOTA_TARGET (found ${WS_FSTYPE:-unknown})"
            else
                warn "workspace project quotas are unavailable on $QUOTA_TARGET (found ${WS_FSTYPE:-unknown}); active mode will be blocked"
            fi
        fi
    fi
fi
if [ -r /sys/fs/cgroup/cgroup.controllers ]; then
    ok "cgroup v2 is available"
else
    warn "cgroup v2 was not detected"
fi
if [ -e /run/citespec-resource/broker.sock ]; then
    warn "broker socket already exists; an implementation may already be running"
fi
if [ -e /var/lib/citespec-resource/state.db ]; then
    warn "broker state database already exists"
fi

section "summary"
printf 'failures: %d, warnings: %d\n' "$FAIL" "$WARN"
if [ "$FAIL" -ne 0 ]; then
    printf 'preflight found blocking problems; do not deploy yet.\n'
    exit 1
fi
printf 'preflight passed; deployment still requires explicit approval.\n'
