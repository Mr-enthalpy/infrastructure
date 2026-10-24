#!/bin/sh
# Repeatable development-container bootstrap.
#
# Usage:
#   bootstrap.sh                 refresh the package index
#   bootstrap.sh apt PKG [...]   install Debian packages with sudo
#   bootstrap.sh pip PKG [...]   install Python packages for the current user
#   bootstrap.sh run SCRIPT      run an executable script from /workspace
#
# The container root filesystem is disposable. Everything under /workspace is
# persistent; installed system packages are not.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -- /usr/local/bin/bootstrap.sh "$@"
fi

if [ ! -x /usr/local/bin/bootstrap.sh ]; then
    echo "bootstrap.sh is not installed at /usr/local/bin/bootstrap.sh" >&2
    exit 1
fi

mode=${1:-update}
case "$mode" in
    update)
        apt-get update
        rm -rf /var/lib/apt/lists/*
        ;;
    apt)
        shift
        [ "$#" -gt 0 ] || { echo "bootstrap.sh apt requires packages" >&2; exit 2; }
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
        rm -rf /var/lib/apt/lists/*
        ;;
    pip)
        shift
        [ "$#" -gt 0 ] || { echo "bootstrap.sh pip requires packages" >&2; exit 2; }
        if [ -n "${SUDO_USER:-}" ]; then
            runuser -u "$SUDO_USER" -- python3 -m pip install --user "$@"
        else
            python3 -m pip install --user "$@"
        fi
        ;;
    run)
        shift
        [ "$#" -gt 0 ] || { echo "bootstrap.sh run requires a script" >&2; exit 2; }
        script=$1
        shift
        if [ -n "${SUDO_USER:-}" ]; then
            runuser -u "$SUDO_USER" -- "$script" "$@"
        else
            "$script" "$@"
        fi
        ;;
    *)
        echo "unknown bootstrap mode: $mode" >&2
        exit 2
        ;;
esac
