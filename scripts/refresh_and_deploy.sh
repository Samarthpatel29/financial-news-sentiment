#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Local scheduled refresh — the "perfect" data path.
#
# Runs the full pipeline FROM THIS MAC (home IP, so yfinance + Finviz actually
# work → full-quality price & analyst signals), regenerates the static snapshot,
# and pushes it so Vercel auto-deploys. Driven by the launchd job
# com.sentimentiq.refresh so it happens on its own — no start.sh, no babysitting.
#
# Logs to data/refresh.log. Safe to run by hand any time: bash scripts/refresh_and_deploy.sh
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"

LOG="$ROOT/data/refresh.log"
mkdir -p "$ROOT/data"
exec >> "$LOG" 2>&1

# Never let two refreshes overlap (a slow run + the next tick).
LOCK="$ROOT/data/.refresh.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T')  another refresh is already running — skipping"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

echo "═════════════ refresh $(date '+%F %T %Z') ═════════════"

# Load GROQ_API_KEY (and any other keys) from .env so the LLM judge is active.
if [ -f .env ]; then set -a; . ./.env; set +a; fi

# 1) pipeline + export
if ! ./.venv/bin/python scripts/refresh_once.py; then
  echo "$(date '+%F %T')  pipeline/export FAILED — not deploying"
  exit 1
fi

# 2) deploy: commit the regenerated snapshot and push (Vercel redeploys on push)
git add public/
if git diff --cached --quiet; then
  echo "$(date '+%F %T')  snapshot unchanged — nothing to deploy"
else
  git commit -q -m "Local auto-refresh $(date -u '+%Y-%m-%d %H:%M UTC')"
  git pull --rebase --autostash origin main >/dev/null 2>&1 || true
  if git push origin main; then
    echo "$(date '+%F %T')  pushed — Vercel will redeploy"
  else
    echo "$(date '+%F %T')  push FAILED (check network / credentials)"
  fi
fi

echo "$(date '+%F %T')  done"
