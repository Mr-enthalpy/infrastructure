#!/usr/bin/env bash
# Install the Citespec Resource client from an extracted full zip.
#
# Before running, verify the zip and this script with verify_release.sh (or the
# published SHA-256). Use --verify <sha256> with the installer_sha256 value
# from the signed manifest's first_install entry to authenticate this script.
set -euo pipefail

usage() {
  cat <<'EOF'
usage: install.sh [--verify SHA256]

  --verify SHA256   Refuse to install unless this script's SHA-256 matches the
                    value signed in the release manifest
                    (first_install.<platform>.installer_sha256).
  -h, --help        Show this help.

Environment:
  CITESPEC_CLIENT_HOME  Install directory (default: ~/.local/opt/citespec-resource)
  CITESPEC_CLIENT_BIN   Launcher directory (default: ~/.local/bin)
EOF
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -- "$1" | awk '{print $1}'
  else
    return 1
  fi
}

verify_expected=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --verify)
      shift
      if [ "$#" -eq 0 ]; then
        printf 'error: --verify requires a SHA-256 value\n' >&2
        exit 2
      fi
      verify_expected=$1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'error: unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [ -n "$verify_expected" ]; then
  expected=$(printf '%s' "$verify_expected" | tr 'A-F' 'a-f')
  case "$expected" in
    *[!0-9a-f]* | "")
      printf 'error: --verify must be a 64-character SHA-256\n' >&2
      exit 2
      ;;
  esac
  if [ "${#expected}" -ne 64 ]; then
    printf 'error: --verify must be a 64-character SHA-256\n' >&2
    exit 2
  fi
  actual=$(sha256_file "${BASH_SOURCE[0]}") || {
    printf 'error: sha256sum or shasum is required for --verify\n' >&2
    exit 1
  }
  if [ "$actual" != "$expected" ]; then
    printf 'error: install.sh SHA-256 mismatch\n  expected: %s\n  actual:   %s\n' \
      "$expected" "$actual" >&2
    exit 1
  fi
  printf 'Verified install.sh SHA-256: %s\n' "$actual"
fi

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install_dir="${CITESPEC_CLIENT_HOME:-$HOME/.local/opt/citespec-resource}"
bin_dir="${CITESPEC_CLIENT_BIN:-$HOME/.local/bin}"

mkdir -p "$install_dir" "$bin_dir"
cp -a "$source_dir"/. "$install_dir"/
if [ ! -f "$install_dir/citespec-resource" ]; then
  printf 'launcher not found after install: %s\n' "$install_dir/citespec-resource" >&2
  exit 1
fi
chmod +x "$install_dir/citespec-resource"
find "$install_dir/versions" -type f -name 'citespec-resource-app' -exec chmod +x {} + 2>/dev/null || true
ln -sfn "$install_dir/citespec-resource" "$bin_dir/citespec-resource"

printf 'Installed Citespec Resource to %s\n' "$install_dir"
printf 'Launcher: %s\n' "$bin_dir/citespec-resource"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) printf 'Add %s to PATH, then open a new shell.\n' "$bin_dir" ;;
esac
printf 'Run: citespec-resource setup --server <address> --user <linux-user>\n'
