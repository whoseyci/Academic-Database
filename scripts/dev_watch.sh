#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

BRANCH="${RH_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
INTERVAL="${RH_PULL_INTERVAL:-30}"
REMOTE="origin"

mkdir -p reports .run

echo "Academic-Database dev watcher"
echo "repo: $ROOT"
echo "branch: $BRANCH"
echo "poll interval: ${INTERVAL}s"

git checkout "$BRANCH" >/dev/null 2>&1 || true

OPEN_BROWSER="${OPEN_BROWSER:-1}" scripts/start_review_ui.sh
OPEN_BROWSER=0

while true; do
  sleep "$INTERVAL"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] checking $REMOTE/$BRANCH ..."
  if ! git fetch "$REMOTE" "$BRANCH" >/tmp/academic_db_fetch.log 2>&1; then
    echo "fetch failed; will retry. $(cat /tmp/academic_db_fetch.log)"
    continue
  fi
  LOCAL="$(git rev-parse HEAD)"
  REMOTE_HEAD="$(git rev-parse "$REMOTE/$BRANCH" 2>/dev/null || true)"
  if [[ -z "$REMOTE_HEAD" || "$LOCAL" == "$REMOTE_HEAD" ]]; then
    continue
  fi
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "Remote update found, but local working tree has changes. Skipping pull."
    git status --short
    continue
  fi
  echo "New commit found: $LOCAL -> $REMOTE_HEAD"
  if git pull --ff-only "$REMOTE" "$BRANCH"; then
    echo "Pulled update. Restarting review UI."
    OPEN_BROWSER=0 scripts/start_review_ui.sh
  else
    echo "Pull failed; resolve manually."
  fi
done
