#!/usr/bin/env bash
# Repair a DiscoFlate clone made before the 2026-09 history rewrite.
# Run from the DiscoFlate folder, or pipe it:
#   curl -fsSL https://raw.githubusercontent.com/Airegasm/DiscoFlate/main/scripts/fix-clone.sh | bash
#
# Your data/ folder (token, config, leaderboard, backups) is untracked and is
# NOT touched by this. Local edits to the app's own source files are discarded.
set -euo pipefail

# find the repo root: current dir, or the dir this script lives in
if [ ! -f app.py ] || [ ! -d .git ]; then
  cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." 2>/dev/null || true
fi
if [ ! -f app.py ] || [ ! -d .git ]; then
  echo "!! Run this from your DiscoFlate folder (where app.py lives)."
  exit 1
fi

echo "→ fetching the rewritten history …"
git fetch origin
echo "→ adopting origin/main (data/ and .venv are untouched) …"
git reset --hard origin/main
echo "✓ done — your clone is on the new history. Start DiscoFlate normally."
