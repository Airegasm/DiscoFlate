"""An intro holds the PICTURE. With no camera, there is nothing to hold.

Go Live with an intro configured and no virtual camera running used to sit in
a countdown nobody could see: commands blocked, events held, and the
activation message waiting behind a pre-show with no picture. If you didn't
wait it out, nothing reached the channel at all — indistinguishable from the
bot being broken.
"""
import asyncio
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


def rig(intro=True, announce="we start soon", img="card.png"):
    e = eng.Engine()
    e.set_config({
        "listener_enabled": True,
        "chat_scene": "S",
        "scenes": [{"name": "S", "golive": {
            "intro_enabled": intro, "seconds": 15,
            "stages": [{"group": "Intro", "seconds": 15}],
            "announce": announce, "announce_image": img}}],
    })
    return e


said = []


async def announce(text, image=None):
    said.append((text, image))


# ---- no camera: the hold is skipped, the announcement is NOT --------------- #
said.clear()
e = rig()
e.camera_live_cb = lambda: False
started = asyncio.run(e.start_intro(announce_cb=announce))
ok(started is False,
   "with no camera the pre-show does NOT open — the caller goes live at once")
ok(not e.intro_active(), "…and nothing is left holding commands or events")
ok(said and said[0][0] == "we start soon",
   "…but the announcement still goes out, because that was never about the camera")
ok(said and said[0][1] == "card.png", "…carrying its picture")
ok(any("no camera" in x["msg"] for x in e.events),
   "…and the log SAYS why, so a skipped pre-show is not a mystery")

# ---- camera running: the pre-show behaves exactly as before ---------------- #
said.clear()
e = rig()
e.camera_live_cb = lambda: True
started = asyncio.run(e.start_intro(announce_cb=announce))
ok(started is True, "with a camera the pre-show opens as it always did")
ok(e.intro_active(), "…and holds")

# ---- no hook at all: unchanged, so nothing regresses off-desktop ----------- #
said.clear()
e = rig()
ok(e.camera_live_cb is None, "the hook is absent by default")
ok(asyncio.run(e.start_intro(announce_cb=announce)) is True,
   "…and without it the pre-show opens, exactly as before — Android has no "
   "virtual camera and must not lose its intro to this")

# ---- an intro that was never enabled is untouched -------------------------- #
e = rig(intro=False)
e.camera_live_cb = lambda: False
ok(asyncio.run(e.start_intro(announce_cb=announce)) is False,
   "'begin immediately' still begins immediately")

# ---- a failing announcement must not block go-live ------------------------- #
async def boom(text, image=None):
    raise RuntimeError("discord said no")

e = rig()
e.camera_live_cb = lambda: False
ok(asyncio.run(e.start_intro(announce_cb=boom)) is False,
   "an announcement that throws still lets the session start")
ok(any("announcement failed" in x["msg"] for x in e.events), "…and is logged")

print(f"{P} passed, {len(F)} failed")
for f in F:
    print("  FAIL:", f)
sys.exit(1 if F else 0)
