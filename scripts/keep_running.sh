#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Keep SentimentIQ up 24/7.
#
#     bash scripts/keep_running.sh
#
# Starts the app and watches it. If it ever crashes or exits, this restarts it
# a few seconds later, forever. Everything it prints goes to data/app.log.
#
# To have it start by itself every time you log in:
#     bash scripts/install_autostart.sh        (macOS)
#
# Stop it with Ctrl+C, or:  pkill -f keep_running.sh
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PORT="${DASHBOARD_PORT:-5001}"
export DASHBOARD_PORT="$PORT"
PY=".venv/bin/python"
LOG="data/app.log"
mkdir -p data

if [ ! -x "$PY" ]; then
  echo "No virtual environment yet. Run  bash start.sh  once first." | tee -a "$LOG"
  exit 1
fi

say() { echo "$(date '+%Y-%m-%d %H:%M:%S')  keep_running: $*" | tee -a "$LOG"; }

CHILD=""
cleanup() { say "stopping"; [ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null; exit 0; }
trap cleanup INT TERM

say "supervisor started on port $PORT (log: $LOG)"
FAILS=0
while true; do
  START=$(date +%s)
  "$PY" run.py >> "$LOG" 2>&1 &
  CHILD=$!
  say "app started (pid $CHILD)"
  wait "$CHILD"
  CODE=$?
  RAN=$(( $(date +%s) - START ))
  say "app exited with code $CODE after ${RAN}s"

  # If it died almost immediately it is probably a real error, not a blip —
  # back off so we don't spin in a tight restart loop.
  if [ "$RAN" -lt 30 ]; then
    FAILS=$((FAILS + 1))
    WAIT=$(( FAILS * 15 )); [ "$WAIT" -gt 300 ] && WAIT=300
    say "quick exit #$FAILS — waiting ${WAIT}s before retrying (check the log above)"
    sleep "$WAIT"
  else
    FAILS=0
    sleep 5
  fi
done
