#!/usr/bin/env bash
# DiscoFlate launcher — sets up the venv on first run, then starts the app.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${DISCOFLATE_PORT:-8765}"

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "!! DiscoFlate needs Python 3.9 or newer (found: $(python3 -V 2>&1 || echo 'no python3'))."
  echo "   Install a newer Python 3 and try again."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "→ creating virtualenv (.venv) …"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
elif [ ! -f .venv/.deps-ok ] || [ requirements.txt -nt .venv/.deps-ok ]; then
  echo "→ syncing dependencies …"
  ./.venv/bin/pip install --quiet -r requirements.txt
fi
touch .venv/.deps-ok

echo "→ DiscoFlate starting on http://127.0.0.1:${PORT}  (Ctrl-C to stop)"
exec ./.venv/bin/python app.py
