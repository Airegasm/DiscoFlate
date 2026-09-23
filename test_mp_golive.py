"""GO LIVE in multiplayer is a DIFFERENT go-live, and solo's must not move.

Solo's go-live STARTS a session: it posts the ON message, fires that message's
[!command] tokens, plays the intro and releases timed/capacity events. In a
match none of that may happen on either install — both shows have to begin on
the same instant, and that instant is the handshake, not the switch. So the
multiplayer flip arms and advertises, and stops.

The risk this file exists for is contamination in either direction: a multi
branch that quietly alters solo's flow, or a stale standby flag that outlives
the mode that set it and leaves a SOLO session silently held forever.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as eng

P, F = 0, []


def ok(cond, label):
    global P
    if cond:
        P += 1
    else:
        F.append(label)


def rig(mode="solo", live=True):
    e = eng.Engine()
    e.set_config({"mode": mode, "listener_enabled": live,
                  "devices": [{"id": "d1", "label": "P1", "host": "h",
                               "calibration_seconds_to_100": 60}],
                  "active_device_id": "d1",
                  "capacity_ranges": [{"min": 0, "max": 999}]})
    return e


# ---- the flag cannot exist in solo ------------------------------------------ #
e = rig("solo")
e._mp_standby = True                     # forced, as a stale flag would be
ok(e.mp_standby() is False,
   "a standby flag left over from multiplayer is dead the moment mode is solo")
ok(e._mp_standby is False, "…and is actually cleared, not just reported false")

e = rig("multi")
e.set_mp_standby(True)
ok(e.mp_standby() is True, "in multiplayer it holds")
e.set_config({"mode": "solo", "listener_enabled": True})
ok(e.mp_standby() is False, "…and switching back to solo drops it")

# ---- what standby actually holds -------------------------------------------- #
e = rig("multi")
e.set_mp_standby(True)
r = e._standby_result("Dave", "55")
ok(r.get("ok") is False and "standing by" in (r.get("reply") or ""),
   "a command during standby gets a standby reply, not a pump")

# solo's own pre-show reply is a DIFFERENT message — they must not be confused
e2 = rig("solo")
ok(e2._intro_result("Dave", "55").get("reply") != r.get("reply"),
   "the pre-show reply and the standby reply say different things")

# ---- standby has NO safety timeout ------------------------------------------ #
# _activation_hold releases itself after 8s on purpose. Standby must not: a bot
# that started announcing itself 8 seconds into "starting soon" is the exact
# leak this exists to prevent.
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine.py"),
           encoding="utf-8").read()
i = src.index("def mp_standby")
ok("8.0" not in src[i:i + 900] and "time.monotonic" not in src[i:i + 900],
   "standby never expires on a timer — it ends when the match begins")

# ---- solo's go-live flow is untouched --------------------------------------- #
# The multiplayer fork must be a dispatch ABOVE solo's body, not a branch woven
# through it. If these two ever share lines, a change to one can silently move
# the other.
app_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"),
               encoding="utf-8").read()
j = app_src.index("async def set_listener")
body = app_src[j:app_src.index("async def command_toggle", j)]
ok("_listener_multi(cfg, enabled)" in body, "set_listener forks to its own handler")
ok(body.count("mode") == 1,
   "…exactly once, at the top — solo's body never asks what mode it is in")
ok("set_mp_standby" not in body and "link_standby" not in body,
   "…and solo's body carries no multiplayer call at all")

multi = app_src[app_src.index("async def _listener_multi"):j]
for banned, why in (("_say_on", "does not announce itself"),
                    ("_say_off", "does not post a sign-off"),
                    ("start_intro", "does not play an intro"),
                    ("finish_activation", "does not release events"),
                    ("fire_inline", "does not fire the ON message's commands")):
    ok(banned not in multi, f"the multiplayer flip {why}")
ok("link_standby" in multi and "link_offair" in multi,
   "…it advertises on, and stands down off")

print(f"{P} passed, {len(F)} failed")
for f in F:
    print("  FAIL:", f)
sys.exit(1 if F else 0)
