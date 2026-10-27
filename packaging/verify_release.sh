#!/usr/bin/env bash
# Verify a first-install package against the signed release manifest.
#
# Usage:
#   ./verify_release.sh --manifest citespec-resource-manifest.json \
#     --package citespec-resource-<version>-linux-x64-full.zip
#
# Requires Python 3.11+ with the cryptography package. Set PYTHON to choose an
# interpreter; verify_release.py is self-contained and does not import the
# installed Citespec package.
set -euo pipefail

here=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

python=${PYTHON:-}
if [ -z "$python" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            python=$candidate
            break
        fi
    done
fi
if [ -z "$python" ]; then
    printf 'verification failed: Python 3.11+ with cryptography is required\n' >&2
    exit 1
fi

exec "$python" "$here/verify_release.py" "$@"
