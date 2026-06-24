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

HOST="${RH_REVIEW_HOST:-127.0.0.1}"
PORT="${RH_REVIEW_PORT:-8765}"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

mkdir -p reports .run
PIDFILE=".run/review_ui.pid"
LOGFILE="reports/review_ui_server.log"

if [[ -f "$PIDFILE" ]]; then
  OLD_PID="$(cat "$PIDFILE" || true)"
  if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Stopping existing review UI pid $OLD_PID"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
fi

echo "Starting review UI on http://$HOST:$PORT"
nohup "$PY" rh2.py review-ui --host "$HOST" --port "$PORT" > "$LOGFILE" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
echo "review-ui pid=$PID"
echo "logs: $LOGFILE"

# Wait briefly for server startup and fail loudly if it crashed.
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
  echo "ERROR: review UI failed to stay running. Last log lines:" >&2
  tail -80 "$LOGFILE" >&2 || true
  exit 1
fi

open_review_url() {
  local url="$1"
  if [[ "${RH_APP_WINDOW:-1}" == "1" ]] && command -v open >/dev/null 2>&1; then
    if [[ -d "/Applications/Google Chrome.app" ]]; then
      open -na "Google Chrome" --args --app="$url" >/dev/null 2>&1 && return 0
    fi
    if [[ -d "/Applications/Microsoft Edge.app" ]]; then
      open -na "Microsoft Edge" --args --app="$url" >/dev/null 2>&1 && return 0
    fi
  fi
  if command -v open >/dev/null 2>&1; then
    open "$url" || true
  else
    echo "Open $url"
  fi
}

if [[ "${OPEN_BROWSER:-0}" == "1" ]]; then
  open_review_url "http://$HOST:$PORT"
fi
