#!/bin/bash
# ── Financial News Sentiment Dashboard ────────────────────────────────────────
# Double-click this file (or run: bash start.sh) to start everything.

cd "$(dirname "$0")"   # always run from the project folder

echo "⏹  Stopping any old server..."
kill $(lsof -ti:5001) 2>/dev/null
sleep 1

echo "🚀 Starting pipeline + dashboard..."
.venv/bin/python3.11 run.py &

echo "⏳ Waiting for server to boot..."
sleep 5

echo "🌐 Opening dashboard..."
open http://localhost:5001

echo "✅ Done! Dashboard is at http://localhost:5001"
echo "   Press Ctrl+C here to stop the server."
wait
