"""Multiplayer is not finished, so a public build must be able to ship without it.

The gate has to hold at three levels, because any one of them alone leaves a
way in: the panel can hide the toggle, but a saved config or a stale tab still
carries `mode: multi`; the API can refuse the switch, but a config already in
multi would boot straight into a mode whose exit is hidden.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

P, F = 0, []


def ok(cond, label):
    global P
    if cond:
        P += 1
    else:
        F.append(label)


def with_flag(value):
    """Reload config_store/app with the env flag set."""
    if value is None:
        os.environ.pop("DISCOFLATE_MULTIPLAYER", None)
    else:
        os.environ["DISCOFLATE_MULTIPLAYER"] = value
    import config_store
    importlib.reload(config_store)
    return config_store


# ---- the default: this working copy keeps multiplayer ---------------------- #
import app as _app  # noqa: E402
ok(_app.MULTIPLAYER_ENABLED is True,
   "the working copy ships with multiplayer ON — the flag is opt-OUT")

# ---- off: a config already in multi is pulled back -------------------------- #
cs = with_flag("0")
os.environ["DISCOFLATE_DATA_DIR"] = __import__("tempfile").mkdtemp(prefix="df-gate-")
cs = with_flag("0")
assert "df-gate-" in cs.CONFIG_PATH, "refusing to run against a real config"

cs.save({**cs.DEFAULTS, "mode": "multi", "discord_token": ""})
back = cs.load()
ok(back.get("mode") == "solo",
   "a build with multiplayer off comes up in SOLO even when the stored config "
   "says multi — the toggle that would get you out is hidden")

for word in ("0", "off", "false"):
    cs = with_flag(word)
    cs.save({**cs.DEFAULTS, "mode": "multi", "discord_token": ""})
    ok(cs.load().get("mode") == "solo", f"…{word!r} turns it off too")

cs = with_flag("1")
cs.save({**cs.DEFAULTS, "mode": "multi", "discord_token": ""})
ok(cs.load().get("mode") == "multi", "…and it is left alone when enabled")
cs = with_flag(None)
cs.save({**cs.DEFAULTS, "mode": "multi", "discord_token": ""})
ok(cs.load().get("mode") == "multi", "…as it is when the flag isn't set at all")

# ---- the panel hides the surface ------------------------------------------- #
html = open("web/index.html", encoding="utf-8").read()
# search FORWARD from the function — these anchors also appear earlier
_a = html.index("function mpAvailable()")
vis = html[_a:html.index("\nfunction ", html.index("function applyModeVis()"))]
ok("multiplayer_enabled === false" in vis,
   "the panel reads the flag off the server rather than guessing")
ok("older server that predates the flag" in vis,
   "…and an absent flag means available, so an older server still works")
ok('#modeTgl' in vis, "the header toggle is hidden when it's off")
# There is no Multiplayer tab any more — the HEADER runs the match, so the
# header is what has to go with it.
for ctl in ("#seatTgl", "#mpInviteBtn", "#mpStartBtn", "#mpAbortBtn"):
    ok(ctl in vis, f"…and so is {ctl}, so no dead control is left in the header")
ok("data-mode" in vis,
   "…and every multi-scoped card, which is what carries the setup now")

# ---- no switch that skips the accept prompt --------------------------------- #
# `auto_accept` used to sit in the config, read by nothing. A dead switch is
# worse than no switch: it implies a behaviour that does not exist, and the
# first person to tick it learns that only when an invite they never saw has
# already started firing their pump.
ok("auto_accept" not in cs.DEFAULTS["multiplayer"],
   "there is no auto-accept: an invite carries a cost estimate precisely so "
   "somebody reads it before agreeing")
import json as _j
_raw = _j.load(open("default_config.json", encoding="utf-8"))
ok("auto_accept" not in (_raw.get("multiplayer") or {}),
   "…and the shipped config does not carry one either")

# ---- the API refuses the switch --------------------------------------------- #
src = open("app.py", encoding="utf-8").read()
i = src.index('if "mode" in body:')
gate = src[i:i + 500]
ok("not MULTIPLAYER_ENABLED" in gate and "HTTPConflict" in gate,
   "the API refuses mode:multi in a build without it — hiding a control is not "
   "the same as disabling it")
ok(src.index("MULTIPLAYER_ENABLED = True") < i,
   "…and the flag is declared before anything reads it")

print(f"{P} passed, {len(F)} failed")
for f in F:
    print("  FAIL:", f)
sys.exit(1 if F else 0)
