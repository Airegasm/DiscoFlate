#!/usr/bin/env bash
# Mirror the desktop sources into the Chaquopy app so the APK can never ship
# stale code. `--check` only verifies (used by scripts/check.sh / CI).
set -euo pipefail
cd "$(dirname "$0")/.."

PY_DST=android-proof/app/src/main/python
ASSETS=android-proof/app/src/main/assets

PY_FILES=(app.py config_store.py device_control.py discord_bot.py engine.py
          kasa_legacy.py minigames.py pumpdirect_import.py version.json)

check_only=false
[ "${1:-}" = "--check" ] && check_only=true

fail=0
copy_or_check() {
  local src="$1" dst="$2"
  if $check_only; then
    if ! cmp -s "$src" "$dst"; then
      echo "   ✗ out of sync: $dst"
      fail=1
    fi
  else
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
  fi
}

for f in "${PY_FILES[@]}"; do
  copy_or_check "$f" "$PY_DST/$f"
done
for f in vendors/*.py; do
  copy_or_check "$f" "$PY_DST/$f"
done
copy_or_check web/index.html "$ASSETS/web/index.html"
copy_or_check default_config.json "$ASSETS/seed/config.json"

if $check_only; then
  [ $fail -eq 0 ] && echo "   ok — android copies match" || exit 1
else
  echo "✓ android-proof sources synced"
fi
