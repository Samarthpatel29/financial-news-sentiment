#!/usr/bin/env bash
# ── SentimentIQ launcher (macOS / Linux) ──────────────────────────────────────
# Starts the data pipeline + web dashboard and opens it in your browser.
#   Usage:  bash start.sh
# Windows: this script is not needed — run  .venv\Scripts\python run.py  (see README).

cd "$(dirname "$0")"                       # always run from the project folder

PORT="${DASHBOARD_PORT:-5001}"            # 5001 by default (macOS uses 5000 for AirPlay)
export DASHBOARD_PORT="$PORT"

# Use the project's own virtualenv Python, whatever version created it.
PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "No virtualenv found. Set it up first (see README):"
  echo "    python3.11 -m venv .venv"
  echo "    .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# Stop an old copy that is still holding the port (lsof exists on macOS and most Linux).
if command -v lsof >/dev/null 2>&1; then
  OLD="$(lsof -ti:"$PORT" 2>/dev/null)"
  if [ -n "$OLD" ]; then
    echo "Stopping the old server on port $PORT ..."
    kill $OLD 2>/dev/null
    sleep 1
  fi
fi

echo "Starting pipeline + dashboard on http://localhost:$PORT ..."
"$PY" run.py &
SERVER=$!

sleep 6
# Open the browser: 'open' on macOS, 'xdg-open' on Linux.
if command -v open >/dev/null 2>&1; then
  open "http://localhost:$PORT"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://localhost:$PORT" >/dev/null 2>&1
fi

echo "Dashboard: http://localhost:$PORT   (the first stock data appears after 1-2 minutes)"
echo "Press Ctrl+C to stop."
trap 'kill $SERVER 2>/dev/null' INT TERM
wait $SERVER
