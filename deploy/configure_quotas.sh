#!/usr/bin/env bash
# Configure per-directory project quotas for member workspaces.
#
# The broker cannot enforce a hard quota from Python without a privileged
# helper.  This script uses the filesystem's project-quota support so a
# container that writes as root is still bounded by the workspace directory.
set -u
set -o pipefail

ROOT=/home/data1/citespec-users
LIMIT_GB=200
SOFT_PERCENT=90
ASSUME_YES=0
DRY_RUN=0

usage() {
    cat <<'EOF'
usage: configure_quotas.sh [options]

Options:
  --workspace-root PATH  Workspace root (default: /home/data1/citespec-users).
  --limit-gb N           Hard quota per member directory (default: 200).
  --soft-percent N       Soft quota percentage (default: 90).
  --yes                  Actually apply quotas (default: dry-run).
  -h, --help             Show this help.

XFS requires the data filesystem to be mounted with prjquota.  ext4 requires
the project and quota features plus the prjquota mount option.  Changing mount
options requires a maintenance window and is deliberately not done here.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --workspace-root)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --workspace-root value\n' >&2; exit 2; }
            ROOT=$1
            ;;
        --limit-gb)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --limit-gb value\n' >&2; exit 2; }
            LIMIT_GB=$1
            ;;
        --soft-percent)
            shift
            [ "$#" -gt 0 ] || { printf 'missing --soft-percent value\n' >&2; exit 2; }
            SOFT_PERCENT=$1
            ;;
        --yes) ASSUME_YES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *)
            printf 'unknown argument: %s\n' "$1" >&2
            usage
            exit 2
            ;;
    esac
    shift
done

[[ "$LIMIT_GB" =~ ^[1-9][0-9]*$ ]] || { printf 'limit-gb must be positive\n' >&2; exit 2; }
[[ "$SOFT_PERCENT" =~ ^[1-9][0-9]?$ ]] || { printf 'soft-percent must be 1..99\n' >&2; exit 2; }
[ "$SOFT_PERCENT" -lt 100 ] || { printf 'soft-percent must be below 100\n' >&2; exit 2; }
[ "$DRY_RUN" -eq 1 ] || [ "$(id -u)" -eq 0 ] || { printf 'run as root or use --dry-run\n' >&2; exit 1; }
[ "$DRY_RUN" -eq 1 ] || [ "$ASSUME_YES" -eq 1 ] || { printf 'refusing to apply quotas without --yes\n' >&2; exit 1; }
[ -d "$ROOT" ] || { printf 'workspace root does not exist: %s\n' "$ROOT" >&2; exit 2; }
have() { command -v "$1" >/dev/null 2>&1; }
have findmnt || { printf 'findmnt not found\n' >&2; exit 2; }

MOUNT=$(findmnt -no TARGET -T "$ROOT") || { printf 'cannot determine mount for %s\n' "$ROOT" >&2; exit 2; }
FSTYPE=$(findmnt -no FSTYPE -T "$ROOT") || { printf 'cannot determine filesystem type\n' >&2; exit 2; }
OPTIONS=$(findmnt -no OPTIONS -T "$ROOT" | tr ',' '\n')
HARD_GB=$LIMIT_GB
SOFT_GB=$((LIMIT_GB * SOFT_PERCENT / 100))
printf '[info] workspace root: %s\n' "$ROOT"
printf '[info] mount: %s (%s), hard=%s GiB soft=%s GiB\n' "$MOUNT" "$FSTYPE" "$HARD_GB" "$SOFT_GB"

if [ "$FSTYPE" = "xfs" ]; then
    if ! printf '%s\n' "$OPTIONS" | grep -Eq '^(prjquota|pquota)$'; then
        printf '[fail] XFS mount %s lacks prjquota/pquota; add it to the mount options during a maintenance window\n' "$MOUNT" >&2
        exit 2
    fi
    have xfs_quota || { printf 'xfs_quota not found\n' >&2; exit 2; }
    for path in "$ROOT"/*; do
        [ -d "$path" ] || continue
        uid=$(stat -c '%u' "$path") || continue
        [ "$uid" -gt 0 ] || continue
        printf '[info] project %s -> %s\n' "$uid" "$path"
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '[dry-run] xfs_quota -x -c "project -s -p %s %s" %s\n' "$path" "$uid" "$MOUNT"
            printf '[dry-run] xfs_quota -x -c "limit -p bsoft=%sg bhard=%sg %s" %s\n' "$SOFT_GB" "$HARD_GB" "$uid" "$MOUNT"
            continue
        fi
        xfs_quota -x -c "project -s -p $path $uid" "$MOUNT" || { printf '[fail] could not assign project %s\n' "$uid" >&2; exit 1; }
        xfs_quota -x -c "limit -p bsoft=${SOFT_GB}g bhard=${HARD_GB}g $uid" "$MOUNT" || { printf '[fail] could not set quota %s\n' "$uid" >&2; exit 1; }
    done
elif [ "$FSTYPE" = "ext4" ] || [ "$FSTYPE" = "ext3" ] || [ "$FSTYPE" = "ext2" ]; then
    if ! printf '%s\n' "$OPTIONS" | grep -Eq '^(prjquota|pquota)$'; then
        printf '[fail] %s mount %s lacks prjquota/pquota; enable project quota during a maintenance window\n' "$FSTYPE" "$MOUNT" >&2
        exit 2
    fi
    have chattr || { printf 'chattr not found\n' >&2; exit 2; }
    have setquota || { printf 'setquota not found\n' >&2; exit 2; }
    for path in "$ROOT"/*; do
        [ -d "$path" ] || continue
        uid=$(stat -c '%u' "$path") || continue
        [ "$uid" -gt 0 ] || continue
        printf '[info] project %s -> %s\n' "$uid" "$path"
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '[dry-run] chattr -R -p %s %s\n' "$uid" "$path"
            printf '[dry-run] find %s -type d -exec chattr +P {} +\n' "$path"
            printf '[dry-run] setquota -P %s %s %s 0 0 %s\n' "$uid" "$((SOFT_GB * 1024 * 1024))" "$((HARD_GB * 1024 * 1024))" "$MOUNT"
            continue
        fi
        chattr -R -p "$uid" "$path" || { printf '[fail] could not assign project %s\n' "$uid" >&2; exit 1; }
        find "$path" -type d -exec chattr +P {} + || { printf '[fail] could not enable project inheritance on %s\n' "$path" >&2; exit 1; }
        setquota -P "$uid" "$((SOFT_GB * 1024 * 1024))" "$((HARD_GB * 1024 * 1024))" 0 0 "$MOUNT" || { printf '[fail] could not set quota %s\n' "$uid" >&2; exit 1; }
    done
else
    printf '[fail] unsupported filesystem %s; use XFS/ext4 project quotas\n' "$FSTYPE" >&2
    exit 2
fi

if [ "$DRY_RUN" -eq 1 ]; then
    printf '[ok] dry-run complete; rerun with --yes during a maintenance window\n'
else
    printf '[ok] project quotas applied; verify with xfs_quota report or repquota -P\n'
fi
