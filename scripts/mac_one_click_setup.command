#!/usr/bin/env bash
set -euo pipefail

# Double-click this file in Finder, or run:
#   ./scripts/mac_one_click_setup.command

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=============================================="
echo " Academic-Database one-click Mac dev setup"
echo "=============================================="
echo "Repo: $ROOT"

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git is required. Install Xcode Command Line Tools: xcode-select --install"
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required. Install Python 3 or use Homebrew: brew install python"
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

BRANCH="${RH_BRANCH:-agent-harness-evidence-audit}"

echo "Checking out branch: $BRANCH"
git fetch origin "$BRANCH" || true
git checkout "$BRANCH" || true
git pull --ff-only origin "$BRANCH" || true

echo "Creating/updating virtual environment..."
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools

# Core harness is stdlib-only. Parser deps are optional but useful.
if [[ "${PDF2MD_INSTALL_PARSER:-1}" == "1" && -f requirements-parser.txt ]]; then
  echo "Installing parser dependencies from requirements-parser.txt ..."
  if ! pip install -r requirements-parser.txt; then
    echo "WARNING: parser dependency install failed. Core review UI still works."
  fi
fi
if [[ "${PDF2MD_INSTALL_VISION:-0}" == "1" && -f requirements-vision.txt ]]; then
  echo "Installing vision dependencies from requirements-vision.txt ..."
  if ! pip install -r requirements-vision.txt; then
    echo "WARNING: vision dependency install failed."
  fi
fi

chmod +x scripts/*.sh scripts/*.command
mkdir -p reports .run data/pdfs data/converted

python -m py_compile rh2.py export_static_site.py || true

echo ""
echo "Setup complete. Starting auto-pull watcher + review UI."
echo "Review UI will open at: http://${RH_REVIEW_HOST:-127.0.0.1}:${RH_REVIEW_PORT:-8765}"
echo "Leave this terminal window open to keep auto-updates running."
echo ""

export OPEN_BROWSER=1
exec scripts/dev_watch.sh
