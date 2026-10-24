#!/usr/bin/env bash
# Create or update a UDC member/admin Linux account.
set -u
set -o pipefail

DRY_RUN=0
ASSUME_YES=0
ADMIN=0
USERNAME=""
SSH_KEY=""
SSH_KEY_STRING=""
WORKSPACE_ROOT=${UDC_WORKSPACE_ROOT:-/data1/udc-users}
MEMBER_SHELL=/usr/local/libexec/udc-resource/member-shell
PYTHON_BIN=${PYTHON_BIN:-}

usage() {
    cat <<'EOF'
usage: create_user.sh [options] <username>

Options:
  --admin                 Create/update an administrator account instead of a member.
  --ssh-key PATH          Install this public key into authorized_keys.
  --ssh-key-string TEXT   Install a literal public key into authorized_keys.
  --workspace-root PATH   Workspace parent (default: /data1/udc-users).
  --dry-run               Print actions without changing the host.
  --yes                   Required for a real (non-dry-run) change.
  -h, --help              Show this help.

Members are placed in udc-members and udc-cli and are subject to SSH
ForceCommand. Administrators use udc-admins as their primary group and keep a
normal shell; they must not be members of udc-members.
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

for group in udc-members udc-cli udc-admins udc-gpu; do
    getent group "$group" >/dev/null 2>&1 || fail "missing group $group; run deploy/install_server.sh first"
done

if [ "$ADMIN" -eq 1 ]; then
    PRIMARY_GROUP=udc-admins
    FORBIDDEN_GROUPS=(udc-members)
    TARGET_SHELL=/bin/bash
else
    PRIMARY_GROUP=udc-members
    FORBIDDEN_GROUPS=(sudo docker udc-gpu video render udc-admins)
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
    run useradd --create-home --shell "$TARGET_SHELL" --gid "$PRIMARY_GROUP" "$USERNAME"
else
    note "updating existing account: $USERNAME"
    run usermod --shell "$TARGET_SHELL" "$USERNAME"
    if [ "$ADMIN" -eq 0 ]; then
        run usermod -aG udc-members "$USERNAME"
    else
        run usermod -aG udc-admins "$USERNAME"
    fi
fi

run usermod -aG udc-cli "$USERNAME"

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

if [ -n "$SSH_KEY" ] || [ -n "$SSH_KEY_STRING" ]; then
    SSH_DIR=$HOME_DIR/.ssh
    AUTHORIZED=$SSH_DIR/authorized_keys
    if [ "$DRY_RUN" -eq 1 ]; then
        if [ -n "$SSH_KEY" ]; then
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                line=${line%$'\r'}
                printf '[dry-run] append key to %s\n' "$AUTHORIZED"
            done < "$SSH_KEY"
        fi
        if [ -n "$SSH_KEY_STRING" ]; then
            printf '[dry-run] append literal SSH key to %s\n' "$AUTHORIZED"
        fi
        note "would install SSH public key for $USERNAME"
    else
        if [ -z "$PYTHON_BIN" ]; then
            if [ -x /opt/udc-resource/.venv/bin/python ]; then
                PYTHON_BIN=/opt/udc-resource/.venv/bin/python
            else
                PYTHON_BIN=$(command -v python3 || true)
            fi
        fi
        [ -n "$PYTHON_BIN" ] || fail "python3 is required to install SSH keys safely"
        if [ -n "$SSH_KEY" ]; then
            [ -r "$SSH_KEY" ] || fail "cannot read SSH key file: $SSH_KEY"
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                line=${line%$'\r'}
                append_key_nofollow "$line" || fail "could not install SSH key safely"
            done < "$SSH_KEY"
        fi
        if [ -n "$SSH_KEY_STRING" ]; then
            append_key_nofollow "$SSH_KEY_STRING" || fail "could not install SSH key safely"
        fi
        ok "SSH public key installed for $USERNAME"
    fi
fi

run install -d -o root -g root -m 0755 "$WORKSPACE_ROOT"

if [ "$DRY_RUN" -eq 0 ]; then
    ok "account ready: $USERNAME"
    printf 'groups: %s\n' "$(id -nG "$USERNAME")"
    if [ "$ADMIN" -eq 1 ]; then
        printf 'administrator: normal shell, no ForceCommand; not in udc-members\n'
    else
        printf 'member: root-owned login shell, SSH ForceCommand, no sudo/docker/GPU device groups\n'
    fi
else
    printf '[dry-run] account would be ready: %s\n' "$USERNAME"
fi
