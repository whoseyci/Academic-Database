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

# Wait briefly for server startup.
sleep 1

if [[ "${OPEN_BROWSER:-0}" == "1" ]]; then
  if command -v open >/dev/null 2>&1; then
    open "http://$HOST:$PORT" || true
  else
    echo "Open http://$HOST:$PORT"
  fi
fi
