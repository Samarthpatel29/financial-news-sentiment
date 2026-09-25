#!/usr/bin/env bash
# Turn off the autostart installed by scripts/install_autostart.sh (macOS).
set -uo pipefail
LABEL="com.sentimentiq.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl unload "$PLIST" 2>/dev/null
rm -f "$PLIST"
pkill -f keep_running.sh 2>/dev/null
echo "Autostart is OFF and the supervisor has been stopped."
