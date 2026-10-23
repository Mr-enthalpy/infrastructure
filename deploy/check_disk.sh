#!/usr/bin/env bash
# Check workspace free space and filesystem usage. Suitable for cron/systemd.
set -u
set -o pipefail

ROOT=/data1
WARN_PERCENT=85
CRIT_PERCENT=95
MIN_FREE_GB=0

usage() {
    cat <<'EOF'
usage: check_disk.sh [options]

Options:
  --path PATH          Workspace/data path to inspect (default: /data1).
  --warn-percent N     Warning threshold (default: 85).
  --crit-percent N     Critical threshold (default: 95).
  --min-free-gb N      Minimum free GiB before failure (default: 0).
  -h, --help           Show this help.

Exit status: 0 healthy, 1 warning, 2 critical/error.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --path)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --path value\n' >&2; exit 2; }
            ROOT=$1
            ;;
        --warn-percent)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --warn-percent value\n' >&2; exit 2; }
            WARN_PERCENT=$1
            ;;
        --crit-percent)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --crit-percent value\n' >&2; exit 2; }
            CRIT_PERCENT=$1
            ;;
        --min-free-gb)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --min-free-gb value\n' >&2; exit 2; }
            MIN_FREE_GB=$1
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

[[ "$WARN_PERCENT" =~ ^[0-9]+$ ]] || { printf 'warn threshold must be an integer\n' >&2; exit 2; }
[[ "$CRIT_PERCENT" =~ ^[0-9]+$ ]] || { printf 'critical threshold must be an integer\n' >&2; exit 2; }
[[ "$MIN_FREE_GB" =~ ^[0-9]+$ ]] || { printf 'min-free-gb must be an integer\n' >&2; exit 2; }
[ "$WARN_PERCENT" -lt "$CRIT_PERCENT" ] || { printf 'warn threshold must be below critical\n' >&2; exit 2; }
[ -e "$ROOT" ] || { printf 'path does not exist: %s\n' "$ROOT" >&2; exit 2; }
command -v df >/dev/null 2>&1 || { printf 'df not found\n' >&2; exit 2; }

read -r AVAILABLE PERCENT <<<"$(df -P -B1 "$ROOT" | awk 'NR==2 {print $4, $5}')"
PERCENT=${PERCENT%\%}
if ! [[ "$AVAILABLE" =~ ^[0-9]+$ && "$PERCENT" =~ ^[0-9]+$ ]]; then
    printf 'could not parse df output for %s\n' "$ROOT" >&2
    exit 2
fi
FREE_GB=$((AVAILABLE / 1024 / 1024 / 1024))

printf '[info] %s: %s GiB free, %s%% used\n' "$ROOT" "$FREE_GB" "$PERCENT"
if [ "$PERCENT" -ge "$CRIT_PERCENT" ] || [ "$FREE_GB" -lt "$MIN_FREE_GB" ]; then
    printf '[critical] workspace disk threshold exceeded\n' >&2
    exit 2
fi
if [ "$PERCENT" -ge "$WARN_PERCENT" ]; then
    printf '[warning] workspace disk usage is high\n' >&2
    exit 1
fi
printf '[ok] workspace disk usage is healthy\n'
