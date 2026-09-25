#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SentimentIQ launcher (macOS / Linux)
#
#     bash start.sh
#
# FIRST RUN:  creates the virtual environment and installs everything (~5 min).
# EVERY RUN AFTER THAT: skips all of that and just starts the site.
#
# The "already installed" marker is .venv/.installed — delete it (or the whole
# .venv folder) if you ever want to force a clean reinstall.
#
# Windows: use start.bat instead.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")"                       # always run from the project folder

PORT="${DASHBOARD_PORT:-5001}"             # 5001 by default (macOS uses 5000 for AirPlay)
export DASHBOARD_PORT="$PORT"
PY=".venv/bin/python"
MARKER=".venv/.installed"

# ── 1. First run only: build the virtual environment ─────────────────────────
if [ ! -x "$PY" ]; then
  echo "First-time setup. This happens once and takes about 5 minutes."
  # Find a Python the project supports (3.13 has no build for one dependency).
  BASE=""
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      V="$("$candidate" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
      case "$V" in 3.11|3.12) BASE="$candidate"; break ;; esac
    fi
  done
  if [ -z "$BASE" ]; then
    echo "ERROR: need Python 3.11 or 3.12 (3.13 is not supported yet)."
    echo "       Install it from https://www.python.org/downloads/ and run this again."
    exit 1
  fi
  echo "Using $BASE to create .venv ..."
  "$BASE" -m venv .venv || { echo "ERROR: could not create the virtual environment."; exit 1; }
fi

# ── 2. First run only: install the dependencies ──────────────────────────────
# If the venv already works (e.g. you set it up by hand before), just record
# that and skip the install.
if [ ! -f "$MARKER" ] && "$PY" -c "import flask, torch, sqlalchemy" >/dev/null 2>&1; then
  date > "$MARKER"
fi

if [ ! -f "$MARKER" ]; then
  echo "Installing dependencies (one time) ..."
  "$PY" -m pip install --quiet --upgrade pip
  if "$PY" -m pip install -r requirements.txt; then
    date > "$MARKER"                       # remember that this succeeded
    echo "Setup complete."
  else
    echo "ERROR: install failed. Fix the error above and run this again."
    exit 1
  fi
fi

# ── 3. Optional config file, so the app never starts without one ─────────────
[ -f .env ] || { [ -f .env.example ] && cp .env.example .env && \
  echo "Created .env from .env.example (API keys are optional)."; }

# ── 4. Free the port if an old copy is still holding it ──────────────────────
if command -v lsof >/dev/null 2>&1; then
  OLD="$(lsof -ti:"$PORT" 2>/dev/null)"
  if [ -n "$OLD" ]; then
    echo "Stopping the old server on port $PORT ..."
    kill $OLD 2>/dev/null
    sleep 1
  fi
fi

# ── 5. Start it ──────────────────────────────────────────────────────────────
echo "Starting SentimentIQ on http://localhost:$PORT ..."
"$PY" run.py &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' INT TERM

sleep 6
if command -v open >/dev/null 2>&1; then          # macOS
  open "http://localhost:$PORT"
elif command -v xdg-open >/dev/null 2>&1; then    # Linux
  xdg-open "http://localhost:$PORT" >/dev/null 2>&1
fi

echo "Dashboard: http://localhost:$PORT   (first data appears after 1-2 minutes)"
echo "Press Ctrl+C to stop."
wait $SERVER
