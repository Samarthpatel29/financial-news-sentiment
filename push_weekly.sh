#!/bin/bash
# Weekly GitHub push — runs every Saturday
cd "$(dirname "$0")"

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Stage any changed/new files
git add -A

# Only commit if there are changes
if ! git diff --cached --quiet; then
  WEEK=$(date +"%Y-W%V")
  DATE=$(date +"%B %d, %Y")
  git commit -m "Weekly update — ${DATE}

Auto-commit: latest pipeline code, dashboard improvements, and activity logs.
Week: ${WEEK}"
  git push origin main
  echo "✅ Pushed to GitHub — ${DATE}"
else
  echo "ℹ️  No changes to commit — $(date)"
fi
