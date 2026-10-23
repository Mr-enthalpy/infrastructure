#!/usr/bin/env bash
# Read-only GPU device permission verification for all cards and NVIDIA nodes.
set -u
set -o pipefail

MEMBER=""
EXPECTED_GPUS=8
FAIL=0
WARN=0

usage() {
    cat <<'EOF'
usage: verify_gpu_permissions.sh --member USER [options]

Options:
  --member USER        Linux member account used for direct access checks.
  --expected-gpus N    Expected GPU count (default: 8).
  -h, --help           Show this help.

Checks every /dev/nvidia* node (nvidia0..N, nvidiactl, nvidia-uvm and any
additional NVIDIA nodes), expected 0660 root:udc-gpu ownership, and verifies
that the member cannot read or write any node and cannot run nvidia-smi.
Run again after reboot and after reloading the NVIDIA driver.
EOF
}

fail() { printf '[fail] %s\n' "$1"; FAIL=$((FAIL + 1)); }
warn() { printf '[warn] %s\n' "$1"; WARN=$((WARN + 1)); }
ok() { printf '[ok]   %s\n' "$1"; }
section() { printf '\n== %s ==\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --member)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --member value\n' >&2; exit 2; }
            MEMBER=$1
            ;;
        --expected-gpus)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --expected-gpus value\n' >&2; exit 2; }
            EXPECTED_GPUS=$1
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
[[ "$EXPECTED_GPUS" =~ ^[1-9][0-9]*$ ]] || { printf 'expected GPU count must be positive\n' >&2; exit 2; }
getent passwd "$MEMBER" >/dev/null 2>&1 || { printf '[fail] unknown member: %s\n' "$MEMBER"; exit 1; }
have nvidia-smi || { printf '[fail] nvidia-smi not found\n'; exit 1; }
have stat || { printf '[fail] stat not found\n'; exit 1; }
have su || { printf '[fail] su not found\n'; exit 1; }
have udevadm || { printf '[fail] udevadm not found; cannot verify the nvidia subsystem\n'; exit 1; }

member_run() {
    su -s /bin/bash "$MEMBER" -c "$1"
}

section "member"
MEMBER_GROUPS=$(id -nG "$MEMBER")
for forbidden in udc-gpu docker sudo video render udc-admins; do
    if printf '%s\n' "$MEMBER_GROUPS" | tr ' ' '\n' | grep -qx "$forbidden"; then
        fail "$MEMBER is in forbidden group $forbidden"
    else
        ok "$MEMBER is not in $forbidden"
    fi
done

section "GPU inventory"
GPU_ROWS=$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits 2>/dev/null) || {
    printf '[fail] nvidia-smi query failed\n'
    exit 1
}
GPU_COUNT=$(printf '%s\n' "$GPU_ROWS" | sed '/^$/d' | wc -l | tr -d ' ')
if [ "$GPU_COUNT" -eq "$EXPECTED_GPUS" ]; then
    ok "GPU count: $GPU_COUNT"
else
    fail "GPU count is $GPU_COUNT, expected $EXPECTED_GPUS"
fi
for index in $(seq 0 $((EXPECTED_GPUS - 1))); do
    if printf '%s\n' "$GPU_ROWS" | awk -F, -v wanted="$index" '$1+0 == wanted {found=1} END {exit found ? 0 : 1}'; then
        ok "GPU index present: $index"
    else
        fail "GPU index missing: $index"
    fi
done

section "GPU group membership"
GPU_MEMBERS=$(getent group udc-gpu 2>/dev/null | awk -F: '{print $4}')
if [ -n "$GPU_MEMBERS" ]; then
    fail "udc-gpu still contains members: $GPU_MEMBERS"
else
    ok "udc-gpu has no members"
fi

section "NVIDIA device nodes"
mapfile -t DEVICES < <(find /dev -maxdepth 2 -name 'nvidia*' -type c -print 2>/dev/null | sort)
[ "${#DEVICES[@]}" -gt 0 ] || fail "no /dev/nvidia* nodes found"
for index in $(seq 0 $((EXPECTED_GPUS - 1))); do
    device="/dev/nvidia$index"
    if [ -e "$device" ]; then
        ok "device exists: $device"
    else
        fail "device missing: $device"
    fi
done
for required in /dev/nvidiactl /dev/nvidia-uvm; do
    if [ -e "$required" ]; then
        ok "device exists: $required"
    else
        fail "device missing: $required"
    fi
done
if [ -e /dev/nvidia-uvm-tools ]; then
    ok "device exists: /dev/nvidia-uvm-tools"
fi
if [ -e /dev/nvidia-modeset ]; then
    ok "device exists: /dev/nvidia-modeset"
fi

checked=0
for device in "${DEVICES[@]}"; do
    [ -e "$device" ] || continue
    meta=$(stat -c '%a %U %G' "$device" 2>/dev/null || true)
    if [ "$meta" = "660 root udc-gpu" ]; then
        ok "$device permissions: $meta"
    else
        fail "$device permissions are $meta (expected 660 root:udc-gpu)"
    fi
    subsystem=$(udevadm info --query=property --name="$device" 2>/dev/null | sed -n 's/^SUBSYSTEM=//p')
    if [ "$subsystem" = "nvidia" ]; then
        ok "$device udev subsystem: nvidia"
    else
        fail "$device udev subsystem is '${subsystem:-unknown}', expected nvidia"
    fi
    if have getfacl; then
        acl=$(getfacl -cp "$device" 2>/dev/null | grep -E '^(user|group):[^:]+:' || true)
        if [ -n "$acl" ]; then
            fail "$device has an unexpected ACL: $acl"
        else
            ok "$device has no extra user/group ACL"
        fi
    else
        warn "getfacl not installed; cannot inspect $device ACLs"
    fi
    if member_run "test -r $(printf '%q' "$device")"; then
        fail "$MEMBER can read $device"
    else
        ok "$MEMBER cannot read $device"
    fi
    if member_run "test -w $(printf '%q' "$device")"; then
        fail "$MEMBER can write $device"
    else
        ok "$MEMBER cannot write $device"
    fi
    checked=$((checked + 1))
done
ok "checked $checked NVIDIA device node(s)"

section "member nvidia-smi"
if member_run 'nvidia-smi -L' >/dev/null 2>&1; then
    fail "$MEMBER can run nvidia-smi -L"
else
    ok "$MEMBER cannot run nvidia-smi -L"
fi

section "summary"
printf 'failures: %d, warnings: %d\n' "$FAIL" "$WARN"
if [ "$FAIL" -ne 0 ]; then
    printf 'GPU permission verification FAILED\n'
    exit 1
fi
printf 'GPU permission verification PASSED; re-run after reboot and driver reload.\n'
