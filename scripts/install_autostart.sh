#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# macOS only: make SentimentIQ start by itself every time you log in, and stay
# up. Uses launchd (Apple's built-in job runner) — nothing to install, no cost.
#
#     bash scripts/install_autostart.sh      turn it on
#     bash scripts/uninstall_autostart.sh    turn it off
#
# Note: the Mac has to be awake for the site to answer. This keeps the LOCAL
# site (localhost:5001) running. The public Vercel site is separate and is
# already online 24/7 on its own.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
LABEL="com.sentimentiq.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x ".venv/bin/python" ]; then
  echo "Run  bash start.sh  once first, so the virtual environment exists."
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$ROOT/scripts/keep_running.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/data/autostart.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/autostart.log</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST" && echo "Autostart is ON. SentimentIQ will run at http://localhost:5001 whenever you are logged in."
echo "Logs: data/app.log   ·   turn it off with: bash scripts/uninstall_autostart.sh"
