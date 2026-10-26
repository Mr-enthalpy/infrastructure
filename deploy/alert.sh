#!/usr/bin/env bash
# Send a failure alert from a systemd OnFailure= unit. The local journal/alert
# log is always written; a webhook is optional and configured in
# /etc/citespec-resource/alert.env as CITESPEC_ALERT_WEBHOOK=...
set -u
set -o pipefail

usage() {
    cat <<'EOF'
usage: alert.sh [unit-name]

Record a Citespec failure in the journal and alert log. If
/etc/citespec-resource/alert.env sets CITESPEC_ALERT_WEBHOOK, also POST a JSON
message to that URL with curl.

Options:
  -h, --help           Show this help.

Exit status: 0 when the local alert was recorded; 1 if a configured webhook
delivery failed.
EOF
}

case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
esac

UNIT=${1:-unknown}
CONFIG_DIR=${CITESPEC_CONFIG_DIR:-/etc/citespec-resource}
LOG_DIR=${CITESPEC_ALERT_LOG_DIR:-/var/log/citespec-resource}
LOG_FILE=$LOG_DIR/alerts.log
HOST=$(hostname -f 2>/dev/null || hostname 2>/dev/null || printf 'unknown-host')
STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

mkdir -p -m 0750 "$LOG_DIR" 2>/dev/null || true
if [ -f "$LOG_FILE" ]; then
    SIZE=$(stat -c '%s' "$LOG_FILE" 2>/dev/null || printf '0')
    if [ "$SIZE" -gt 1048576 ] 2>/dev/null; then
        mv -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
    fi
fi
printf '%s unit=%s host=%s\n' "$STAMP" "$UNIT" "$HOST" >> "$LOG_FILE" 2>/dev/null || true
if command -v logger >/dev/null 2>&1; then
    logger -t citespec-alert -p daemon.err -- "$UNIT failed on $HOST at $STAMP" 2>/dev/null || true
fi

WEBHOOK=${CITESPEC_ALERT_WEBHOOK:-}
if [ -r "$CONFIG_DIR/alert.env" ]; then
    # shellcheck disable=SC1091
    . "$CONFIG_DIR/alert.env"
fi
WEBHOOK=${CITESPEC_ALERT_WEBHOOK:-}
[ -n "$WEBHOOK" ] || exit 0

if ! command -v curl >/dev/null 2>&1; then
    printf 'curl is required to deliver CITESPEC_ALERT_WEBHOOK\n' >&2
    exit 1
fi

ESCAPED_UNIT=${UNIT//\\/\\\\}
ESCAPED_UNIT=${ESCAPED_UNIT//\"/\\\"}
ESCAPED_HOST=${HOST//\\/\\\\}
ESCAPED_HOST=${ESCAPED_HOST//\"/\\\"}
BODY=$(printf '{"text":"Citespec alert: %s failed on %s at %s"}' "$ESCAPED_UNIT" "$ESCAPED_HOST" "$STAMP")
if ! curl --fail --silent --show-error --max-time 10 --retry 2 \
    -H 'Content-Type: application/json' --data-binary "$BODY" "$WEBHOOK" >/dev/null; then
    printf 'alert webhook delivery failed for %s\n' "$UNIT" >&2
    exit 1
fi
exit 0
