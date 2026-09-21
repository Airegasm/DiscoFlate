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

pip_sync() {
  # pip hygiene + install: clear '~xyz' corpse folders left by interrupted
  # installs (the "Ignoring invalid distribution" warnings), install, and on
  # failure purge pip's download cache and retry once. A cache-corruption
  # warning on a successful install also triggers a purge so later runs are quiet.
  find .venv/lib/python*/site-packages -maxdepth 1 -type d -name '~*' \
    -exec rm -rf {} + 2>/dev/null || true
  local log; log="$(mktemp)"
  if ! ./.venv/bin/pip install --quiet -r requirements.txt >"$log" 2>&1; then
    echo "   pip hit trouble — cleaning its cache and retrying once …"
    ./.venv/bin/pip cache purge >/dev/null 2>&1 || true
    if ! ./.venv/bin/pip install --quiet -r requirements.txt >"$log" 2>&1; then
      cat "$log"; rm -f "$log"
      echo "!! pip install failed — the log above shows why (try deleting .venv and rerunning)."
      return 1
    fi
  fi
  if grep -q "Cache entry deserialization failed" "$log"; then
    echo "   cleaning a corrupted pip cache …"
    ./.venv/bin/pip cache purge >/dev/null 2>&1 || true
  fi
  rm -f "$log"
}

if [ ! -d .venv ]; then
  echo "→ creating virtualenv (.venv) …"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  pip_sync
elif [ ! -f .venv/.deps-ok ] || [ requirements.txt -nt .venv/.deps-ok ]; then
  echo "→ syncing dependencies …"
  pip_sync
fi
touch .venv/.deps-ok

echo "→ DiscoFlate starting on http://127.0.0.1:${PORT}  (Ctrl-C to stop)"
exec ./.venv/bin/python app.py
