"""The standby card: an image, or the line, never a blank frame.

Anyone who picks your virtual camera before you go LIVE sees this. A black
rectangle reads as a broken camera, which is why the text exists at all — so
an image that fails to load must fall back to the TEXT, not to nothing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import camera

P, F = 0, []


def ok(cond, label):
    global P
    if cond:
        P += 1
    else:
        F.append(label)


def cam():
    c = camera.VirtualCam.__new__(camera.VirtualCam)
    c._standby_text, c._standby_image, c._standby_cache = "STARTING SOON", "", None
    return c


HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.path.join(HERE, "data", "images", "versus-intro.png")

c = cam()
ok(c._standby_card() is None, "no image set → no card, so the line shows")

c.set_standby("STARTING SOON", "does-not-exist.png")
ok(c._standby_card() is None,
   "a MISSING file falls back to the text — never to a blank frame, which is "
   "the one thing that reads as a broken camera")

if os.path.exists(REAL):
    c.set_standby("STARTING SOON", REAL)
    got = c._standby_card()
    ok(got is not None, "a real image loads")
    ok(got is not None and got.shape[2] == 4,
       "…as BGRA, so transparency composites instead of showing as black")
    # decoded ONCE: this runs on every frame of the pre-show
    before = c._standby_cache
    c._standby_card()
    ok(c._standby_cache is before, "…and is cached, not re-decoded every frame")

# an unreadable file is remembered as unreadable, so it is not retried 30x/sec
bad = os.path.join(HERE, "test_standby.py")          # not an image
c2 = cam()
c2.set_standby("X", bad)
ok(c2._standby_card() is None, "a file that is not an image is no card")
ok(c2._standby_cache is not None and c2._standby_cache[2] is None,
   "…and the failure is CACHED, so a bad path isn't re-read on every frame")

# changing the picture invalidates the cache
c3 = cam()
c3.set_standby("X", "a.png")
c3._standby_cache = ("a.png", 1, "STALE")
c3.set_standby("X", "b.png")
ok(c3._standby_cache is None, "picking a different image drops the old one")
c3.set_standby("X", "b.png")
ok(c3._standby_cache is None, "…and setting the SAME one does not churn it")

r = c3.set_standby("BACK SOON", "")
ok(r["standby"] == "BACK SOON" and r["standby_image"] == "",
   "clearing the image returns to the text")

src = open(os.path.join(HERE, "camera.py"), encoding="utf-8").read()
st = src[src.index("    def _standby(self, frame)"):]
st = st[:st.index("\n    def ", 10)]
ok("min(W / max(1, w), H / max(1, h))" in st,
   "the card is CONTAINED, not cropped — a standby card is a whole design, and "
   "losing its edges is worse than a letterbox")

print(f"{P} passed, {len(F)} failed")
for f in F:
    print("  FAIL:", f)
sys.exit(1 if F else 0)
