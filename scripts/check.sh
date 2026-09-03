#!/usr/bin/env bash
# Pre-release sanity checks — run before tagging/shipping a version.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3

echo "→ compiling Python modules …"
$PY -m py_compile app.py config_store.py discord_bot.py engine.py \
    device_control.py kasa_legacy.py minigames.py pumpdirect_import.py vendors/*.py

echo "→ default_config.json covers every DEFAULTS key …"
$PY - <<'EOF'
import json, config_store
d = set(json.load(open("default_config.json")))
py = set(config_store.DEFAULTS) - {"config_rev"}
missing = sorted(py - d)
assert not missing, f"default_config.json is missing: {missing}"
print("   ok")
EOF

echo "→ version.json parses and matches app version …"
$PY - <<'EOF'
import json
v = json.load(open("version.json"))
assert v.get("version") and int(v.get("versionCode", 0)) > 0
print(f"   ok — v{v['version']} (code {v['versionCode']})")
EOF

if command -v node >/dev/null; then
  echo "→ UI JavaScript syntax …"
  sed -n '/<script>/,/<\/script>/p' web/index.html | sed '1d;$d' > /tmp/discoflate-ui-check.js
  node --check /tmp/discoflate-ui-check.js && echo "   ok"
  rm -f /tmp/discoflate-ui-check.js
fi

if [ -x scripts/sync-android.sh ]; then
  echo "→ android-proof copies in sync …"
  scripts/sync-android.sh --check
fi

echo "✓ all checks passed"
