#!/usr/bin/env bash
# Read-only audit for legacy privileged access. It never removes users,
# groups, keys, containers, or sudo rules.
set -u
set -o pipefail

FAIL=0
WARN=0

usage() {
    cat <<'EOF'
usage: audit_legacy.sh

Read-only checks for legacy root/shared accounts, sudo/docker/GPU group
membership, root SSH keys, privileged containers, host namespaces, Docker
socket mounts, and non-managed GPU holders.

The script reports findings and exits non-zero when a critical finding exists.
It never changes host state.
EOF
}

section() { printf '\n== %s ==\n' "$1"; }
ok() { printf '[ok]   %s\n' "$1"; }
warn() { printf '[warn] %s\n' "$1"; WARN=$((WARN + 1)); }
fail() { printf '[fail] %s\n' "$1"; FAIL=$((FAIL + 1)); }

case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
    "") ;;
    *)
        printf 'unknown argument: %s\n' "$1" >&2
        exit 2
        ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    warn "not running as root; some audit checks may be incomplete"
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
done < /etc/passwd

section "privileged groups"
for group in sudo docker udc-gpu video render; do
    members=$(getent group "$group" 2>/dev/null | awk -F: '{print $4}')
    [ -n "$members" ] || continue
    printf '%s members: %s\n' "$group" "$members"
    for user in $(printf '%s' "$members" | tr ',' ' '); do
        user_groups=$(id -nG "$user" 2>/dev/null || true)
        if [ "$group" = "udc-gpu" ]; then
            fail "$user is in udc-gpu; ordinary members must not receive GPU device access"
        elif printf '%s\n' "$user_groups" | tr ' ' '\n' | grep -qx 'udc-members'; then
            fail "member $user is in privileged group $group"
        elif printf '%s\n' "$user_groups" | tr ' ' '\n' | grep -qx 'udc-admins'; then
            warn "admin $user is in privileged group $group; verify this is intentional"
        else
            warn "account $user is in privileged group $group; review whether it is legacy"
        fi
    done
done

section "SSH keys and sudoers"
if [ -s /root/.ssh/authorized_keys ]; then
    warn "/root/.ssh/authorized_keys exists; review whether every key is still needed"
else
    ok "no root authorized_keys file contents"
fi
if [ -r /etc/sudoers ]; then
    sudo_lines=$(grep -R -nE 'NOPASSWD|ALL[[:space:]]*=' /etc/sudoers /etc/sudoers.d 2>/dev/null || true)
    if [ -n "$sudo_lines" ]; then
        warn "sudoers contains matching entries; review manually:"
        printf '%s\n' "$sudo_lines" | sed 's/^/       /'
    else
        ok "no obvious NOPASSWD/ALL sudoers entries"
    fi
else
    warn "/etc/sudoers is not readable"
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
            privileged=$(docker inspect -f '{{.HostConfig.Privileged}}' "$container_id" 2>/dev/null || true)
            pid_mode=$(docker inspect -f '{{.HostConfig.PidMode}}' "$container_id" 2>/dev/null || true)
            network_mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$container_id" 2>/dev/null || true)
            mounts=$(docker inspect -f '{{range .Mounts}}{{.Source}} {{end}}' "$container_id" 2>/dev/null || true)
            devices=$(docker inspect -f '{{range .HostConfig.Devices}}{{.PathOnHost}} {{end}}' "$container_id" 2>/dev/null || true)
            requests=$(docker inspect -f '{{range .HostConfig.DeviceRequests}}{{.Count}}/{{.DeviceIDs}} {{end}}' "$container_id" 2>/dev/null || true)
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
            if printf '%s' "$requests" | grep -qE 'GPU-|(^| )-1/|/[^]]*[0-9]'; then
                gpu_access="${gpu_access:+$gpu_access,}device-requests"
            fi
            if printf '%s' "$env" | grep -qE 'NVIDIA_VISIBLE_DEVICES=(all|[0-9]|GPU-)'; then
                gpu_access="${gpu_access:+$gpu_access,}nvidia-visible-devices"
            fi
            if [ -n "$gpu_access" ]; then
                if [ "$managed" = "true" ]; then
                    warn "managed container $name has GPU access ($gpu_access)"
                else
                    fail "unmanaged container $name has GPU access ($gpu_access)"
                fi
            fi
        done
    fi
fi

section "summary"
printf 'failures: %d, warnings: %d\n' "$FAIL" "$WARN"
if [ "$FAIL" -ne 0 ]; then
    printf 'legacy audit FAILED; resolve critical findings before starting active mode.\n'
    exit 1
fi
printf 'legacy audit passed.\n'
