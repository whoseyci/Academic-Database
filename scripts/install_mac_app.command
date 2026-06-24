#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

scripts/build_mac_app.sh
mkdir -p "$HOME/Applications"
rm -rf "$HOME/Applications/Academic Database.app"
cp -R "dist/Academic Database.app" "$HOME/Applications/Academic Database.app"

echo "Installed to: $HOME/Applications/Academic Database.app"
echo "You can launch it from Finder, Spotlight, or Launchpad after opening it once."

if command -v open >/dev/null 2>&1; then
  open "$HOME/Applications"
fi
