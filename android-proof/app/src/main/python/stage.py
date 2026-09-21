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
             mode: str = "timed", layer: str | None = None) -> dict:
        """Register an overlay; semantics mirror VirtualCam.fire_overlay."""
        mode = str(mode or "timed").lower()
        key = (str(layer or "").strip().lower()) or None
        if mode == "clear":
            return self.clear(key)
        name = os.path.basename(str(media or "").strip())
        if not name:
            return {"ok": False, "error": "no overlay media set"}
        if not os.path.isfile(os.path.join(self.images_dir, name)):
            return {"ok": False, "error": f"media not found: {name}"}
        try:
            secs = max(0.5, float(seconds or 5))
            sc = max(0.05, min(1.0, float(scale or 0.5)))
        except (TypeError, ValueError):
            secs, sc = 5.0, 0.5
        kind = ("video" if os.path.splitext(name)[1].lower() in VIDEO_EXTS
                else "image")
        # a play-once image is just a short timed one (same rule as the vcam)
        if mode == "once" and kind == "image":
            mode = "timed"
        ent = {"id": next(self._ids), "media": name, "kind": kind, "mode": mode,
               "seconds": secs, "pos": str(pos or "center").lower(),
               "scale": sc, "layer": key,
               "until": (time.time() + secs) if mode == "timed" else None}
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
            return [dict(o) for o in self._items]
