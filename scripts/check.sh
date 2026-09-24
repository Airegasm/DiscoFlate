#!/usr/bin/env bash
# Pre-release sanity checks — run before tagging/shipping a version.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3

echo "→ compiling Python modules …"
$PY -m py_compile app.py camera.py config_store.py discord_bot.py engine.py \
    device_control.py kasa_legacy.py minigames.py mp_games.py multiplayer.py \
    pumpdirect_import.py stage.py vendors/*.py

echo "→ default_config.json covers every DEFAULTS key …"
$PY - <<'EOF'
import json, config_store
d = set(json.load(open("default_config.json")))
py = set(config_store.DEFAULTS) - {"config_rev"}
missing = sorted(py - d)
assert not missing, f"default_config.json is missing: {missing}"
print("   ok")
EOF

echo "→ multiplayer rail (protocol + transport) + game modes …"
$PY test_multiplayer.py
$PY test_fire_stacking.py
$PY test_mp_actions.py
$PY test_multiplayer_bot.py
$PY test_mp_scene.py
$PY test_mp_gate.py
$PY test_mp_results.py
$PY test_mp_golive.py
$PY test_standby.py
$PY test_intro_nocam.py

# Every new [placeholder] has to reach the Help table, or it exists only for
# whoever wrote it. Multiplayer publishes a lot of them, so this is checked
# rather than remembered.
echo "→ every [multi_*] placeholder is documented in Help …"
$PY - <<'EOF'
import re, sys
html = open("web/index.html", encoding="utf-8").read()
# row/scene FIELDS, not placeholders — they never appear in a message
NOT_PLACEHOLDERS = {"multi_who", "multi_game_mode"}
published = set()
for f in ("mp_games.py", "discord_bot.py"):
    published |= set(re.findall(r'"(multi_[a-z_]+)"', open(f, encoding="utf-8").read()))
published -= NOT_PLACEHOLDERS
missing = sorted(published - set(re.findall(r"[\[](multi_[a-z_]+)[\]]", html)))
assert not missing, f"undocumented placeholders: {missing}"
print(f"   ok — {len(published)} documented")
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
  # node --check is valid-syntax only: it accepts markup spliced into the wrong
  # function and every undefined runtime name (that is how adding an overlay
  # stayed broken 3.51.0 -> 3.55.1). This EXERCISES the panel against stubs.
  # node --check cannot see a TDZ read or any other load-time death, and one
  # of those blanks the whole panel. This actually runs the file.
  echo "→ the panel script loads at all …"
  node test_panel_boot.js
  echo "→ multiplayer panel behaviour …"
node test_golive_scope.js
node test_save_race.js
  node test_multiplayer_ui.js
fi

# The APK ships a hand-listed set of modules. A module the app imports but
# nobody added to that list is invisible here (the file is in the repo) and
# fatal on Android: the import throws, the server thread dies before it binds,
# and the app hangs on "booting local server" until it times out. media_len.py
# went missing this way in v3.79.0 and shipped broken for five releases.
echo "→ every imported local module is in the APK …"
python3 - <<'PYEOF'
import ast, os, re, sys
src = open("scripts/sync-android.sh", encoding="utf-8").read()
listed = set(re.search(r"PY_FILES=\(([^)]*)\)", src, re.S).group(1).split())
shipped = {f[:-3] for f in listed if f.endswith(".py")}
local = {f[:-3] for f in os.listdir(".") if f.endswith(".py")}
missing = {}
for mod in sorted(shipped):
    if not os.path.exists(f"{mod}.py"):
        continue
    for n in ast.walk(ast.parse(open(f"{mod}.py", encoding="utf-8").read())):
        names = []
        if isinstance(n, ast.Import):
            names = [a.name.split(".")[0] for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            names = [n.module.split(".")[0]]
        for nm in names:
            if nm in local and nm not in shipped:
                missing.setdefault(nm, set()).add(mod)
if missing:
    for nm, who in sorted(missing.items()):
        print(f"   \u2717 {nm}.py is imported by {', '.join(sorted(who))} "
              f"but is NOT in sync-android.sh PY_FILES")
    sys.exit(1)
print("   ok")
PYEOF

if [ -x scripts/sync-android.sh ]; then
  echo "→ android-proof copies in sync …"
  scripts/sync-android.sh --check
fi

echo "✓ all checks passed"
