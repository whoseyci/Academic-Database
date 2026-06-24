#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="Academic Database"
APP_DIR="$ROOT/dist/${APP_NAME}.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
LAUNCHER="$MACOS/academic-database-launcher"

mkdir -p "$MACOS" "$RESOURCES"

cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>${APP_NAME}</string>
  <key>CFBundleDisplayName</key><string>${APP_NAME}</string>
  <key>CFBundleIdentifier</key><string>ai.arena.academic-database</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleExecutable</key><string>academic-database-launcher</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
PLIST

cat > "$LAUNCHER" <<LAUNCHER
#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$ROOT"
cd "\$REPO_DIR"

mkdir -p reports .run
LOG="reports/mac_app_launcher.log"
exec >>"\$LOG" 2>&1

echo "===== \$(date) Academic Database launcher ====="

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

BRANCH="\${RH_BRANCH:-main}"
HOST="\${RH_REVIEW_HOST:-127.0.0.1}"
PORT="\${RH_REVIEW_PORT:-8765}"
URL="http://\$HOST:\$PORT"

notify() {
  /usr/bin/osascript -e "display notification \"\$1\" with title \"Academic Database\"" >/dev/null 2>&1 || true
}

notify "Starting Academic Database…"

if command -v git >/dev/null 2>&1; then
  CURRENT_BRANCH="\$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [[ "\$CURRENT_BRANCH" != "\$BRANCH" ]]; then
    git checkout "\$BRANCH" || true
  fi
  if [[ -z "\$(git status --porcelain 2>/dev/null)" ]]; then
    git fetch origin "\$BRANCH" || true
    git pull --ff-only origin "\$BRANCH" || true
  else
    echo "Working tree has local changes; skipping auto-pull."
    git status --short || true
  fi
fi

if [[ ! -d .venv ]]; then
  echo "Creating venv…"
  python3 -m venv .venv
fi

PY="\$REPO_DIR/.venv/bin/python"
"\$PY" -m pip install --upgrade pip wheel setuptools >/dev/null 2>&1 || true

# Install parser deps once, or whenever requirements-parser.txt is newer than stamp.
STAMP=".venv/.academic_db_setup_stamp"
if [[ "\${PDF2MD_INSTALL_PARSER:-1}" == "1" && -f requirements-parser.txt ]]; then
  if [[ ! -f "\$STAMP" || requirements-parser.txt -nt "\$STAMP" ]]; then
    echo "Installing/updating parser dependencies…"
    "\$PY" -m pip install -r requirements-parser.txt || true
    date > "\$STAMP"
  fi
fi
if [[ "\${PDF2MD_INSTALL_VISION:-0}" == "1" && -f requirements-vision.txt ]]; then
  if [[ ! -f ".venv/.academic_db_vision_stamp" || requirements-vision.txt -nt ".venv/.academic_db_vision_stamp" ]]; then
    echo "Installing/updating vision dependencies…"
    "\$PY" -m pip install -r requirements-vision.txt || true
    date > ".venv/.academic_db_vision_stamp"
  fi
fi

# Start/restart local review UI.
RH_REVIEW_HOST="\$HOST" RH_REVIEW_PORT="\$PORT" OPEN_BROWSER=0 scripts/start_review_ui.sh || true

sleep 1
open "\$URL" || true
notify "Review UI ready at \$URL"
LAUNCHER

chmod +x "$LAUNCHER"

echo "Built: $APP_DIR"
echo "You can copy it to ~/Applications or /Applications."
