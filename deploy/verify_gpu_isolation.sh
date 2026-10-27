#!/usr/bin/env bash
# Verify that container root cannot mutate host NVIDIA device nodes.
#
# This is the P0 gate from docs/acceptance.md §5.6.  Run it only in an approved
# maintenance window, with one dedicated test GPU allocated to a development
# container.  It records the host device state, attempts chmod/chown as the
# member via container sudo, records the host state again, and restores any
# change it observes.
#
# A passing run with --bystander and --write-marker writes
# /etc/citespec-resource/gpu-isolation.ok.  Active development containers with
# sudo are refused by the broker and preflight until that marker exists.
set -u
set -o pipefail

CONFIRM=0
GPU_INDEX=""
CONTAINER=""
MEMBER=""
BYSTANDER=""
DEVICE=""
MARKER=/etc/citespec-resource/gpu-isolation.ok
EVIDENCE_DIR=""
WRITE_MARKER=0

usage() {
    cat <<'EOF'
usage: verify_gpu_isolation.sh --confirm --gpu N --container NAME --member USER
                               [--bystander USER] [--device PATH]
                               [--write-marker] [--marker PATH]

Records host /dev/nvidia* state, then inside the running development container
runs as MEMBER via sudo: stat, chmod 0666, and chown 1000:1000 on the allocated
GPU node.  Any host mode/owner/inode change is a FAIL and is restored.

  --confirm          Required acknowledgement of the maintenance window.
  --gpu N            Allocated GPU index (used as /dev/nvidiaN).
  --container NAME   Running development-profile container ID or name.
  --member USER      Member account allowed to use container sudo.
  --bystander USER   Second, non-allocated member; must NOT read the node.
                     Required with --write-marker.
  --device PATH      Override the device node (default /dev/nvidiaN).
  --write-marker     Write the acceptance marker after a fully passing test.
  --marker PATH      Marker path (default /etc/citespec-resource/gpu-isolation.ok).
EOF
}

fail() { printf '[fail] %s\n' "$1" >&2; exit 1; }
note() { printf '[info] %s\n' "$1"; }
ok() { printf '[ok]   %s\n' "$1"; }
warn() { printf '[warn] %s\n' "$1"; }

have() { command -v "$1" >/dev/null 2>&1; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --confirm) CONFIRM=1 ;;
        --gpu)
            shift
            [ "$#" -gt 0 ] || fail "missing --gpu value"
            GPU_INDEX=$1
            ;;
        --container)
            shift
            [ "$#" -gt 0 ] || fail "missing --container value"
            CONTAINER=$1
            ;;
        --member)
            shift
            [ "$#" -gt 0 ] || fail "missing --member value"
            MEMBER=$1
            ;;
        --bystander)
            shift
            [ "$#" -gt 0 ] || fail "missing --bystander value"
            BYSTANDER=$1
            ;;
        --device)
            shift
            [ "$#" -gt 0 ] || fail "missing --device value"
            DEVICE=$1
            ;;
        --write-marker) WRITE_MARKER=1 ;;
        --marker)
            shift
            [ "$#" -gt 0 ] || fail "missing --marker value"
            MARKER=$1
            ;;
        *)
            printf 'unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || fail "run as root"
[ "$CONFIRM" -eq 1 ] || fail "refusing to mutate a GPU device node without --confirm"
[[ "$GPU_INDEX" =~ ^[0-9]+$ ]] || fail "--gpu must be a non-negative integer"
[ -n "$CONTAINER" ] || fail "--container is required"
[ -n "$MEMBER" ] || fail "--member is required"
[ -n "$DEVICE" ] || DEVICE=/dev/nvidia$GPU_INDEX
[ -e "$DEVICE" ] || fail "device node does not exist: $DEVICE"
have docker || fail "docker is required"
have stat || fail "stat is required"
have diff || fail "diff is required"
if [ "$WRITE_MARKER" -eq 1 ]; then
    [ -n "$BYSTANDER" ] || fail "--write-marker requires --bystander so two-user isolation is proven"
fi

RUNNING=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)
[ "$RUNNING" = "true" ] || fail "container is not running: $CONTAINER"
PROFILE=$(docker inspect -f '{{ index .Config.Labels "citespec.profile" }}' "$CONTAINER" 2>/dev/null || true)
[ "$PROFILE" = "development" ] \
    || fail "container is not a Citespec development-profile container (label=$PROFILE)"
if docker exec -u "$MEMBER" "$CONTAINER" sudo -n true >/dev/null 2>&1; then
    ok "container sudo is available to $MEMBER"
else
    fail "container sudo is not available to $MEMBER; cannot run the P0 test"
fi
if docker exec -u "$MEMBER" "$CONTAINER" test -e "$DEVICE" >/dev/null 2>&1; then
    ok "container can see the allocated device: $DEVICE"
else
    fail "container $CONTAINER cannot see $DEVICE; allocate the GPU to this container and retry"
fi

if systemctl is-active --quiet citespec-resource-broker.service 2>/dev/null; then
    warn "broker is active; confirm no other member has an allocation before continuing"
fi

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE_DIR=/var/log/citespec-resource/gpu-isolation-$TIMESTAMP
install -d -o root -g root -m 0700 "$EVIDENCE_DIR" \
    || fail "could not create evidence directory: $EVIDENCE_DIR"
BEFORE=$EVIDENCE_DIR/before.txt
AFTER=$EVIDENCE_DIR/after.txt
TRANSCRIPT=$EVIDENCE_DIR/transcript.txt

record_state() {
    local path
    for path in /dev/nvidia* /dev/nvidia-caps/nvidia*; do
        [ -e "$path" ] || continue
        stat -c '%n inode=%i mode=%a owner=%u:%g' "$path" 2>/dev/null || true
    done | sort
}

restore_device_state() {
    [ -f "$BEFORE" ] || return 0
    local path mode owner
    while IFS= read -r line; do
        path=${line%% *}
        mode=$(printf '%s\n' "$line" | sed -n 's/.* mode=\([0-7]*\).*/\1/p')
        owner=$(printf '%s\n' "$line" | sed -n 's/.* owner=\([0-9]*:[0-9]*\).*/\1/p')
        [ -n "$path" ] && [ -n "$mode" ] && [ -n "$owner" ] || continue
        chown "$owner" "$path" 2>/dev/null || true
        chmod "$mode" "$path" 2>/dev/null || true
    done < "$BEFORE"
}

CHANGED=0
FINISHED=0
cleanup_on_error() {
    local code=$?
    if [ "$FINISHED" -eq 1 ]; then
        return "$code"
    fi
    trap - EXIT
    if [ "$CHANGED" -eq 1 ]; then
        printf '[rollback] restoring original host device mode/owner\n' >&2
        restore_device_state
    fi
    rm -f -- "${MARKER_TMP:-}" || true
    printf '[rollback] evidence retained: %s\n' "$EVIDENCE_DIR" >&2
    exit "$code"
}

record_state > "$BEFORE"
[ -s "$BEFORE" ] || fail "could not record host device state before the test"
ok "recorded pre-test host device state: $BEFORE"

trap cleanup_on_error EXIT
{
    printf 'timestamp=%s\n' "$TIMESTAMP"
    printf 'gpu=%s device=%s container=%s member=%s bystander=%s\n' \
        "$GPU_INDEX" "$DEVICE" "$CONTAINER" "$MEMBER" "${BYSTANDER:-none}"
    printf '\n== container stat ==\n'
    docker exec -u "$MEMBER" "$CONTAINER" sudo -n stat "$DEVICE" 2>&1 || true
    printf '\n== container chmod 0666 ==\n'
    docker exec -u "$MEMBER" "$CONTAINER" sudo -n chmod 0666 "$DEVICE" 2>&1 || true
    printf '\n== container chown 1000:1000 ==\n'
    docker exec -u "$MEMBER" "$CONTAINER" sudo -n chown 1000:1000 "$DEVICE" 2>&1 || true
    printf '\n== host state after ==\n'
    record_state
} | tee "$TRANSCRIPT"

record_state > "$AFTER"
[ -s "$AFTER" ] || fail "could not record host device state after the test"
if diff -u "$BEFORE" "$AFTER" > "$EVIDENCE_DIR/diff.txt"; then
    ok "container root could not change host device mode/owner/inode"
else
    CHANGED=1
    cat "$EVIDENCE_DIR/diff.txt" >&2
    restore_device_state
    record_state > "$EVIDENCE_DIR/after-restored.txt"
    if diff -u "$BEFORE" "$EVIDENCE_DIR/after-restored.txt" >> "$EVIDENCE_DIR/diff.txt"; then
        warn "original mode/owner restored; the test still FAILS because host state changed"
    else
        warn "could not fully restore the original host device state; inspect $EVIDENCE_DIR"
    fi
    fail "P0 FAIL: container root mutated host device state; development profile must remain closed"
fi

if [ -n "$BYSTANDER" ]; then
    if sudo -u "$BYSTANDER" test -r "$DEVICE"; then
        fail "P0 FAIL: non-allocated member $BYSTANDER can read $DEVICE"
    elif sudo -u "$BYSTANDER" test -w "$DEVICE"; then
        fail "P0 FAIL: non-allocated member $BYSTANDER can write $DEVICE"
    else
        ok "non-allocated member $BYSTANDER cannot read or write $DEVICE"
    fi
else
    warn "no --bystander supplied; two-member isolation was not proven"
fi

if [ "$WRITE_MARKER" -eq 1 ]; then
    install -d -o root -g root -m 0700 "$(dirname "$MARKER")" \
        || fail "could not create marker directory: $(dirname "$MARKER")"
    MARKER_TMP=$(mktemp "${MARKER}.tmp.XXXXXX") || fail "could not create marker temp file"
    {
        printf 'created_at=%s\n' "$TIMESTAMP"
        printf 'gpu=%s\n' "$GPU_INDEX"
        printf 'device=%s\n' "$DEVICE"
        printf 'container=%s\n' "$CONTAINER"
        printf 'member=%s\n' "$MEMBER"
        printf 'bystander=%s\n' "$BYSTANDER"
        printf 'evidence=%s\n' "$EVIDENCE_DIR"
    } > "$MARKER_TMP" || fail "could not write marker temp file"
    install -o root -g root -m 0600 "$MARKER_TMP" "$MARKER" \
        || fail "could not install marker: $MARKER"
    rm -f -- "$MARKER_TMP"
    ok "GPU isolation marker written: $MARKER"
fi

FINISHED=1
trap - EXIT
printf '\nP0 PASS: container root could not mutate host GPU device state.\n'
printf 'Evidence retained at %s\n' "$EVIDENCE_DIR"
