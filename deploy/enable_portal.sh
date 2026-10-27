#!/usr/bin/env bash
# Enable and start the Portal only after the fail-closed network audit passes.
#
# The installer never enables the Portal.  This script is the single gate:
#   1. verify config [portal].enabled = true;
#   2. run verify_portal_network.sh --acceptance with the approved sources;
#   3. write /etc/citespec-resource/portal-network-accepted;
#   4. systemctl enable --now, or roll the marker/enablement back on failure.
#
# Rollback:
#   systemctl disable --now citespec-resource-portal.service
#   rm -f /etc/citespec-resource/portal-network-accepted
set -u
set -o pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_FILE=/etc/citespec-resource/config.toml
MARKER=/etc/citespec-resource/portal-network-accepted
PORTAL_UNIT=citespec-resource-portal.service
PORTAL_PORT=8770
NGINX_ROOT=/etc/nginx
PORTAL_HTTPS_PORT=443
APPROVED_CIDRS=()
APPROVED_CIDR_FILE=""
ASSUME_YES=0
COMMITTED=0
MARKER_PREEXISTED=0
UNIT_WAS_ENABLED=0

usage() {
    cat <<EOF
usage: $0 --yes --approved-cidr CIDR [--approved-cidr CIDR]...
          [--approved-cidr-file PATH] [--config PATH] [--nginx-root PATH]
          [--portal-port PORT] [--portal-https-port PORT]

Runs the fail-closed Portal network/TLS/source audit and only then enables and
starts $PORTAL_UNIT.  --yes is mandatory.

Approved CIDRs are the campus/VPN source ranges that may reach the Portal.
They are also written into the nginx allow rules by the operator.
EOF
}

fail() { printf '[fail] %s\n' "$1" >&2; exit 1; }
note() { printf '[info] %s\n' "$1"; }
ok() { printf '[ok]   %s\n' "$1"; }

find_python() {
    if [ -n "${CITESPEC_PYTHON:-}" ]; then
        printf '%s\n' "$CITESPEC_PYTHON"
        return 0
    fi
    # Prefer the deployed runtime: the system python3 may be older than the
    # 3.11 baseline required by the installer.
    if [ -x /opt/citespec-resource/.venv/bin/python ]; then
        printf '%s\n' /opt/citespec-resource/.venv/bin/python
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi
    if command -v python >/dev/null 2>&1; then
        command -v python
        return 0
    fi
    return 1
}

cleanup_on_error() {
    local code=$?
    if [ "$code" -eq 0 ] || [ "$COMMITTED" -eq 1 ]; then
        return "$code"
    fi
    trap - EXIT
    printf '[rollback] undoing the partial Portal enable attempt\n' >&2
    # Only undo state this run introduced.  A re-run on an already accepted
    # Portal must not disable it or delete its marker because a later step
    # failed.
    if [ "$MARKER_PREEXISTED" -eq 0 ]; then
        rm -f -- "${MARKER_TMP:-}" "$MARKER" || true
        printf '[rollback] acceptance marker removed\n' >&2
    else
        rm -f -- "${MARKER_TMP:-}" || true
        printf '[rollback] kept the pre-existing acceptance marker\n' >&2
    fi
    if [ "$UNIT_WAS_ENABLED" -eq 0 ]; then
        systemctl disable --now "$PORTAL_UNIT" >/dev/null 2>&1 || true
        printf '[rollback] portal enablement removed\n' >&2
    else
        printf '[rollback] kept the pre-existing portal enablement\n' >&2
    fi
    printf '[rollback] fix the audit/runtime findings and retry\n' >&2
    exit "$code"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --yes) ASSUME_YES=1 ;;
        --approved-cidr)
            shift
            [ "$#" -gt 0 ] || fail "missing --approved-cidr value"
            APPROVED_CIDRS+=("$1")
            ;;
        --approved-cidr-file)
            shift
            [ "$#" -gt 0 ] || fail "missing --approved-cidr-file value"
            APPROVED_CIDR_FILE=$1
            ;;
        --config)
            shift
            [ "$#" -gt 0 ] || fail "missing --config value"
            CONFIG_FILE=$1
            ;;
        --nginx-root)
            shift
            [ "$#" -gt 0 ] || fail "missing --nginx-root value"
            NGINX_ROOT=$1
            ;;
        --portal-port)
            shift
            [ "$#" -gt 0 ] || fail "missing --portal-port value"
            PORTAL_PORT=$1
            ;;
        --portal-https-port)
            shift
            [ "$#" -gt 0 ] || fail "missing --portal-https-port value"
            PORTAL_HTTPS_PORT=$1
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
[ "$ASSUME_YES" -eq 1 ] || fail "refusing to enable the Portal without --yes"
[ -f "$CONFIG_FILE" ] || fail "config not found: $CONFIG_FILE"
[ -x "$SCRIPT_DIR/verify_portal_network.sh" ] \
    || fail "verify_portal_network.sh is missing or not executable next to $0"

PYTHON_BIN=$(find_python) || fail "Python 3 is required to read the config"
PORTAL_ENABLED=$("$PYTHON_BIN" - "$CONFIG_FILE" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    print("true" if tomllib.load(handle).get("portal", {}).get("enabled", False) else "false")
PY
) || fail "could not read portal.enabled from $CONFIG_FILE"
[ "$PORTAL_ENABLED" = "true" ] \
    || fail "[portal].enabled is false in $CONFIG_FILE; enable it only after Portal acceptance"

AUDIT_ARGS=(
    --acceptance
    --nginx-root "$NGINX_ROOT"
    --portal-port "$PORTAL_PORT"
    --portal-https-port "$PORTAL_HTTPS_PORT"
)
for cidr in "${APPROVED_CIDRS[@]}"; do
    AUDIT_ARGS+=(--approved-cidr "$cidr")
done
if [ -n "$APPROVED_CIDR_FILE" ]; then
    AUDIT_ARGS+=(--approved-cidr-file "$APPROVED_CIDR_FILE")
fi

note "running fail-closed Portal network audit"
"$SCRIPT_DIR/verify_portal_network.sh" "${AUDIT_ARGS[@]}" \
    || fail "portal network audit failed; Portal remains disabled"
ok "portal network audit passed"

# Record the pre-existing state so a failure only undoes this run's changes.
if [ -e "$MARKER" ]; then
    MARKER_PREEXISTED=1
fi
if systemctl is-enabled --quiet "$PORTAL_UNIT" 2>/dev/null; then
    UNIT_WAS_ENABLED=1
fi
trap cleanup_on_error EXIT
umask 077
MARKER_TMP=$(mktemp "${MARKER}.tmp.XXXXXX") || fail "could not create marker temp file"
{
    printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'unit=%s\n' "$PORTAL_UNIT"
    printf 'script=enable_portal.sh\n'
} > "$MARKER_TMP" || fail "could not write marker temp file"
install -o root -g root -m 0600 "$MARKER_TMP" "$MARKER" \
    || fail "could not install acceptance marker"
rm -f -- "$MARKER_TMP"
ok "acceptance marker written: $MARKER"

note "enabling and starting $PORTAL_UNIT"
systemctl enable --now "$PORTAL_UNIT" \
    || fail "systemctl enable --now $PORTAL_UNIT failed"
sleep 1
systemctl is-active --quiet "$PORTAL_UNIT" \
    || fail "$PORTAL_UNIT did not become active; inspect journalctl -u $PORTAL_UNIT"

COMMITTED=1
trap - EXIT
ok "Portal enabled after acceptance"
printf '\nRollback:\n'
printf '  systemctl disable --now %s\n' "$PORTAL_UNIT"
printf '  rm -f %s\n' "$MARKER"
