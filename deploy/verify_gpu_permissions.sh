#!/usr/bin/env bash
# Read-only GPU device permission verification.
#
# With --member it checks one account. Without --member it checks every
# effective udc-members account, which is what the Broker uses for its
# continuous gate.
set -u
set -o pipefail

MEMBER=""
MEMBER_SET=0
EXPECTED_GPUS=8
PRODUCTION=0
JSON=0
FAIL=0
WARN=0
FAILURES=()
WARNINGS=()
RESULTS=""

usage() {
    cat <<'EOF'
usage: verify_gpu_permissions.sh [--member USER] [options]

Options:
  --member USER        Check one Linux account instead of every udc-members account.
  --expected-gpus N    Expected GPU count (default: 8).
  --production         Treat missing tools and unreadable state as failures.
  --json               Print one machine-readable JSON object and no human text.
  -h, --help           Show this help.

Checks every /dev/nvidia* node (nvidia0..N, nvidiactl, nvidia-uvm and any
additional NVIDIA nodes), expected 0660 root:udc-gpu ownership, and verifies
that members cannot read or write any node and cannot run nvidia-smi.
Run again after reboot and after reloading the NVIDIA driver.
EOF
}

section() { [ "$JSON" -eq 1 ] || printf '\n== %s ==\n' "$1"; }
ok() { [ "$JSON" -eq 1 ] || printf '[ok]   %s\n' "$1"; }
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
            MEMBER_SET=1
            ;;
        --expected-gpus)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --expected-gpus value\n' >&2; exit 2; }
            EXPECTED_GPUS=$1
            ;;
        --production) PRODUCTION=1 ;;
        --json) JSON=1 ;;
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

[[ "$EXPECTED_GPUS" =~ ^[1-9][0-9]*$ ]] || { printf 'expected GPU count must be positive\n' >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { printf 'run as root\n' >&2; exit 1; }

PYTHON_BIN=$(command -v python3 2>/dev/null || true)
if [ "$JSON" -eq 1 ] && [ -z "$PYTHON_BIN" ]; then
    printf 'python3 is required for --json output\n' >&2
    exit 1
fi

RESULTS=$(mktemp "${TMPDIR:-/tmp}/udc-gpu-audit.XXXXXX") || exit 1
trap 'rm -f "$RESULTS"' EXIT

for tool in nvidia-smi stat su getent id awk find; do
    if ! have "$tool"; then
        if [ "$PRODUCTION" -eq 1 ]; then
            fail "required GPU audit tool is missing: $tool"
        else
            warn "required GPU audit tool is missing: $tool"
        fi
    fi
done
for tool in udevadm getfacl; do
    if ! have "$tool"; then
        fail "required GPU audit tool is missing: $tool"
    fi
done

if [ "$MEMBER_SET" -eq 1 ]; then
    MEMBERS="$MEMBER"
else
    MEMBERS=$(effective_members udc-members)
fi

section "members"
if [ -z "$MEMBERS" ]; then
    ok "no udc-members accounts to test"
else
    for current in $MEMBERS; do
        if ! getent passwd "$current" >/dev/null 2>&1; then
            fail "unknown member: $current"
            continue
        fi
        MEMBER_GROUPS=$(id -nG "$current")
        for forbidden in udc-gpu docker sudo video render udc-admins; do
            if printf '%s\n' "$MEMBER_GROUPS" | tr ' ' '\n' | grep -qx "$forbidden"; then
                fail "$current is in forbidden group $forbidden"
            else
                ok "$current is not in $forbidden"
            fi
        done
    done
fi

section "GPU inventory"
GPU_ROWS=$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits 2>/dev/null) || GPU_ROWS=""
if [ -z "$GPU_ROWS" ]; then
    fail "nvidia-smi query failed or returned no GPUs"
    GPU_COUNT=0
else
    GPU_COUNT=$(printf '%s\n' "$GPU_ROWS" | sed '/^$/d' | wc -l | tr -d ' ')
    if [ "$GPU_COUNT" -eq "$EXPECTED_GPUS" ]; then
        ok "GPU count: $GPU_COUNT"
    else
        fail "GPU count is $GPU_COUNT, expected $EXPECTED_GPUS"
    fi
    for ((index = 0; index < EXPECTED_GPUS; index++)); do
        if printf '%s\n' "$GPU_ROWS" | awk -F, -v wanted="$index" '$1+0 == wanted {found=1} END {exit found ? 0 : 1}'; then
            ok "GPU index present: $index"
        else
            fail "GPU index missing: $index"
        fi
    done
fi

section "GPU group membership"
GPU_MEMBERS=$(effective_members udc-gpu | tr '\n' ' ')
if [ -n "$GPU_MEMBERS" ]; then
    fail "udc-gpu still contains effective members: $GPU_MEMBERS"
else
    ok "udc-gpu has no effective members"
fi

section "udev rule"
UDEV_RULE=/etc/udev/rules.d/99-udc-gpu.rules
if [ -r "$UDEV_RULE" ] && grep -Fq 'KERNEL=="nvidia*"' "$UDEV_RULE"; then
    ok "$UDEV_RULE matches every nvidia* kernel name"
else
    fail "$UDEV_RULE is missing or does not use KERNEL==\"nvidia*\""
fi

section "NVIDIA device nodes"
mapfile -t DEVICES < <(find /dev -maxdepth 2 -name 'nvidia*' -type c -print 2>/dev/null | sort)
[ "${#DEVICES[@]}" -gt 0 ] || fail "no /dev/nvidia* nodes found"
for ((index = 0; index < EXPECTED_GPUS; index++)); do
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

member_run() {
    local current=$1 command=$2
    # Use a non-interactive POSIX shell so member rc files cannot influence the
    # permission probe. The command is built only from trusted paths/policy.
    su -s /bin/sh "$current" -c "$command"
}

# A broken su/PAM path would make every denial probe look successful. Require a
# positive control so a failed audit cannot silently pass.
for current in $MEMBERS; do
    if ! member_run "$current" "test -r /etc/passwd"; then
        fail "cannot execute permission probes as member $current"
    fi
done

checked=0
for device in "${DEVICES[@]}"; do
    [ -e "$device" ] || continue
    meta=$(stat -c '%a %U %G' "$device" 2>/dev/null || true)
    if [ "$meta" = "660 root udc-gpu" ]; then
        ok "$device permissions: $meta"
    else
        fail "$device permissions are $meta (expected 660 root:udc-gpu)"
    fi
    if have udevadm; then
        subsystem=$(udevadm info --query=property --name="$device" 2>/dev/null | sed -n 's/^SUBSYSTEM=//p')
        case "$subsystem" in
            nvidia|misc) ok "$device udev subsystem: $subsystem" ;;
            "") warn "$device udev subsystem is unavailable" ;;
            *) warn "$device udev subsystem is '$subsystem'; verify the KERNEL-based rule" ;;
        esac
    fi
    if have getfacl; then
        acl=$(getfacl -cp "$device" 2>/dev/null | grep -E '^(user|group):[^:]+:' || true)
        if [ -n "$acl" ]; then
            fail "$device has an unexpected ACL: $acl"
        else
            ok "$device has no extra user/group ACL"
        fi
    fi
    for current in $MEMBERS; do
        if member_run "$current" "test -r $(printf '%q' "$device")"; then
            fail "$current can read $device"
        else
            ok "$current cannot read $device"
        fi
        if member_run "$current" "test -w $(printf '%q' "$device")"; then
            fail "$current can write $device"
        else
            ok "$current cannot write $device"
        fi
    done
    checked=$((checked + 1))
done
ok "checked $checked NVIDIA device node(s)"

section "member nvidia-smi"
if [ -z "$MEMBERS" ]; then
    ok "no member accounts to test"
else
    for current in $MEMBERS; do
        if member_run "$current" 'nvidia-smi -L' >/dev/null 2>&1; then
            fail "$current can run nvidia-smi -L"
        else
            ok "$current cannot run nvidia-smi -L"
        fi
    done
fi

section "summary"
if [ "$JSON" -eq 1 ]; then
    "$PYTHON_BIN" - "$FAIL" "$WARN" "$RESULTS" "$(hostname 2>/dev/null || printf unknown)" \
        "$(date -Is 2>/dev/null || date)" "$GPU_COUNT" "$EXPECTED_GPUS" "$MEMBERS" <<'PY'
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
            "gpu_count": int(sys.argv[6]),
            "expected_gpus": int(sys.argv[7]),
            "members": sys.argv[8].split(),
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
        printf 'GPU permission verification FAILED\n'
    else
        printf 'GPU permission verification PASSED; re-run after reboot and driver reload.\n'
    fi
fi

if [ "$FAIL" -ne 0 ]; then
    exit 1
fi
exit 0
