#!/usr/bin/env bash
# Repair a DiscoFlate clone made before the 2026-09 history rewrite.
# Run from the DiscoFlate folder, or pipe it:
#   curl -fsSL https://raw.githubusercontent.com/Airegasm/DiscoFlate/main/scripts/fix-clone.sh | bash
#
# Your data/ folder (token, config, leaderboard, backups) is untracked and is
# NOT touched by this. Local edits to the app's own source files are discarded.
set -euo pipefail

# Find the repo root — works from the folder itself, with the script
# downloaded INTO the DiscoFlate folder, or from its shipped scripts/ home.
here="$(dirname "${BASH_SOURCE[0]:-.}")"
for d in . "$here" "$here/.."; do
  if [ -f "$d/app.py" ] && [ -d "$d/.git" ]; then cd "$d"; break; fi
done
if [ ! -f app.py ] || [ ! -d .git ]; then
  echo "!! Put this script in your DiscoFlate folder (next to app.py) and run it again."
  exit 1
fi

echo "→ fetching the rewritten history …"
git fetch origin
echo "→ adopting origin/main (data/ and .venv are untouched) …"
git reset --hard origin/main
echo "✓ done — your clone is on the new history. Start DiscoFlate normally."
