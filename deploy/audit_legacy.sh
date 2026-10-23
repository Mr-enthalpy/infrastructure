#!/usr/bin/env bash
# Read-only audit for legacy privileged access. It never removes users,
# groups, keys, containers, or sudo rules.
set -u
set -o pipefail

PRODUCTION=0
JSON=0
ALLOW_ROOT_SSH_KEYS=0
FAIL=0
WARN=0
FAILURES=()
WARNINGS=()

usage() {
    cat <<'EOF'
usage: audit_legacy.sh [options]

Read-only checks for legacy root/shared accounts, sudo/docker/GPU group
membership, root SSH keys, privileged containers, host namespaces, Docker
socket mounts, and non-managed GPU holders.

Options:
  --production             Treat missing tools, unreadable state, and root SSH
                           keys as failures. Required for the active-mode gate.
  --json                   Print a machine-readable result and no human text.
  --allow-root-ssh-keys    Permit /root/.ssh/authorized_keys (still warns).
  -h, --help               Show this help.

The script reports findings and exits non-zero when a critical finding exists.
It never changes host state.
EOF
}

section() { [ "$JSON" -eq 1 ] || printf '\n== %s ==\n' "$1"; }
ok() { [ "$JSON" -eq 1 ] || printf '[ok]   %s\n' "$1"; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --production) PRODUCTION=1 ;;
        --json) JSON=1 ;;
        --allow-root-ssh-keys) ALLOW_ROOT_SSH_KEYS=1 ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'unknown argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
    shift
done

RESULTS=$(mktemp "${TMPDIR:-/tmp}/udc-legacy-audit.XXXXXX") || exit 1
trap 'rm -f "$RESULTS"' EXIT

PYTHON_BIN=$(command -v python3 2>/dev/null || command -v python3.12 2>/dev/null || true)
if [ "$JSON" -eq 1 ] && [ -z "$PYTHON_BIN" ]; then
    printf 'python3 is required for --json audit output\n' >&2
    exit 1
fi
fail() {
    FAIL=$((FAIL + 1))
    FAILURES+=("$1")
    printf 'F\t%s\n' "$1" >> "$RESULTS"
    [ "$JSON" -eq 1 ] || printf '[fail] %s\n' "$1" >&2
}
warn() {
    WARN=$((WARN + 1))
    WARNINGS+=("$1")
    printf 'W\t%s\n' "$1" >> "$RESULTS"
    [ "$JSON" -eq 1 ] || printf '[warn] %s\n' "$1" >&2
}

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

user_in_group() {
    local user=$1 group=$2
    id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qx "$group"
}

if [ "$(id -u)" -ne 0 ]; then
    if [ "$PRODUCTION" -eq 1 ]; then
        fail "not running as root; production audit cannot inspect host state"
    else
        warn "not running as root; some audit checks may be incomplete"
    fi
fi

for tool in getent id awk grep stat docker; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        if [ "$PRODUCTION" -eq 1 ]; then
            fail "required audit tool is missing: $tool"
        else
            warn "required audit tool is missing: $tool"
        fi
    fi
done

SSHD_BIN=$(command -v sshd 2>/dev/null || true)
[ -n "$SSHD_BIN" ] || SSHD_BIN=/usr/sbin/sshd
if [ ! -x "$SSHD_BIN" ]; then
    if [ "$PRODUCTION" -eq 1 ]; then
        fail "sshd binary is not available; cannot verify effective SSH policy"
    else
        warn "sshd binary is not available"
    fi
fi
if ! command -v sudo >/dev/null 2>&1; then
    if [ "$PRODUCTION" -eq 1 ]; then
        fail "sudo is not installed; cannot verify per-account sudo rights"
    else
        warn "sudo is not installed; cannot verify per-account sudo rights"
    fi
fi

section "root and shared accounts"
while IFS=: read -r name _ uid gid _ home shell; do
    if [ "$uid" -eq 0 ] && [ "$name" != "root" ]; then
        fail "UID 0 account exists: $name"
    fi
    if [ "$gid" -eq 0 ] && [ "$uid" -ne 0 ]; then
        fail "non-root account has primary group root: $name"
    fi
    if [ "$name" = "root" ] && [ "$shell" != "/bin/bash" ] && [ "$shell" != "/bin/sh" ]; then
        warn "root shell is not bash/sh: $shell"
    fi
done < <(getent passwd)

section "privileged groups"
for group in sudo docker udc-gpu video render wheel; do
    members=$(effective_members "$group")
    [ -n "$members" ] || continue
    [ "$JSON" -eq 1 ] || printf '%s effective members: %s\n' "$group" "$members"
    for user in $members; do
        approved=0
        if user_in_group "$user" udc-admins; then
            approved=1
        fi
        if [ "$group" = "udc-gpu" ]; then
            fail "$user is in udc-gpu; ordinary members must not receive GPU device access"
        elif user_in_group "$user" udc-members; then
            fail "member $user is in privileged group $group"
        elif [ "$approved" -eq 1 ]; then
            warn "approved admin $user is in privileged group $group; verify this is intentional"
        else
            fail "non-approved account $user is in privileged group $group; revoke it"
        fi
    done
done

for user in $(effective_members udc-members); do
    if user_in_group "$user" udc-admins; then
        fail "member $user is also in udc-admins; remove one role"
    fi
done

section "SSH keys and sudoers"
if [ -s /root/.ssh/authorized_keys ]; then
    if [ "$ALLOW_ROOT_SSH_KEYS" -eq 1 ]; then
        warn "/root/.ssh/authorized_keys is non-empty (explicitly allowed)"
    else
        fail "/root/.ssh/authorized_keys is non-empty; revoke it or pass --allow-root-ssh-keys"
    fi
else
    ok "no root authorized_keys file contents"
fi

if [ -r /etc/sudoers ]; then
    sudo_lines=$(grep -R -nE 'NOPASSWD|ALL[[:space:]]*=' /etc/sudoers /etc/sudoers.d 2>/dev/null || true)
    if [ -n "$sudo_lines" ]; then
        warn "sudoers contains matching entries; review manually"
        [ "$JSON" -eq 1 ] || printf '%s\n' "$sudo_lines" | sed 's/^/       /' >&2
    else
        ok "no obvious NOPASSWD/ALL sudoers entries"
    fi
else
    fail "/etc/sudoers is not readable; cannot verify sudo policy"
fi

if command -v sudo >/dev/null 2>&1; then
    while IFS=: read -r user _ uid _; do
        [ "$uid" -ge 1000 ] || continue
        sudo_output=$(sudo -l -U "$user" 2>&1 || true)
        if printf '%s\n' "$sudo_output" | grep -qE 'may run the following|NOPASSWD|ALL[[:space:]]*='; then
            if user_in_group "$user" udc-admins; then
                warn "approved admin $user has sudo rights; verify this is intentional"
            else
                fail "non-approved account $user has sudo rights"
            fi
        fi
    done < <(getent passwd)
fi

if [ -x "$SSHD_BIN" ]; then
    if "$SSHD_BIN" -t 2>/dev/null; then
        ok "sshd configuration test passed"
    else
        fail "sshd -t failed; effective SSH policy is invalid"
    fi
    SSH_EFFECTIVE=$("$SSHD_BIN" -T 2>/dev/null || true)
    if [ -z "$SSH_EFFECTIVE" ]; then
        fail "sshd -T failed; cannot verify effective SSH policy"
    else
        if printf '%s\n' "$SSH_EFFECTIVE" | grep -q '^permitrootlogin yes'; then
            warn "effective sshd policy permits root login; verify it is key-only and intentional"
        fi
        if printf '%s\n' "$SSH_EFFECTIVE" | grep -q '^passwordauthentication yes'; then
            warn "effective sshd policy permits password authentication"
        fi
    fi
fi

DROPIN=/etc/ssh/sshd_config.d/60-udc-resource.conf
if [ -r "$DROPIN" ] \
    && grep -q '^[[:space:]]*Match[[:space:]]\+Group[[:space:]]\+udc-members' "$DROPIN" \
    && grep -q '^[[:space:]]*ForceCommand[[:space:]]\+/usr/local/bin/udc-resource-login' "$DROPIN"; then
    ok "member ForceCommand drop-in is present"
else
    fail "member ForceCommand drop-in is missing or invalid: $DROPIN"
fi

section "Docker socket"
if [ -e /var/run/docker.sock ] || [ -e /run/docker.sock ]; then
    socket=/var/run/docker.sock
    [ -e "$socket" ] || socket=/run/docker.sock
    sock_meta=$(stat -c '%a %U %G' "$socket" 2>/dev/null || true)
    [ "$JSON" -eq 1 ] || printf 'docker socket: %s %s\n' "$socket" "$sock_meta"
    sock_group=$(printf '%s\n' "$sock_meta" | awk '{print $3}')
    if [ -n "$sock_group" ] && [ "$sock_group" != "root" ]; then
        for user in $(effective_members udc-members); do
            if user_in_group "$user" "$sock_group"; then
                fail "member $user can access the Docker socket via group $sock_group"
            fi
        done
    fi
else
    warn "Docker socket was not found; cannot verify socket permissions"
fi

section "legacy containers"
if command -v docker >/dev/null 2>&1; then
    docker_bin=$(command -v docker)
else
    docker_bin=""
    fail "docker is not available; cannot audit existing containers"
fi
if [ -n "$docker_bin" ]; then
    if container_ids=$(docker ps -aq 2>/dev/null); then
        :
    else
        fail "docker ps failed; cannot audit existing containers"
        container_ids=""
    fi
    if [ -z "$container_ids" ]; then
        ok "no containers found"
    else
        for container_id in $container_ids; do
            name=$(docker inspect -f '{{.Name}}' "$container_id" 2>/dev/null || printf '%s' "$container_id")
            managed=$(docker inspect -f '{{index .Config.Labels "udc.managed"}}' "$container_id" 2>/dev/null || true)
            container_status=$(docker inspect -f '{{.State.Status}}' "$container_id" 2>/dev/null || true)
            privileged=$(docker inspect -f '{{.HostConfig.Privileged}}' "$container_id" 2>/dev/null || true)
            pid_mode=$(docker inspect -f '{{.HostConfig.PidMode}}' "$container_id" 2>/dev/null || true)
            network_mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$container_id" 2>/dev/null || true)
            mounts=$(docker inspect -f '{{range .Mounts}}{{.Source}} {{end}}' "$container_id" 2>/dev/null || true)
            devices=$(docker inspect -f '{{range .HostConfig.Devices}}{{.PathOnHost}} {{end}}' "$container_id" 2>/dev/null || true)
            requests=$(docker inspect -f '{{json .HostConfig.DeviceRequests}}' "$container_id" 2>/dev/null || true)
            env=$(docker inspect -f '{{range .Config.Env}}{{.}} {{end}}' "$container_id" 2>/dev/null || true)

            if [ "$privileged" = "true" ]; then
                fail "container $name is privileged"
            fi
            if [ "$pid_mode" = "host" ]; then
                fail "container $name uses host PID namespace"
            fi
            if [ "$network_mode" = "host" ]; then
                fail "container $name uses host network namespace"
            fi
            if printf '%s' "$mounts" | grep -q '/var/run/docker.sock'; then
                fail "container $name mounts the host Docker socket"
            fi
            gpu_access=""
            if printf '%s' "$devices" | grep -q '/dev/nvidia'; then
                gpu_access="devices"
            fi
            if printf '%s' "$requests" | grep -qE 'GPU-|"Count":-1|"Count":[1-9]|"DeviceIDs":\[[^]]'; then
                gpu_access="${gpu_access:+$gpu_access,}device-requests"
            fi
            if printf '%s' "$env" | grep -qE 'NVIDIA_VISIBLE_DEVICES=(all|[0-9]|GPU-)'; then
                gpu_access="${gpu_access:+$gpu_access,}nvidia-visible-devices"
            fi
            if [ -n "$gpu_access" ]; then
                case "$container_status" in
                    running|restarting|paused)
                        if [ "$managed" = "true" ]; then
                            warn "managed container $name has active GPU access ($gpu_access)"
                        else
                            fail "unmanaged container $name has active GPU access ($gpu_access)"
                        fi
                        ;;
                    *)
                        warn "stopped container $name has historical GPU refs ($gpu_access)"
                        ;;
                esac
            fi
        done
    fi
fi

section "summary"
if [ "$JSON" -eq 1 ]; then
    "$PYTHON_BIN" - "$FAIL" "$WARN" "$RESULTS" "$(hostname 2>/dev/null || printf unknown)" \
        "$(date -Is 2>/dev/null || date)" <<'PY'
import json
import sys

failure_count = int(sys.argv[1])
warning_count = int(sys.argv[2])
path = sys.argv[3]
failures = []
warnings = []
with open(path, encoding="utf-8") as handle:
    for line in handle:
        line = line.rstrip("\n")
        if "\t" not in line:
            continue
        kind, message = line.split("\t", 1)
        if kind == "F":
            failures.append(message)
        elif kind == "W":
            warnings.append(message)
print(
    json.dumps(
        {
            "ok": failure_count == 0,
            "hostname": sys.argv[4],
            "checked_at": sys.argv[5],
            "failure_count": failure_count,
            "warning_count": warning_count,
            "failures": failures,
            "warnings": warnings,
        },
        sort_keys=True,
    )
)
PY
else
    printf 'failures: %d, warnings: %d\n' "$FAIL" "$WARN"
    if [ "$FAIL" -ne 0 ]; then
        printf 'legacy audit FAILED; resolve critical findings before starting active mode.\n'
    else
        printf 'legacy audit passed.\n'
    fi
fi

if [ "$FAIL" -ne 0 ]; then
    exit 1
fi
exit 0
