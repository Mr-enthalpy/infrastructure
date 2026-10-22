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
fi

for group in udc-members udc-cli udc-admins udc-gpu; do
    getent group "$group" >/dev/null 2>&1 || fail "missing group $group; run deploy/install_server.sh first"
done

if [ "$ADMIN" -eq 1 ]; then
    PRIMARY_GROUP=udc-admins
    FORBIDDEN_GROUPS=(udc-members)
else
    PRIMARY_GROUP=udc-members
    FORBIDDEN_GROUPS=(sudo docker udc-gpu video render udc-admins)
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
    run useradd --create-home --shell /bin/bash --gid "$PRIMARY_GROUP" "$USERNAME"
else
    note "updating existing account: $USERNAME"
    run usermod --shell /bin/bash "$USERNAME"
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
    PRIMARY_GROUP_NAME=$(id -gn "$USERNAME")
else
    HOME_DIR=/home/$USERNAME
    PRIMARY_GROUP_NAME=$PRIMARY_GROUP
fi
[ -n "$HOME_DIR" ] || fail "could not determine home directory for $USERNAME"

install_key_lines() {
    local source=$1
    local authorized=$2
    local line
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '[dry-run] append key to %s\n' "$authorized"
            continue
        fi
        if ! grep -qxF -- "$line" "$authorized" 2>/dev/null; then
            printf '%s\n' "$line" >> "$authorized" || fail "could not append SSH key"
        fi
    done < "$source"
}

if [ -n "$SSH_KEY" ] || [ -n "$SSH_KEY_STRING" ]; then
    SSH_DIR=$HOME_DIR/.ssh
    AUTHORIZED=$SSH_DIR/authorized_keys
    if [ -L "$SSH_DIR" ] || [ -L "$AUTHORIZED" ]; then
        fail "refusing to follow a symlink in $SSH_DIR"
    fi
    run install -d -m 0700 -o "$USERNAME" -g "$PRIMARY_GROUP_NAME" "$SSH_DIR"
    if [ "$DRY_RUN" -eq 0 ]; then
        touch "$AUTHORIZED" || fail "could not create $AUTHORIZED"
        chown "$USERNAME:$PRIMARY_GROUP_NAME" "$AUTHORIZED" || fail "could not chown $AUTHORIZED"
        chmod 0600 "$AUTHORIZED" || fail "could not chmod $AUTHORIZED"
    fi
    if [ -n "$SSH_KEY" ]; then
        install_key_lines "$SSH_KEY" "$AUTHORIZED"
    fi
    if [ -n "$SSH_KEY_STRING" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '[dry-run] append literal SSH key to %s\n' "$AUTHORIZED"
        else
            TMP_KEY=$(mktemp) || fail "could not create temporary key file"
            printf '%s\n' "$SSH_KEY_STRING" > "$TMP_KEY" || fail "could not write temporary key file"
            install_key_lines "$TMP_KEY" "$AUTHORIZED"
            rm -f "$TMP_KEY"
        fi
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        note "would install SSH public key for $USERNAME"
    else
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
        printf 'member: SSH ForceCommand, no sudo/docker/GPU device groups\n'
    fi
else
    printf '[dry-run] account would be ready: %s\n' "$USERNAME"
fi
