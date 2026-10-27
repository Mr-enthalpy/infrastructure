#!/usr/bin/env bash
set -euo pipefail

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
