#!/usr/bin/env bash
# Create or update a Citespec member/admin Linux account.
set -u
set -o pipefail

DRY_RUN=0
ASSUME_YES=0
ADMIN=0
USERNAME=""
SSH_KEY=""
SSH_KEY_STRING=""
WORKSPACE_ROOT=${CITESPEC_RESOURCE_WORKSPACE_ROOT:-/home/data1/citespec-users}
WORKSPACE_LIMIT_GB=${CITESPEC_RESOURCE_WORKSPACE_LIMIT_GB:-200}
SKIP_WORKSPACE=0
MEMBER_SHELL=/usr/local/libexec/citespec-resource/member-shell
CENTRAL_KEY_DIR=${CITESPEC_RESOURCE_CENTRAL_KEY_DIR:-/etc/citespec-resource/authorized-keys.d}
PYTHON_BIN=${PYTHON_BIN:-}

usage() {
    cat <<'EOF'
usage: create_user.sh [options] <username>

Options:
  --admin                 Create/update an administrator account instead of a member.
  --ssh-key PATH          Install this public key (member: root-managed central
                          store; admin: the administrator's home authorized_keys).
  --ssh-key-string TEXT   Install a literal public key (same destination rules).
  --workspace-root PATH   Workspace parent (default: /home/data1/citespec-users).
  --limit-gb N            Per-member workspace hard quota in GiB (default: 200).
  --no-workspace          Do not create a workspace or configure its quota.
  --dry-run               Print actions without changing the host.
  --yes                   Required for a real (non-dry-run) change.
  -h, --help              Show this help.

Members are placed in citespec-members and citespec-cli and are subject to SSH
ForceCommand. Administrators use citespec-admins as their primary group and keep a
normal shell; they must not be members of citespec-members.

Each account also receives a workspace directory with a filesystem project
quota (project ID = UID, hard limit --limit-gb). The broker re-verifies and,
if needed, reprovisions this quota before every allocation, but creating it
here keeps member provisioning a single transaction.
EOF
}

fail() { printf '[fail] %s\n' "$1" >&2; exit 1; }
note() { printf '[info] %s\n' "$1"; }
ok() { printf '[ok]   %s\n' "$1"; }

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[dry-run] %s\n' "$*"
        return 0
    fi
    if ! "$@"; then
        fail "command failed: $*"
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --admin) ADMIN=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --yes) ASSUME_YES=1 ;;
        --ssh-key)
            shift
            [ "$#" -gt 0 ] || fail "--ssh-key requires a path"
            SSH_KEY=$1
            ;;
        --ssh-key-string)
            shift
            [ "$#" -gt 0 ] || fail "--ssh-key-string requires a value"
            SSH_KEY_STRING=$1
            ;;
        --workspace-root)
            shift
            [ "$#" -gt 0 ] || fail "--workspace-root requires a path"
            WORKSPACE_ROOT=$1
            ;;
        --limit-gb)
            shift
            [ "$#" -gt 0 ] || fail "--limit-gb requires a value"
            WORKSPACE_LIMIT_GB=$1
            ;;
        --no-workspace) SKIP_WORKSPACE=1 ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            printf 'unknown option: %s\n' "$1" >&2
            usage
            exit 2
            ;;
        *)
            [ -z "$USERNAME" ] || fail "only one username may be specified"
            USERNAME=$1
            ;;
    esac
    shift
done

[ -n "$USERNAME" ] || { usage; exit 2; }
[[ "$USERNAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "invalid Linux username: $USERNAME"
[ "$USERNAME" != "root" ] || fail "refusing to modify root"
[[ "$WORKSPACE_LIMIT_GB" =~ ^[1-9][0-9]*$ ]] || fail "limit-gb must be positive"

if [ "$DRY_RUN" -eq 0 ]; then
    [ "$(id -u)" -eq 0 ] || fail "run as root, or use --dry-run"
    [ "$ASSUME_YES" -eq 1 ] || fail "refusing to modify users without --yes"
    [ -x "$MEMBER_SHELL" ] || fail "root-owned member shell is missing: $MEMBER_SHELL"
    SHELL_META=$(stat -c '%a %U %G' "$MEMBER_SHELL" 2>/dev/null || true)
    case "$SHELL_META" in
        "755 root root") ;;
        *) fail "member shell has unsafe ownership/mode: $SHELL_META" ;;
    esac
fi

for group in citespec-members citespec-cli citespec-admins citespec-gpu; do
    getent group "$group" >/dev/null 2>&1 || fail "missing group $group; run deploy/install_server.sh first"
done

if [ "$ADMIN" -eq 1 ]; then
    PRIMARY_GROUP=citespec-admins
    FORBIDDEN_GROUPS=(citespec-members)
    TARGET_SHELL=/bin/bash
else
    PRIMARY_GROUP=citespec-members
    FORBIDDEN_GROUPS=(sudo docker citespec-gpu video render citespec-admins citespec-portal)
    TARGET_SHELL=$MEMBER_SHELL
fi

if [ -n "$SSH_KEY" ]; then
    [ -r "$SSH_KEY" ] || fail "SSH key is not readable: $SSH_KEY"
fi

user_exists() {
    getent passwd "$USERNAME" >/dev/null 2>&1
}

check_forbidden_groups() {
    local groups
    groups=$(id -nG "$USERNAME" 2>/dev/null || true)
    local group
    for group in "${FORBIDDEN_GROUPS[@]}"; do
        if printf '%s\n' "$groups" | tr ' ' '\n' | grep -qx "$group"; then
            fail "$USERNAME is in forbidden group $group"
        fi
    done
}

if user_exists; then
    check_forbidden_groups
fi

if ! user_exists; then
    note "creating $([ "$ADMIN" -eq 1 ] && echo administrator || echo member): $USERNAME"
    # --no-log-init keeps useradd from writing lastlog/faillog.  The provisioning
    # executor runs with ProtectSystem=strict and intentionally has no write
    # access to /var/log; those optional updates must not fail account creation.
    run useradd --no-log-init --create-home --shell "$TARGET_SHELL" --gid "$PRIMARY_GROUP" "$USERNAME"
else
    note "updating existing account: $USERNAME"
    run usermod --shell "$TARGET_SHELL" "$USERNAME"
    if [ "$ADMIN" -eq 0 ]; then
        run usermod -aG citespec-members "$USERNAME"
    else
        run usermod -aG citespec-admins "$USERNAME"
    fi
fi

run usermod -aG citespec-cli "$USERNAME"

# A freshly-created useradd account is locked in /etc/shadow.  OpenSSH rejects
# public-key authentication for a locked account even though the key is valid.
# Give members a random, never-disclosed password hash instead of a password:
# this unlocks the account for public-key auth without enabling password login.
# Reset it on every provisioning retry so a partially-created account recovers.
if [ "$DRY_RUN" -eq 0 ] && [ "$ADMIN" -eq 0 ]; then
    UNLOCK_PW=$(head -c 32 /dev/urandom | base64 | tr -d '\n')
    if ! printf '%s:%s\n' "$USERNAME" "$UNLOCK_PW" | chpasswd; then
        unset UNLOCK_PW
        fail "could not unlock member account: $USERNAME"
    fi
    unset UNLOCK_PW
fi

if [ "$DRY_RUN" -eq 0 ]; then
    check_forbidden_groups
fi

if user_exists; then
    HOME_DIR=$(getent passwd "$USERNAME" | cut -d: -f6)
else
    HOME_DIR=/home/$USERNAME
fi
[ -n "$HOME_DIR" ] || fail "could not determine home directory for $USERNAME"

append_key_nofollow() {
    local line=$1
    "$PYTHON_BIN" - "$USERNAME" "$line" <<'PY'
import os
import pwd
import stat
import sys

user, line = sys.argv[1], sys.argv[2]
record = pwd.getpwnam(user)
home = record.pw_dir
uid, gid = record.pw_uid, record.pw_gid
if not hasattr(os, "O_NOFOLLOW"):
    raise SystemExit("O_NOFOLLOW is required to install SSH keys safely")
dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
home_fd = os.open(home, dir_flags)
try:
    try:
        ssh_fd = os.open(".ssh", dir_flags, dir_fd=home_fd)
    except FileNotFoundError:
        os.mkdir(".ssh", 0o700, dir_fd=home_fd)
        ssh_fd = os.open(".ssh", dir_flags, dir_fd=home_fd)
        os.fchown(ssh_fd, uid, gid)
    try:
        if os.fstat(ssh_fd).st_uid != uid:
            raise SystemExit(f"{home}/.ssh is not owned by {user}")
        os.fchmod(ssh_fd, 0o700)
        file_fd = os.open(
            "authorized_keys",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=ssh_fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise SystemExit("authorized_keys is not a regular file")
            os.fchown(file_fd, uid, gid)
            os.fchmod(file_fd, 0o600)
            chunks = []
            size = 0
            while True:
                chunk = os.read(file_fd, 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 4 * 1024 * 1024:
                    raise SystemExit("authorized_keys is too large")
                chunks.append(chunk)
            key = line.encode("utf-8")
            content = b"".join(chunks)
            if key not in content.splitlines():
                os.lseek(file_fd, 0, os.SEEK_END)
                if content and not content.endswith(b"\n"):
                    os.write(file_fd, b"\n")
                os.write(file_fd, key + b"\n")
        finally:
            os.close(file_fd)
    finally:
        os.close(ssh_fd)
finally:
    os.close(home_fd)
PY
}

append_central_key() {
    local line=$1
    "$PYTHON_BIN" - "$USERNAME" "$line" "$CENTRAL_KEY_DIR" <<'PY'
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

user, line, central = sys.argv[1], sys.argv[2].strip(), sys.argv[3]
parts = line.split()
if len(parts) < 2:
    raise SystemExit("invalid public key")
if not re.fullmatch(
    r"ssh-(rsa|dss|ed25519)|ecdsa-sha2-nistp(256|384|521)", parts[0]
):
    raise SystemExit("unsupported public key type")
if not re.fullmatch(r"[A-Za-z0-9+/=]+", parts[1]):
    raise SystemExit("invalid public key material")
if any("=" in option or option.startswith('"') for option in parts[2:]):
    raise SystemExit("authorized_keys options are not accepted")

directory = Path(central)
if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
    raise SystemExit(f"{directory} is not a safe directory")
directory.mkdir(parents=True, exist_ok=True)
info = directory.stat()
if info.st_uid != 0:
    raise SystemExit(f"{directory} is not owned by root")
os.chmod(directory, 0o755)

target = directory / user
try:
    existing_info = target.lstat()
except FileNotFoundError:
    existing = ""
else:
    if target.is_symlink() or not stat.S_ISREG(existing_info.st_mode):
        raise SystemExit(f"{target} is not a regular file")
    if existing_info.st_uid != 0:
        raise SystemExit(f"{target} is not owned by root")
    existing = target.read_text(encoding="utf-8")

lines = existing.splitlines()
if line not in lines:
    content = (existing + ("" if not existing or existing.endswith("\n") else "\n") + line + "\n")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{user}.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.chown(temp_name, 0, 0)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
os.chmod(target, 0o644)
PY
}

if [ -n "$SSH_KEY" ] || [ -n "$SSH_KEY_STRING" ]; then
    SSH_DIR=$HOME_DIR/.ssh
    AUTHORIZED=$SSH_DIR/authorized_keys
    if [ "$DRY_RUN" -eq 1 ]; then
        if [ "$ADMIN" -eq 1 ]; then
            TARGET_KEY_FILE=$AUTHORIZED
        else
            TARGET_KEY_FILE=$CENTRAL_KEY_DIR/$USERNAME
        fi
        if [ -n "$SSH_KEY" ]; then
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                line=${line%$'\r'}
                printf '[dry-run] append key to %s\n' "$TARGET_KEY_FILE"
            done < "$SSH_KEY"
        fi
        if [ -n "$SSH_KEY_STRING" ]; then
            printf '[dry-run] append literal SSH key to %s\n' "$TARGET_KEY_FILE"
        fi
        note "would install SSH public key for $USERNAME"
    else
        if [ -z "$PYTHON_BIN" ]; then
            if [ -x /opt/citespec-resource/.venv/bin/python ]; then
                PYTHON_BIN=/opt/citespec-resource/.venv/bin/python
            else
                PYTHON_BIN=$(command -v python3 || true)
            fi
        fi
        [ -n "$PYTHON_BIN" ] || fail "python3 is required to install SSH keys safely"
        if [ "$ADMIN" -eq 0 ]; then
            run install -d -o root -g root -m 0755 "$CENTRAL_KEY_DIR"
        fi
        if [ -n "$SSH_KEY" ]; then
            [ -r "$SSH_KEY" ] || fail "cannot read SSH key file: $SSH_KEY"
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                line=${line%$'\r'}
                if [ "$ADMIN" -eq 1 ]; then
                    append_key_nofollow "$line" || fail "could not install SSH key safely"
                else
                    append_central_key "$line" || fail "could not install central SSH key safely"
                fi
            done < "$SSH_KEY"
        fi
        if [ -n "$SSH_KEY_STRING" ]; then
            if [ "$ADMIN" -eq 1 ]; then
                append_key_nofollow "$SSH_KEY_STRING" || fail "could not install SSH key safely"
            else
                append_central_key "$SSH_KEY_STRING" || fail "could not install central SSH key safely"
            fi
        fi
        ok "SSH public key installed for $USERNAME"
    fi
fi

run install -d -o root -g root -m 0755 "$WORKSPACE_ROOT"

# ---------------------------------------------------------------------------
# Workspace provisioning transaction: create the directory, assign the
# project ID (convention: project ID = UID), set the hard quota, and verify
# the result by querying the filesystem back.
# ---------------------------------------------------------------------------
provision_workspace() {
    local ws_dir=$1 uid=$2 mount fstype
    mount=$(findmnt -no TARGET -T "$ws_dir" | head -n 1) \
        || fail "cannot determine mount for $ws_dir"
    fstype=$(findmnt -no FSTYPE -T "$ws_dir" | head -n 1) \
        || fail "cannot determine filesystem type"
    [ -n "$mount" ] || fail "cannot determine mount for $ws_dir"
    [ -n "$fstype" ] || fail "cannot determine filesystem type"
    local soft_gb=$((WORKSPACE_LIMIT_GB / 2))
    [ "$soft_gb" -ge 1 ] || soft_gb=1
    if [ "$fstype" = "xfs" ]; then
        command -v xfs_quota >/dev/null 2>&1 || fail "xfs_quota not found"
        xfs_quota -x -c "project -s -p $ws_dir $uid" "$mount" \
            || fail "could not assign project ID $uid to $ws_dir"
        xfs_quota -x -c "limit -p bsoft=${soft_gb}g bhard=${WORKSPACE_LIMIT_GB}g $uid" "$mount" \
            || fail "could not set XFS project quota for $uid"
    elif [ "$fstype" = "ext4" ] || [ "$fstype" = "ext3" ] || [ "$fstype" = "ext2" ]; then
        command -v chattr >/dev/null 2>&1 || fail "chattr not found"
        command -v setquota >/dev/null 2>&1 || fail "setquota not found"
        chattr -R -p "$uid" "$ws_dir" || fail "could not assign project ID $uid to $ws_dir"
        find "$ws_dir" -type d -exec chattr +P {} + \
            || fail "could not enable project inheritance on $ws_dir"
        local soft_kib=$((soft_gb * 1024 * 1024)) hard_kib=$((WORKSPACE_LIMIT_GB * 1024 * 1024))
        setquota -P "$uid" "$soft_kib" "$hard_kib" 0 0 "$mount" \
            || fail "could not set ext4 project quota for $uid"
    else
        fail "unsupported workspace filesystem $fstype; use XFS/ext4 project quotas"
    fi
}

verify_workspace_quota() {
    local ws_dir=$1 uid=$2 mount fstype hard projid
    projid=$(lsattr -p -d "$ws_dir" 2>/dev/null | awk '{print $1}')
    [ "$projid" = "$uid" ] || fail "project ID of $ws_dir is '$projid', expected $uid"
    mount=$(findmnt -no TARGET -T "$ws_dir" | head -n 1) \
        || fail "cannot determine mount for $ws_dir"
    fstype=$(findmnt -no FSTYPE -T "$ws_dir" | head -n 1) \
        || fail "cannot determine filesystem type"
    [ -n "$mount" ] || fail "cannot determine mount for $ws_dir"
    [ -n "$fstype" ] || fail "cannot determine filesystem type"
    if [ "$fstype" = "xfs" ]; then
        # Accept the same block sizes xfs_quota may report (1K/2K/4K/1M).
        hard=$(xfs_quota -x -c "report -p -b" "$mount" 2>/dev/null \
            | awk -v u="$uid" '$1==u || $1=="#"u {print $4; exit}')
        case "$hard" in
            "$((WORKSPACE_LIMIT_GB * 1024 * 1024))"|\
            "$((WORKSPACE_LIMIT_GB * 2048 * 1024))"|\
            "$((WORKSPACE_LIMIT_GB * 256 * 1024))"|\
            "$WORKSPACE_LIMIT_GB") ;;
            *) fail "XFS hard limit for project $uid is '$hard', expected ${WORKSPACE_LIMIT_GB}g" ;;
        esac
    else
        hard=$(repquota -P "$mount" 2>/dev/null \
            | awk -v u="#$uid" '$1==u {c=0; for (i=3; i<=NF; i++) if ($i ~ /^[0-9]+$/) {c++; if (c==3) {print $i; exit}}}')
        [ "$hard" = "$((WORKSPACE_LIMIT_GB * 1024 * 1024))" ] \
            || fail "ext4 hard limit for project $uid is '$hard', expected ${WORKSPACE_LIMIT_GB}g"
    fi
    ok "workspace quota verified: $ws_dir (project $uid, hard ${WORKSPACE_LIMIT_GB}g)"
}

if [ "$SKIP_WORKSPACE" -eq 0 ]; then
    WS_DIR="$WORKSPACE_ROOT/$USERNAME"
    if [ "$DRY_RUN" -eq 1 ]; then
        note "would create workspace $WS_DIR (project ID = UID, hard ${WORKSPACE_LIMIT_GB}g) and verify it"
    else
        uid=$(id -u "$USERNAME") || fail "could not resolve UID for $USERNAME"
        primary_gid=$(id -g "$USERNAME") || fail "could not resolve primary GID for $USERNAME"
        run install -d -o "$uid" -g "$primary_gid" -m 0700 "$WS_DIR"
        provision_workspace "$WS_DIR" "$uid"
        verify_workspace_quota "$WS_DIR" "$uid"
    fi
fi

if [ "$DRY_RUN" -eq 0 ]; then
    ok "account ready: $USERNAME"
    printf 'groups: %s\n' "$(id -nG "$USERNAME")"
    if [ "$ADMIN" -eq 1 ]; then
        printf 'administrator: normal shell, no ForceCommand; not in citespec-members\n'
    else
        printf 'member: root-owned login shell, SSH ForceCommand, no sudo/docker/GPU device groups\n'
    fi
else
    printf '[dry-run] account would be ready: %s\n' "$USERNAME"
fi
