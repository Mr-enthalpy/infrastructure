#!/usr/bin/env bash
# Read-only network audit for the Portal and legacy apply front ends.
#
# It answers three questions without changing host state:
#   1. Do the nginx vhosts bind a specific private address (never 0.0.0.0/[::])?
#   2. Do they restrict sources with allow/deny instead of relying on the
#      firewall alone, and proxy only to loopback?
#   3. Does the portal/apply upstream listen on loopback only, and what is the
#      portal unit's current state?
#
# Exit status: 0 when no failures, 1 when any check failed.  Warnings do not
# fail the audit (for example, Portal is intentionally disabled in P0).
set -u
set -o pipefail

FAIL=0
WARN=0
NGINX_ROOT=/etc/nginx
PORTAL_PORT=8770
APPLY_PORT=8765
PORTAL_UNIT=citespec-resource-portal.service
EXPECT_PORTAL_LISTENER=0

section() { printf '\n== %s ==\n' "$1"; }
ok() { printf '[ok]   %s\n' "$1"; }
note() { printf '[note] %s\n' "$1"; }
warn() { printf '[warn] %s\n' "$1"; WARN=$((WARN + 1)); }
fail() { printf '[fail] %s\n' "$1"; FAIL=$((FAIL + 1)); }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<EOF
usage: $0 [--nginx-root PATH] [--portal-port PORT] [--apply-port PORT]

Read-only audit of nginx -> Portal/apply listeners and source restrictions.

  --nginx-root PATH   nginx configuration root (default: $NGINX_ROOT)
  --portal-port PORT  Portal upstream port (default: $PORTAL_PORT)
  --apply-port PORT   Legacy apply upstream port (default: $APPLY_PORT)
  -h, --help          Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --nginx-root)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --nginx-root value\n' >&2; exit 2; }
            NGINX_ROOT=$1
            ;;
        --portal-port)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --portal-port value\n' >&2; exit 2; }
            PORTAL_PORT=$1
            ;;
        --apply-port)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --apply-port value\n' >&2; exit 2; }
            APPLY_PORT=$1
            ;;
        *)
            printf 'unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

# Return the local addresses listening on a TCP port, one per line.  Falls back
# silently when ss is unavailable; the caller reports that as a warning.
listeners_for_port() {
    local port=$1
    have ss || return 1
    ss -ltn 2>/dev/null | awk -v port=":$port" 'NR > 1 && $4 ~ port"$" { print $4 }'
}

is_loopback_address() {
    case "$1" in
        127.0.0.1:*|\[::1\]:*) return 0 ;;
        *) return 1 ;;
    esac
}

check_upstream_listener() {
    local port=$1 name=$2 found=0 address
    local listeners
    if ! listeners=$(listeners_for_port "$port"); then
        warn "ss is not available; cannot verify the $name listener on port $port"
        return 0
    fi
    while IFS= read -r address; do
        [ -n "$address" ] || continue
        found=1
        if is_loopback_address "$address"; then
            ok "$name upstream listens on loopback: $address"
        else
            fail "$name upstream is exposed beyond loopback: $address (expected 127.0.0.1:$port)"
        fi
    done <<< "$listeners"
    if [ "$found" -eq 0 ]; then
        warn "no TCP listener on port $port; $name is not running or not installed"
    fi
}

check_vhost() {
    local file=$1 name=$2 port=$3
    local listen_lines allow_count deny_all=0
    listen_lines=$(grep -Ei '^[[:space:]]*listen[[:space:]]' "$file" 2>/dev/null || true)
    if [ -z "$listen_lines" ]; then
        fail "$name vhost has no listen directive: $file"
    else
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            if printf '%s' "$line" | grep -Eq 'listen[[:space:]]+(0\.0\.0\.0|\[::\]|\*):'; then
                fail "$name vhost binds a wildcard address: $line"
            elif printf '%s' "$line" | grep -Eq '<[^>]+>'; then
                fail "$name vhost still contains an unreplaced listen placeholder: $line"
            else
                ok "$name vhost binds a specific address: $(printf '%s' "$line" | sed 's/^[[:space:]]*//')"
            fi
        done <<< "$listen_lines"
    fi

    allow_count=$(grep -Ec '^[[:space:]]*allow[[:space:]]+[0-9A-Fa-f:.]+/[0-9]+;' "$file" 2>/dev/null || true)
    if grep -Eq '^[[:space:]]*deny[[:space:]]+all;' "$file" 2>/dev/null; then
        deny_all=1
    fi
    if [ "${allow_count:-0}" -gt 0 ] && [ "$deny_all" -eq 1 ]; then
        ok "$name vhost has $allow_count allow rule(s) followed by deny all"
    else
        fail "$name vhost must restrict sources with allow <vpn-source-cidr>; ... deny all; ($file)"
    fi

    if grep -Eq "^[[:space:]]*proxy_pass[[:space:]]+http://127\\.0\\.0\\.1:${port};" "$file" 2>/dev/null; then
        ok "$name proxies only to http://127.0.0.1:$port"
    else
        fail "$name vhost must proxy to http://127.0.0.1:$port ($file)"
    fi

    if grep -Eq 'X-Citespec-Client-IP[[:space:]]+\$http_' "$file" 2>/dev/null; then
        fail "$name vhost forwards a client-supplied X-Citespec-Client-IP header ($file)"
    elif grep -Eq 'X-Citespec-Client-IP[[:space:]]+\$remote_addr' "$file" 2>/dev/null; then
        ok "$name vhost overwrites X-Citespec-Client-IP with \$remote_addr"
    else
        warn "$name vhost does not set X-Citespec-Client-IP from \$remote_addr ($file)"
    fi
}

section "nginx vhosts"
VHOST_FILES=()
if [ -d "$NGINX_ROOT" ]; then
    while IFS= read -r file; do
        [ -n "$file" ] && VHOST_FILES+=("$file")
    done < <(grep -rlE 'server_name[[:space:]]+citespec-(portal|apply)\.' "$NGINX_ROOT" 2>/dev/null || true)
else
    warn "nginx configuration root does not exist: $NGINX_ROOT"
fi

PORTAL_VHOSTS=()
APPLY_VHOSTS=()
if [ "${#VHOST_FILES[@]}" -gt 0 ]; then
    for file in "${VHOST_FILES[@]}"; do
        if grep -Eq 'server_name[[:space:]]+citespec-portal\.' "$file" 2>/dev/null; then
            PORTAL_VHOSTS+=("$file")
            check_vhost "$file" "portal" "$PORTAL_PORT"
        fi
        if grep -Eq 'server_name[[:space:]]+citespec-apply\.' "$file" 2>/dev/null; then
            APPLY_VHOSTS+=("$file")
            check_vhost "$file" "apply" "$APPLY_PORT"
        fi
    done
fi
if [ "${#PORTAL_VHOSTS[@]}" -eq 0 ]; then
    warn "no nginx portal vhost found under $NGINX_ROOT"
fi
if [ "${#APPLY_VHOSTS[@]}" -eq 0 ]; then
    warn "no nginx apply vhost found under $NGINX_ROOT"
fi

section "upstream listeners"
check_upstream_listener "$PORTAL_PORT" "portal"
check_upstream_listener "$APPLY_PORT" "apply"

# Catch a wildcard listener added directly (without nginx) on the upstream
# ports.  check_upstream_listener already fails non-loopback addresses; this
# repeats the distinction explicitly for 0.0.0.0/[::].
if have ss; then
    for port in "$PORTAL_PORT" "$APPLY_PORT"; do
        if ss -ltn 2>/dev/null | awk -v port=":$port" 'NR > 1 && $4 ~ port"$" { print $4 }' \
            | grep -Eq '^(0\.0\.0\.0|\[::\]):'; then
            fail "port $port has a wildcard listener"
        fi
    done
fi

section "portal unit"
if have systemctl; then
    if systemctl is-active --quiet "$PORTAL_UNIT" 2>/dev/null; then
        ok "$PORTAL_UNIT is active"
        EXPECT_PORTAL_LISTENER=1
    else
        if systemctl is-enabled --quiet "$PORTAL_UNIT" 2>/dev/null; then
            warn "$PORTAL_UNIT is enabled but not active"
        else
            note "$PORTAL_UNIT is not enabled (expected while Portal is disabled)"
        fi
    fi
    if systemctl is-enabled --quiet "$PORTAL_UNIT" 2>/dev/null; then
        ok "$PORTAL_UNIT is enabled"
    else
        note "$PORTAL_UNIT is not enabled"
    fi
else
    warn "systemctl is not available; cannot inspect $PORTAL_UNIT"
fi
if [ "$EXPECT_PORTAL_LISTENER" -eq 1 ] && have ss; then
    if ss -ltn 2>/dev/null | awk -v port=":$PORTAL_PORT" 'NR > 1 && $4 ~ port"$" { print $4 }' | grep -q .; then
        ok "active portal has a listener on port $PORTAL_PORT"
    else
        fail "portal unit is active but no listener was found on port $PORTAL_PORT"
    fi
fi

section "summary"
printf 'failures: %d, warnings: %d\n' "$FAIL" "$WARN"
if [ "$FAIL" -ne 0 ]; then
    printf 'portal network audit found blocking problems.\n'
    exit 1
fi
printf 'portal network audit passed (read-only).\n'
