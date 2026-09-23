"""
stage.py — the server-side overlay registry behind the /stage page.

The Stage is the PHONE's answer to the desktop virtual camera: a fullscreen
page (camera feed + overlay layers composited in the DOM) that the user
screen-shares into a Discord call ("Share one app" keeps everything else
off-stream). Overlay actions land here exactly like they land on the virtual
cam — same modes (timed / hold / once / clear), same named layers — and the
page polls active() to reconcile what it renders. No OpenCV involved: the
browser does the compositing, so this works everywhere the panel does.
"""

from __future__ import annotations

import itertools
import os
import threading
import time

VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v")

# The six fields an overlay design carries for its slide legs. Listed here so
# app.py can lift them off a design item without naming them one at a time.
SLIDE_KEYS = ("slide_in", "slide_in_dir", "slide_in_secs",
              "slide_out", "slide_out_dir", "slide_out_secs")
SLIDE_EDGES = ("left", "right", "top", "bottom")
_EDGE_ALIAS = {"l": "left", "r": "right", "u": "top", "d": "bottom",
               "up": "top", "down": "bottom"}


def slide_spec(src: dict | None) -> dict:
    """Normalise an overlay's two optional slide legs.

    A direction names an EDGE of the frame: `slide_in_dir` is the side it
    arrives FROM, `slide_out_dir` the side it leaves TOWARD — so in-left /
    out-left is a pass-through and in-right / out-left is a sweep. The two
    durations are ADDITIVE: they bracket the overlay's own `seconds` rather
    than eating into it, so a 5s alert with 0.3s legs is on screen 5.6s and
    still sits still for the full 5.

    Returns {"in": leg|None, "out": leg|None}, a leg being {dir, secs}.
    Lives here (not camera.py) so the vcam, the Stage registry and the panel
    all read the same spec — and because stage.py has no cv2 dependency."""
    src = src or {}

    def leg(on, direction, secs):
        if not on:
            return None
        try:
            s = float(secs)
        except (TypeError, ValueError):
            s = 0.0
        d = str(direction or "").strip().lower()
        d = _EDGE_ALIAS.get(d, d)
        return {"dir": d if d in SLIDE_EDGES else "left",
                "secs": max(0.05, s or 0.4)}

    return {"in": leg(src.get("slide_in"), src.get("slide_in_dir"),
                      src.get("slide_in_secs")),
            "out": leg(src.get("slide_out"), src.get("slide_out_dir"),
                       src.get("slide_out_secs"))}


def slide_pad(spec: dict | None) -> float:
    """Seconds the slide legs ADD to an overlay's life (lead-in + tail-out)."""
    spec = spec or {}
    return float(sum((spec.get(k) or {}).get("secs", 0.0) for k in ("in", "out")))


class Stage:
    def __init__(self, images_dir: str):
        self.images_dir = images_dir
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._ids = itertools.count(1)
        self._last_poll = 0.0

    def watching(self, within: float = 10.0) -> bool:
        """True when a Stage page has polled recently (someone's watching)."""
        return (time.time() - self._last_poll) <= within

    def fire(self, media, seconds=5.0, pos: str = "center", scale=0.5,
             mode: str = "timed", layer: str | None = None,
             x=None, y=None, item=None, z=None, opacity=None,
             rot=None, flash=None, anim=None, anim_dir=None,
             slide=None) -> dict:
        """Register an overlay; semantics mirror VirtualCam.fire_overlay."""
        mode = str(mode or "timed").lower()
        key = (str(layer or "").strip().lower()) or None
        if mode == "clear":
            return self.clear(key)
        try:
            secs = max(0.5, float(seconds or 5))
            sc = max(0.05, min(1.0, float(scale or 0.5)))
        except (TypeError, ValueError):
            secs, sc = 5.0, 0.5
        # a drawn overlay carries its slide fields on the item itself; a media
        # one gets them lifted off the design by the router
        sl = slide_spec(item if item is not None else slide)
        life = secs + slide_pad(sl)      # the legs BRACKET `secs`, see slide_spec
        if item is not None:
            # a DRAWN overlay (text / gauge / timer): the page renders it from
            # the item's own style, so pass the whole spec through verbatim
            ent = {"id": next(self._ids), "kind": "draw", "item": dict(item),
                   "mode": mode, "seconds": secs, "layer": key, "z": z,
                   "slide": sl,
                   "until": (time.time() + life) if mode == "timed" else None}
            with self._lock:
                if key:
                    self._items = [o for o in self._items if o.get("layer") != key]
                self._items.append(ent)
            return {"ok": True, "overlays": len(self._items)}
        name = os.path.basename(str(media or "").strip())
        if not name:
            return {"ok": False, "error": "no overlay media set"}
        if not os.path.isfile(os.path.join(self.images_dir, name)):
            return {"ok": False, "error": f"media not found: {name}"}
        kind = ("video" if os.path.splitext(name)[1].lower() in VIDEO_EXTS
                else "image")
        # a play-once image is just a short timed one (same rule as the vcam)
        if mode == "once" and kind == "image":
            mode = "timed"
        ent = {"id": next(self._ids), "media": name, "kind": kind, "mode": mode,
               "seconds": secs, "pos": str(pos or "center").lower(),
               "scale": sc, "layer": key, "x": x, "y": y, "z": z,
               "opacity": opacity, "rot": rot, "flash": flash,
               "anim": anim, "anim_dir": anim_dir, "slide": sl,
               "until": (time.time() + life) if mode == "timed" else None}
        with self._lock:
            if key:   # named slot: replace the previous holder
                self._items = [o for o in self._items if o.get("layer") != key]
            self._items.append(ent)
        return {"ok": True, "overlays": len(self._items)}

    def clear(self, layer: str | None = None) -> dict:
        """Remove all overlays, or just the named layer."""
        key = (str(layer or "").strip().lower()) or None
        with self._lock:
            self._items = ([o for o in self._items if o.get("layer") != key]
                           if key else [])
        return {"ok": True}

    def update_item(self, oid: str, fields: dict) -> dict:
        """Change a live overlay's text in place (see VirtualCam.update_item)."""
        oid = str(oid or "")
        hit = 0
        with self._lock:
            for o in self._items:
                it = o.get("item")
                if not it or str(it.get("id")) != oid:
                    continue
                for k, v in (fields or {}).items():
                    if v is not None:
                        it[k] = v
                hit += 1
        return {"ok": True, "updated": hit}

    def done(self, oid) -> dict:
        """The page reports a play-once video finished — drop its entry."""
        try:
            oid = int(oid)
        except (TypeError, ValueError):
            return {"ok": True}
        with self._lock:
            self._items = [o for o in self._items if o["id"] != oid]
        return {"ok": True}

    def active(self) -> list[dict]:
        """Current overlays (timed ones pruned); marks the stage as watched."""
        now = time.time()
        self._last_poll = now
        with self._lock:
            self._items = [o for o in self._items
                           if not (o["until"] and o["until"] <= now)]
            # ship SECONDS LEFT, not the absolute deadline — the page times its
            # slide-out off this, and the browser's clock isn't ours
            return [dict(o, left=(round(o["until"] - now, 3)
                                  if o.get("until") else None))
                    for o in self._items]
