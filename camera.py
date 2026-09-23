"""
camera.py — DiscoFlate's virtual camera (the OBS-style overlay pipe).

Captures the real webcam, composites the active overlay layers into every
frame, and outputs a VIRTUAL CAMERA device that you select in Discord's video
settings — so overlays play inside your live camera feed. Overlays are timed
RGBA layers fired from the Chat tab's Overlay actions (and, later, the full
overlay/scene system, which plugs into fire_overlay/clear_overlays).

Desktop only. Deps: opencv-python-headless + pyvirtualcam, plus a virtual-cam
backend — Windows: OBS's virtual camera driver (install OBS once), Linux: the
v4l2loopback kernel module, macOS: OBS. Everything is import-guarded: when a
piece is missing, status() says exactly why instead of crashing the app.
"""

from __future__ import annotations

import colorsys
import os
import sys
import threading
import time

try:
    import cv2
    import numpy as np
    _CV_ERR = None
except Exception as e:  # noqa: BLE001 — missing/broken wheel
    cv2 = np = None
    _CV_ERR = f"opencv not available ({e.__class__.__name__})"

try:
    import pyvirtualcam
    _VC_ERR = None
except Exception as e:  # noqa: BLE001
    pyvirtualcam = None
    _VC_ERR = f"pyvirtualcam not available ({e.__class__.__name__})"


class VirtualCam:
    """One camera pipeline: real webcam → overlay compositor → virtual cam.
    Runs in a daemon thread; all overlay state is lock-guarded."""

    def __init__(self, images_dir: str):
        self.images_dir = images_dir
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._overlays: list[dict] = []
        # layer -> [entry, ...] waiting their turn (notification queueing)
        self._queued: dict[str, list] = {}
        self._err: str | None = None
        self._info: str = ""
        self._device = 0
        self._size = (1280, 720)
        self._fps = 30
        self._last = None   # latest composited frame (BGR) for the panel preview
        # OFF by default: we send the TRUE image, so remote viewers see you (and
        # overlay text) correctly. Discord mirrors your own self-view preview on
        # its end — that's cosmetic and local, not what viewers get.
        self._mirror = False
        self._frozen = False       # hold the last camera frame (outro freeze)
        # Blackout: the CAMERA picture is suppressed but overlays still draw on
        # top — so an intro can play over black without leaking your room.
        self._black = False
        # What a viewer sees if they pick the virtual camera before you go
        # live. app.py keeps it in step with the live scene's Go Live block.
        self._standby_text = "STARTING SOON"
        # () -> 'live' | 'intro' | 'off'. app.py ties this to LIVE, and it's
        # asked every frame so it can never drift:
        #   'live'  — the camera picture, every overlay
        #   'intro' — black picture; only what the pre-show fires is drawn,
        #             the scene's always-on overlays stay down
        #   'off'   — a genuinely black screen: no picture, NO overlays
        self.gate_cb = None
        self._raw = None           # the last frame read from the webcam
        # () -> {"capacity","firing","remaining","device_timers":[...]} for widgets
        self.state_cb = None
        # () -> rendered string. Set by app.py to engine.render, so ANY
        # placeholder still in an overlay's text keeps updating on screen.
        self.render_cb = None
        self._rtxt: dict = {}      # cache: raw text -> (rendered, when)
        self._state = {}
        self._state_at = 0.0

    # -- lifecycle ------------------------------------------------------------ #
    def available(self) -> str | None:
        """None when the pipeline can run; else the human reason it can't."""
        return _CV_ERR or _VC_ERR

    def status(self) -> dict:
        running = bool(self._thread and self._thread.is_alive())
        return {"running": running,
                "error": ("" if running else (self.available() or self._err or "")),
                "info": self._info, "device": self._device,
                "width": self._size[0], "height": self._size[1], "fps": self._fps,
                "mirror": self._mirror, "frozen": self._frozen,
                "blackout": self._black or self._gate_mode() != "live",
                "gate": self._gate_mode(),
                "overlays": len(self._overlays)}

    def _gate_mode(self) -> str:
        if self.gate_cb is None:
            return "live"
        try:
            m = self.gate_cb()
        except Exception:  # noqa: BLE001 — never let a bad gate kill the feed
            return "live"
        if m is True:
            return "live"
        if m is False:
            return "off"
        return str(m or "live")

    def _gate_open(self) -> bool:
        return self._gate_mode() == "live" 

    def set_standby(self, text: str) -> dict:
        """The line shown on the pre-show black frame ("" = plain black)."""
        self._standby_text = str(text or "")
        return {"ok": True, "standby": self._standby_text}

    def set_blackout(self, on: bool) -> dict:
        """Black out the camera image while STILL compositing overlays over
        it. Going live to an intro uses this so viewers never see the room
        before the show starts."""
        self._black = bool(on)
        return {"ok": True, "blackout": self._black}

    def set_frozen(self, on: bool) -> dict:
        """Freeze-frame: stop pulling new webcam frames and keep compositing
        over the last one. Overlays still animate on top, so an outro can
        freeze the picture, fade to black, then stop the camera."""
        self._frozen = bool(on)
        return {"ok": True, "frozen": self._frozen}

    def set_mirror(self, on: bool) -> dict:
        """Flip the CAMERA IMAGE horizontally (live). Overlays are drawn on
        top afterwards, so your text always reads the right way round to
        viewers whichever way this is set.

        Use it when your camera hands you a reversed picture, or when
        something in shot (a sign, a label) reads backwards. It is NOT a fix
        for Discord's self-view: Discord mirrors your own tile and gives no
        way to disable that, so your overlays will read backwards TO YOU there
        regardless. Judge your layout in the panel's preview, which is the
        finished frame exactly as sent."""
        self._mirror = bool(on)
        return {"ok": True, "mirror": self._mirror}

    def start(self, device=0, width=1280, height=720, fps=30, mirror=None) -> dict:
        why = self.available()
        if why:
            return {"ok": False, "error":
                    why + " — install the requirements (Windows also needs OBS once, "
                          "for its virtual-camera driver), then restart DiscoFlate"}
        if self._thread and self._thread.is_alive():
            return {"ok": False, "error": "virtual camera is already running"}
        try:
            self._device = int(device or 0)
            self._size = (max(160, int(width or 1280)), max(120, int(height or 720)))
            self._fps = max(5, min(60, int(fps or 30)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad camera parameters"}
        if mirror is not None:
            self._mirror = bool(mirror)
        self._stop.clear()
        self._err = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="discoflate-vcam")
        self._thread.start()
        return {"ok": True}

    def stop(self) -> dict:
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout=3)
        self._thread = None
        self._info = ""
        return {"ok": True}

    # landscape first, then portrait — drivers snap each request to the nearest
    # mode the sensor really has, so only genuinely supported sizes come back
    _PROBE_RES = ((640, 480), (1280, 720), (1920, 1080), (2560, 1440),
                  (480, 640), (720, 1280), (1080, 1920))

    def detect(self, max_devices: int = 5) -> dict:
        """Probe attached webcams (device 0..max_devices-1) and the resolutions
        each one actually delivers. Portrait modes appear only when the
        hardware truly provides them."""
        if _CV_ERR:
            return {"ok": False, "error": _CV_ERR}
        if self._thread and self._thread.is_alive():
            return {"ok": False, "error":
                    "stop the virtual camera first — detecting needs the webcam"}
        cams = []
        for idx in range(max_devices):
            cap = None
            try:
                cap = cv2.VideoCapture(idx)
                ok, _f = cap.read()
                if not ok:
                    continue
                modes = []
                for w, h in self._PROBE_RES:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                    ok, f = cap.read()   # the frame itself is the ground truth
                    if not ok or f is None:
                        continue
                    got = (int(f.shape[1]), int(f.shape[0]))
                    if got not in modes:
                        modes.append(got)
                if modes:
                    cams.append({"device": idx,
                                 "modes": [f"{w}x{h}" for w, h in modes]})
            except Exception:  # noqa: BLE001 — a broken driver shouldn't kill the scan
                continue
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:  # noqa: BLE001
                        pass
        return {"ok": True, "cameras": cams}

    # -- overlays --------------------------------------------------------------#
    _VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v")

    def clear_overlays(self, layer: str | None = None, fade_out=0.0) -> dict:
        """Remove all overlays, or just the named layer. With `fade_out` the
        layer dies over N seconds instead of vanishing. Clearing a layer that
        isn't up is a silent no-op — callers ask for 'gone', not 'was there'."""
        key = (layer or "").strip().lower()
        try:
            fade = max(0.0, float(fade_out or 0))
        except (TypeError, ValueError):
            fade = 0.0
        now = time.monotonic()
        hit = 0
        with self._lock:
            for o in self._overlays:
                if key and o.get("layer") != key:
                    continue
                hit += 1
                if fade:
                    o["fade_out"] = fade
                    o["until"] = min(o["until"], now + fade) if o.get("until") else now + fade
                else:
                    o["dead"] = True
        return {"ok": True, "cleared": hit}

    _QUEUE_MAX = 12      # a burst deeper than this is noise; drop the overflow

    @staticmethod
    def _arm(ent: dict, now: float) -> None:
        """Stamp when a layer starts and (for timed ones) when it ends. A
        delayed layer is admitted immediately but stays invisible until its
        moment — that's what staggers a scene group without a timeline editor."""
        try:
            d = max(0.0, float(ent.get("delay") or 0))
        except (TypeError, ValueError):
            d = 0.0
        ent["start_at"] = now + d
        ent["born"] = ent["start_at"]          # fades begin when it appears
        if ent.get("_dur"):
            ent["until"] = ent["start_at"] + ent["_dur"]

    def _admit(self, ent: dict, key: str | None) -> dict:
        """Put an overlay on screen, or QUEUE it behind the one already on its
        layer. Queueing is what stops a rush of notifications from landing on
        top of each other — they play one after another instead."""
        with self._lock:
            live = [o for o in self._overlays
                    if key and o.get("layer") == key and not o.get("dead")]
            if key and live and ent.get("queue"):
                q = self._queued.setdefault(key, [])
                if len(q) >= self._QUEUE_MAX:
                    return {"ok": True, "dropped": True}
                q.append(ent)
                return {"ok": True, "queued": len(q)}
            for o in live:      # no queueing: the newest replaces
                o["dead"] = True
            self._arm(ent, time.monotonic())
            self._overlays.append(ent)
        return {"ok": True, "overlays": len(self._overlays)}

    def update_item(self, oid: str, fields: dict) -> dict:
        """Change a LIVE overlay's text without re-firing it — so a scoreboard
        or status line can tick over in place. Queued copies get it too, so a
        waiting alert shows the current value when its turn comes. Silent when
        that overlay isn't on screen."""
        oid = str(oid or "")
        hit = 0
        with self._lock:
            pools = [self._overlays] + list(self._queued.values())
            for pool in pools:
                for o in pool:
                    it = o.get("item")
                    if not it or str(it.get("id")) != oid or o.get("dead"):
                        continue
                    for k, v in (fields or {}).items():
                        if v is not None:
                            it[k] = v
                    hit += 1
        return {"ok": True, "updated": hit}

    def clear_queue(self, layer: str | None = None) -> dict:
        """Drop overlays waiting their turn (all, or one layer's)."""
        with self._lock:
            n = sum(len(v) for v in self._queued.values()) if not layer \
                else len(self._queued.get(str(layer).strip().lower(), []))
            if layer:
                self._queued.pop(str(layer).strip().lower(), None)
            else:
                self._queued.clear()
        return {"ok": True, "dropped": n}

    def add_item(self, item: dict, seconds=None, layer: str | None = None,
                 always_on: bool = False) -> dict:
        """A DRAWN overlay layer — text / capacity_gauge / pump_timer — rendered
        fresh each frame from `item` (its full style: color/size/font/bg/rot/
        orient/flash) plus live game state. Named layers replace, like media."""
        item = dict(item or {})
        key = (str(layer or item.get("layer") or "").strip().lower()) or None
        until = None
        if seconds:
            try:
                until = time.monotonic() + max(0.2, float(seconds))
            except (TypeError, ValueError):
                until = None
        ent = {"kind": "draw", "item": item, "layer": key, "dead": False,
               "until": until, "_dur": (until - time.monotonic()) if until else None,
               "pos": str(item.get("pos") or "center").lower(),
               "x": item.get("x"), "y": item.get("y"),
               "center": bool(item.get("center")),
               "rot": item.get("rot"), "flash": item.get("flash"),
               "fade_in": item.get("fade_in"), "fade_out": item.get("fade_out"),
               "anim": item.get("anim"), "anim_dir": item.get("anim_dir"),
               "queue": item.get("queue"), "delay": item.get("delay"),
               "always_on": always_on, "born": time.monotonic()}
        return self._admit(ent, key)

    def add_widget(self, widget: str, x=None, y=None, w=None,
                   layer: str | None = None) -> dict:
        """Back-compat shim for the plain gauge/timer call."""
        return self.add_item({"kind": str(widget or ""), "x": x, "y": y,
                              "w": w or 0.35}, layer=layer)

    def fire_overlay(self, media: str, seconds=5.0, pos: str = "center",
                     scale=0.5, mode: str = "timed", layer: str | None = None,
                     x=None, y=None, rot=None, flash=None, h=None,
                     fade_in=None, fade_out=None, anim=None, anim_dir=None,
                     queue=None, chroma_on=None, chroma=None, chroma_tol=None,
                     chroma_soft=None, z=None, opacity=None, delay=None,
                     always_on=False) -> dict:
        """Show a MEDIA layer over the camera. `media` = an image (PNG alpha
        welcome) or a video file from data/images (or an absolute path).
        mode: "timed"  = shown/looping for `seconds`
              "hold"   = stays until cleared or replaced (video loops)
              "once"   = a video plays through once, then removes itself
              "clear"  = remove the named layer (or ALL when no layer given)
        `layer` names the slot — firing the same layer name REPLACES it (scene
        building). pos/scale as before (anchor + fraction of frame width);
        x/y are stage-designer fractions that override pos, `rot` spins the
        layer, `flash` blinks it (seconds visible = seconds hidden)."""
        why = self.available()
        if why:
            return {"ok": False, "error": why}
        mode = str(mode or "timed").lower()
        key = (str(layer or "").strip().lower()) or None
        if mode == "clear":
            return self.clear_overlays(key)
        name = str(media or "").strip()
        if not name:
            return {"ok": False, "error": "no overlay media set"}
        path = name if os.path.isabs(name) else os.path.join(self.images_dir,
                                                             os.path.basename(name))
        try:
            secs = max(0.5, float(seconds or 5))
            sc = max(0.05, min(1.0, float(scale or 0.5)))
        except (TypeError, ValueError):
            secs, sc = 5.0, 0.5
        entry = {"layer": key, "pos": str(pos or "center").lower(), "scale": sc,
                 "dead": False, "x": x, "y": y,   # fractional coords beat `pos`
                 "rot": rot, "flash": flash, "h": h,
                 "fade_in": fade_in, "fade_out": fade_out, "born": time.monotonic(),
                 "anim": anim, "anim_dir": anim_dir, "queue": queue,
                 "z": z, "opacity": opacity, "delay": delay,
                 "always_on": always_on,
                 "chroma_on": chroma_on, "chroma": chroma,
                 "chroma_tol": chroma_tol, "chroma_soft": chroma_soft,
                 "until": (time.monotonic() + secs) if mode == "timed" else None}
        if os.path.splitext(path)[1].lower() in self._VIDEO_EXTS:
            cap = cv2.VideoCapture(path)
            ok, _f = cap.read()
            if not ok:
                cap.release()
                return {"ok": False, "error": f"couldn't read video: {name}"}
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            entry.update({"kind": "video", "cap": cap,
                          "loop": mode != "once"})
        else:
            img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if img is None:
                return {"ok": False, "error": f"couldn't read image: {name}"}
            if img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
            elif img.shape[2] == 3:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
            if mode == "once":   # a play-once image is just a short timed one
                entry["until"] = time.monotonic() + secs
            entry.update({"kind": "image", "img": img})
        entry["_dur"] = secs if mode == "timed" else None
        return self._admit(entry, key)

    # -- the pipeline thread ---------------------------------------------------#
    @staticmethod
    def _place(W: int, H: int, w: int, h: int, pos: str) -> tuple[int, int]:
        x = (W - w) // 2
        y = (H - h) // 2
        if "left" in pos:
            x = int(W * 0.03)
        if "right" in pos:
            x = W - w - int(W * 0.03)
        if "top" in pos:
            y = int(H * 0.03)
        if "bottom" in pos:
            y = H - h - int(H * 0.03)
        return max(0, x), max(0, y)

    def _get_state(self) -> dict:
        """Cached game state for widgets — refreshed at most twice a second."""
        now = time.monotonic()
        if self.state_cb is not None and now - self._state_at > 0.5:
            try:
                self._state = self.state_cb() or {}
            except Exception:  # noqa: BLE001
                pass
            self._state_at = now
        return self._state

    # ---- sprite rendering: text / gauge / timer, all as RGBA layers -------- #
    _FONTS = {"sans": 0, "bold": 1, "serif": 3, "mono": 4, "script": 6}
    # cv2: 0=SIMPLEX 1=PLAIN 3=COMPLEX 4=TRIPLEX 6=SCRIPT_SIMPLEX

    @staticmethod
    def _bgr(color, default=(255, 255, 255)):
        """#RRGGBB (or #RGB) -> BGR tuple. Blank/bad -> default."""
        s = str(color or "").strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            return default
        try:
            return (int(s[4:6], 16), int(s[2:4], 16), int(s[0:2], 16))
        except ValueError:
            return default

    @staticmethod
    def _ramp(frac: float) -> tuple:
        """Capacity colour: green → YELLOW → red. Interpolating the HUE (not
        raw RGB) is what puts a real yellow at the midpoint; this matches the
        `hsl(120-1.2*pct 70% 45%)` the Stage page uses, so both surfaces agree."""
        h = (120.0 * (1.0 - max(0.0, min(1.0, frac)))) / 360.0
        r, g, b = colorsys.hls_to_rgb(h, 0.45, 0.70)
        return (int(b * 255), int(g * 255), int(r * 255))   # BGR

    def _text_sprite(self, txt: str, item: dict, H: int):
        """An RGBA sprite of `txt` honouring color / size / font / bg.
        MULTI-LINE: newlines split into stacked lines, aligned left / center /
        right against the widest one."""
        txt = str(txt if txt is not None else "")
        if not txt:
            return None
        lines = txt.replace("\r\n", "\n").replace("\\n", "\n").split("\n")
        if not any(l.strip() for l in lines):
            return None
        font = self._FONTS.get(str(item.get("font") or "sans").lower(), 0)
        try:
            size = max(0.01, min(0.9, float(item.get("size") or 0.06)))
        except (TypeError, ValueError):
            size = 0.06
        px = max(10, int(H * size))                 # target cap height in pixels
        scale = px / 22.0                           # Hershey units -> ~px
        thick = max(1, int(round(scale * 1.6)))
        # Padding exists to give a BACKGROUND box some breathing room. Unboxed
        # text needs only enough not to clip its own outline — charging it the
        # full box padding pushed every line ~0.4x its own size below where the
        # canvas showed it, and stacked device rows twice as far apart.
        boxed = bool(str(item.get("bg") or "").strip())
        pad = max(4, int(px * 0.28)) if boxed else max(2, int(px * 0.08))
        gap = max(2, int(px * 0.30))                # leading -> ~1.15x advance, as the canvas
        metrics = []
        for ln in lines:
            (tw, th), base = cv2.getTextSize(ln or " ", font, scale, thick)
            metrics.append((tw, th, base))
        body_w = max(m[0] for m in metrics)
        line_h = max(m[1] + m[2] for m in metrics)
        w = body_w + pad * 2
        h = line_h * len(lines) + gap * (len(lines) - 1) + pad * 2
        sp = np.zeros((h, w, 4), np.uint8)
        bg = str(item.get("bg") or "").strip()
        if bg:                                      # blank bg = transparent
            sp[:, :, :3] = self._bgr(bg, (0, 0, 0))
            sp[:, :, 3] = 255
        align = str(item.get("align") or "left").lower()
        col = self._bgr(item.get("color"), (255, 255, 255))
        y = pad
        for ln, (tw, th, base) in zip(lines, metrics):
            if align.startswith("c"):
                x = (w - tw) // 2
            elif align.startswith("r"):
                x = w - pad - tw
            else:
                x = pad
            org = (x, y + th)
            if ln.strip():
                if not bg:   # unbacked text gets an outline so it reads on any feed
                    cv2.putText(sp, ln, org, font, scale, (0, 0, 0, 255),
                                thick + max(2, thick), cv2.LINE_AA)
                cv2.putText(sp, ln, org, font, scale, (*col, 255), thick, cv2.LINE_AA)
            y += line_h + gap
        return sp

    def _gauge_sprite(self, item: dict, W: int, H: int, pct: float):
        """Capacity bar as an RGBA sprite; horizontal or vertical."""
        vert = str(item.get("orient") or "h").lower().startswith("v")
        try:
            length_f = max(0.04, min(1.0, float(item.get("w") or 0.35)))
            thick_f = max(0.01, min(0.6, float(item.get("size") or 0.05)))
        except (TypeError, ValueError):
            length_f, thick_f = 0.35, 0.05
        length = max(24, int((H if vert else W) * length_f))
        thick = max(10, int(H * thick_f))
        w, h = (thick, length) if vert else (length, thick)
        sp = np.zeros((h, w, 4), np.uint8)
        frac = max(0.0, min(1.0, pct / 100.0))
        fill = self._bgr(item.get("color"), None) or self._ramp(frac)
        bg = self._bgr(item.get("bg"), (30, 30, 30))
        sp[:, :, :3] = bg
        sp[:, :, 3] = 255
        if frac > 0:
            n = max(1, int((h if vert else w) * frac))
            if vert:   # vertical bars fill upward
                sp[h - n:, :, :3] = fill
            else:
                sp[:, :n, :3] = fill
        cv2.rectangle(sp, (0, 0), (w - 1, h - 1), (240, 240, 240, 255), 2)
        if item.get("show_pct", True):
            lbl = f"{pct:.0f}%"
            fs = max(0.35, thick / 42.0)
            (tw, th), _b = cv2.getTextSize(lbl, 0, fs, max(1, int(fs * 2)))
            if tw < w - 4 and th < h - 4:
                org = ((w - tw) // 2, (h + th) // 2)
                cv2.putText(sp, lbl, org, 0, fs, (0, 0, 0, 255),
                            max(3, int(fs * 4)), cv2.LINE_AA)
                cv2.putText(sp, lbl, org, 0, fs, (255, 255, 255, 255),
                            max(1, int(fs * 2)), cv2.LINE_AA)
        return sp

    def _timers_sprite(self, item: dict, H: int, st: dict, single: bool = False):
        """The Device Timer List: one row per device, PRIMARY PUMP FIRST, each
        with its own countdown. `single` renders just the primary row (what the
        old Pump Timer overlay was). Rows appear/disappear as devices are added
        in settings, so the list grows with the rig."""
        rows = list(st.get("device_timers") or [])
        if not rows:   # no devices configured yet — fall back to the bare timer
            rem = float(st.get("remaining") or 0)
            on = bool(st.get("firing"))
            rows = [{"name": "PUMP", "primary": True, "firing": on, "remaining": rem}]
        if single:
            rows = rows[:1]
        elif not item.get("show_idle", True):
            rows = [r for r in rows if r.get("firing")] or []
        if not rows:
            return None
        fmt_on = str(item.get("fmt_on") or "[name] [secs]s")
        fmt_off = str(item.get("fmt_off") or "[name] idle")
        lines = []
        for r in rows:
            f = fmt_on if r.get("firing") else fmt_off
            lines.append(f.replace("[name]", str(r.get("name") or "device"))
                          .replace("[secs]", f"{float(r.get('remaining') or 0):.0f}"))
        sprites = [self._text_sprite(t, item, H) for t in lines]
        sprites = [s for s in sprites if s is not None]
        if not sprites:
            return None
        if len(sprites) == 1:
            return sprites[0]
        # rows are already padded individually — keep the seam tight so the
        # list reads as one block, the way the canvas draws it
        gap = max(1, int(H * 0.002))
        w = max(s.shape[1] for s in sprites)
        h = sum(s.shape[0] for s in sprites) + gap * (len(sprites) - 1)
        out = np.zeros((h, w, 4), np.uint8)
        y = 0
        for s in sprites:
            out[y:y + s.shape[0], :s.shape[1]] = s
            y += s.shape[0] + gap
        return out

    def _poll_sprite(self, item: dict, W: int, H: int, pv: dict):
        """The Poll Viewer: an embed-style card sized by w/h, listing each
        option with a vote bar, plus its own countdown (time left while the
        poll runs, then how long the results stay up)."""
        try:
            bw = max(120, int(W * float(item.get("w") or 0.34)))
            bh = max(80, int(H * float(item.get("h") or 0.30)))
        except (TypeError, ValueError):
            bw, bh = int(W * 0.34), int(H * 0.30)
        sp = np.zeros((bh, bw, 4), np.uint8)
        bg = self._bgr(item.get("bg"), (18, 18, 22))
        sp[:, :, :3] = bg
        bga = item.get("bg_opacity")
        if bga is None:                 # pre-3.53 configs stored 0-255 here
            bga = item.get("opacity") if (item.get("opacity") or 0) > 100 else 220
        sp[:, :, 3] = int(max(0, min(255, float(bga or 220))))
        accent = self._bgr(item.get("color"), (244, 168, 40))
        cv2.rectangle(sp, (0, 0), (bw - 1, bh - 1), (*accent, 255), 2)
        cv2.rectangle(sp, (0, 0), (5, bh - 1), (*accent, 255), -1)   # embed spine
        pad = max(8, int(bh * 0.07))
        fs = max(0.4, bh / 300.0)
        y = pad + int(fs * 26)
        cv2.putText(sp, str(pv.get("title") or "Poll")[:42], (pad + 8, y),
                    0, fs * 1.05, (255, 255, 255, 255), max(1, int(fs * 2)), cv2.LINE_AA)
        # countdown, right-aligned on the title row
        rem = pv.get("remaining")
        if rem is None and pv.get("_hold") is not None:
            rem = pv["_hold"]
        if rem is not None:
            lbl = f"{float(rem):.0f}s"
            (tw_, _t), _b = cv2.getTextSize(lbl, 0, fs * 0.95, max(1, int(fs * 2)))
            cv2.putText(sp, lbl, (bw - pad - tw_ - 4, y), 0, fs * 0.95,
                        (*accent, 255), max(1, int(fs * 2)), cv2.LINE_AA)
        opts = pv.get("options") or []
        total = max(1, int(pv.get("total") or 0))
        rows = opts[:8]
        room = bh - y - pad
        rh = max(14, int(room / max(1, len(rows))))
        winner = pv.get("winner")
        for i, o in enumerate(rows):
            ry = y + int(rh * (i + 0.35)) + 4
            if ry + 6 > bh - 2:
                break
            votes = int(o.get("votes") or 0)
            frac = votes / total if pv.get("total") else 0.0
            barw = int((bw - pad * 2 - 8) * max(0.0, min(1.0, frac)))
            bar_y2 = min(bh - 2, ry + max(6, int(rh * 0.42)))
            cv2.rectangle(sp, (pad + 4, ry), (bw - pad - 4, bar_y2), (46, 46, 54, 255), -1)
            if barw > 1:
                col = (*accent, 255) if (winner is None or i == winner) else (110, 110, 120, 255)
                cv2.rectangle(sp, (pad + 4, ry), (pad + 4 + barw, bar_y2), col, -1)
            txt = ("🏆 " if i == winner else "") + f"{o.get('label', '')[:26]} · {votes}"
            txt = txt.replace("🏆 ", "> ")      # cv2 can't draw emoji
            cv2.putText(sp, txt, (pad + 10, bar_y2 - max(2, int(rh * 0.12))),
                        0, fs * 0.8, (0, 0, 0, 255), max(3, int(fs * 4)), cv2.LINE_AA)
            cv2.putText(sp, txt, (pad + 10, bar_y2 - max(2, int(rh * 0.12))),
                        0, fs * 0.8, (255, 255, 255, 255), max(1, int(fs * 2)), cv2.LINE_AA)
        return sp

    def _live(self, txt: str, st: dict) -> str:
        """Re-render the placeholders still in an overlay's text, so a label
        keeps counting while it's on screen. Actor tokens ([user] etc.) were
        already baked when it fired; what's left is global state — capacity,
        the pump timer, uptime, variables. Cached ~4x/sec: a countdown needs
        to tick, not to re-render 30 times a second."""
        if "[" not in txt:
            return txt
        now = time.monotonic()
        hit = self._rtxt.get(txt)
        if hit and now - hit[1] < 0.25:
            return hit[0]
        out = txt
        if self.render_cb is not None:
            try:
                out = self.render_cb(txt)
            except Exception:  # noqa: BLE001 — a bad token never blanks a scene
                out = txt
        else:   # no engine attached (tests): the two tokens we can do locally
            try:
                out = (txt.replace("[capacity]", f"{float(st.get('capacity') or 0):.0f}")
                          .replace("[secs]", f"{float(st.get('remaining') or 0):.0f}"))
            except (TypeError, ValueError):
                pass
        if len(self._rtxt) > 64:
            self._rtxt.clear()
        self._rtxt[txt] = (out, now)
        return out

    def _solid_sprite(self, item: dict, W: int, H: int):
        """A flat rectangle of `color` sized by w/h — a backdrop card, a
        letterbox bar, a plate under a lower third. Exists so a scene can
        carry a full-frame background WITHOUT shipping an image file."""
        try:
            bw = max(1, int(W * float(item.get("w") or 1.0)))
            bh = max(1, int(H * float(item.get("h") or 1.0)))
        except (TypeError, ValueError):
            bw, bh = W, H
        sp = np.zeros((bh, bw, 4), np.uint8)
        sp[:, :, :3] = self._bgr(item.get("color"), (0, 0, 0))
        sp[:, :, 3] = 255          # layer opacity is applied later, per-frame
        return sp

    def _render_item(self, item: dict, W: int, H: int):
        """RGBA sprite for a non-media overlay, or None to draw nothing."""
        kind = str(item.get("kind") or "text")
        if kind == "audio":
            return None          # a sound cue draws nothing — by design
        if kind == "solid":
            return self._solid_sprite(item, W, H)
        st = self._get_state()
        if kind == "capacity_gauge":
            try:
                pct = float(st.get("capacity") or 0)
            except (TypeError, ValueError):
                pct = 0.0
            return self._gauge_sprite(item, W, H, pct)
        if kind == "poll_viewer":
            pv = st.get("poll")
            if not pv:
                return None                     # no poll → nothing on screen
            if pv.get("phase") == "results":
                try:
                    hold = max(0.0, float(item.get("results_secs", 8) or 0))
                except (TypeError, ValueError):
                    hold = 8.0
                age = float(pv.get("results_age") or 0)
                if age > hold:
                    return None                 # results window elapsed
                pv = {**pv, "_hold": max(0.0, hold - age)}
            return self._poll_sprite(item, W, H, pv)
        if kind == "timer":
            # a countdown the action blocks start/stop; before it ever runs it
            # just shows its configured length, so the scene reads right idle
            oid = str(item.get("id") or "")
            secs = (st.get("timers") or {}).get(oid)
            if secs is None:
                try:
                    secs = float(item.get("seconds") or 0)
                except (TypeError, ValueError):
                    secs = 0.0
            txt = str(item.get("text") or item.get("label") or "[secs]")
            if "[secs]" not in txt and "[mmss]" not in txt:
                txt = (txt + " [secs]").strip()
            m, s = divmod(max(0, int(round(float(secs)))), 60)
            return self._text_sprite(txt.replace("[secs]", f"{float(secs):.0f}")
                                        .replace("[mmss]", f"{m}:{s:02d}"), item, H)
        if kind in ("pump_timer", "device_timers"):
            return self._timers_sprite(item, H, st, single=(kind == "pump_timer"))
        return self._text_sprite(self._live(str(item.get("text") or ""), st), item, H)

    @staticmethod
    def _rotate(sprite, deg):
        """Rotate an RGBA sprite about its centre, expanding to fit."""
        try:
            deg = float(deg or 0)
        except (TypeError, ValueError):
            return sprite
        if not deg % 360:
            return sprite
        h, w = sprite.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
        cos, sin = abs(M[0, 0]), abs(M[0, 1])
        nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
        M[0, 2] += nw / 2 - w / 2
        M[1, 2] += nh / 2 - h / 2
        return cv2.warpAffine(sprite, M, (max(1, nw), max(1, nh)),
                              flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))

    @staticmethod
    def _anim_offset(o: dict, now: float, w: int, h: int) -> tuple:
        """Pixel offset for a sliding overlay: it flies IN from its direction
        while fading up, sits still, then flies OUT that way while fading down.
        Pure ease-out on a static direction — no keyframes to author."""
        if not o.get("anim"):
            return (0, 0)
        d = str(o.get("anim_dir") or "up").lower()
        dist = (h if d in ("up", "down") else w) * 0.9 + 12
        t = 0.0                     # 0 = in place, 1 = fully off in `d`
        try:
            fin = float(o.get("fade_in") or 0)
            if fin > 0 and o.get("born"):
                p = (now - o["born"]) / fin
                if p < 1.0:
                    t = -(1.0 - max(0.0, p)) ** 2       # arrive FROM `d`
            fout = float(o.get("fade_out") or 0)
            if fout > 0 and o.get("until"):
                p = (o["until"] - now) / fout
                if p < 1.0:
                    t = (1.0 - max(0.0, p)) ** 2        # leave TOWARD `d`
        except (TypeError, ValueError, ZeroDivisionError):
            return (0, 0)
        off = int(dist * t)
        if d == "up":
            return (0, -off)
        if d == "down":
            return (0, off)
        if d == "left":
            return (-off, 0)
        return (off, 0)

    def _chroma(self, bgra, item: dict):
        """Green-screen keying: knock the key colour out of an OPAQUE frame so
        a plain .mp4 can be used as a transparent overlay (no alpha codec
        needed). Distance in HSV hue/sat space, with a soft edge so hair and
        motion blur don't get a hard jagged cut."""
        try:
            tol = max(1.0, min(120.0, float(item.get("chroma_tol") or 35)))
            soft = max(0.0, min(80.0, float(item.get("chroma_soft") or 12)))
        except (TypeError, ValueError):
            tol, soft = 35.0, 12.0
        key = self._bgr(item.get("chroma"), (0, 255, 0))     # default: green
        kh = cv2.cvtColor(np.uint8([[list(key)]]), cv2.COLOR_BGR2HSV)[0][0]
        hsv = cv2.cvtColor(bgra[:, :, :3], cv2.COLOR_BGR2HSV)
        dh = np.abs(hsv[:, :, 0].astype("int16") - int(kh[0]))
        dh = np.minimum(dh, 180 - dh).astype("float32")      # hue is a circle
        sat = hsv[:, :, 1].astype("float32")
        # fully transparent inside `tol`, fading to opaque across `soft`
        a = np.clip((dh - tol) / max(1.0, soft), 0.0, 1.0)
        a[sat < 40] = 1.0          # near-grey pixels are never the key colour
        out = bgra.copy()
        out[:, :, 3] = (out[:, :, 3].astype("float32") * a).astype("uint8")
        return out

    @staticmethod
    def _fade_alpha(o: dict, now: float) -> float:
        """0..1 opacity for a layer: its own opacity setting, further scaled by
        any fade_in / fade_out in progress."""
        a = 1.0
        try:
            fin = float(o.get("fade_in") or 0)
            if fin > 0 and o.get("born"):
                a = min(a, (now - o["born"]) / fin)
            fout = float(o.get("fade_out") or 0)
            if fout > 0 and o.get("until"):
                a = min(a, (o["until"] - now) / fout)
        except (TypeError, ValueError, ZeroDivisionError):
            a = 1.0
        # the layer's own opacity SCALES the fade (a 50% layer halfway through
        # a fade-in is 25%, not 50%) — min() here would swallow one of them
        src = o.get("item") if o.get("kind") == "draw" else o
        raw = (src or {}).get("opacity", o.get("opacity"))
        if raw is not None:
            try:                        # stored 0-100; higher = an old 0-255 value
                op = float(raw)
                a *= max(0.0, min(1.0, (op / 255.0) if op > 100 else (op / 100.0)))
            except (TypeError, ValueError):
                pass
        return max(0.0, min(1.0, a))

    @staticmethod
    def _blend(frame, ov, x: int, y: int, alpha: float = 1.0) -> None:
        """Alpha-composite an RGBA sprite onto the frame at x,y (clipped)."""
        H, W = frame.shape[:2]
        th, tw = ov.shape[:2]
        sx, sy = max(0, -x), max(0, -y)      # sprite may start off the left/top
        x, y = max(0, x), max(0, y)
        th = min(th - sy, H - y)
        tw = min(tw - sx, W - x)
        if th <= 0 or tw <= 0:
            return
        ov = ov[sy:sy + th, sx:sx + tw]
        roi = frame[y:y + th, x:x + tw]
        a = ov[:, :, 3:4].astype("float32") / 255.0
        if alpha < 1.0:
            a = a * max(0.0, alpha)
        roi[:] = (ov[:, :, :3].astype("float32") * a
                  + roi.astype("float32") * (1.0 - a)).astype("uint8")

    def _reap_only(self) -> None:
        """Expire timed overlays while nothing is being drawn, so a blacked-out
        stretch doesn't leave a backlog to dump on screen when it lifts."""
        now = time.monotonic()
        with self._lock:
            dead = [o for o in self._overlays
                    if o.get("dead") or (o.get("until") and o["until"] <= now)]
            self._overlays = [o for o in self._overlays if o not in dead]
        for o in dead:
            cap = o.get("cap")
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass

    def _standby(self, frame) -> None:
        """Centre the standby line on an otherwise black pre-show frame.
        Empty text = a genuinely black screen, as before."""
        txt = str(self._standby_text or "").strip()
        if not txt:
            return
        H, W = frame.shape[:2]
        sp = self._text_sprite(txt, {"size": 0.14, "align": "center", "font": "bold",
                                     "color": "#ffffff", "bg": ""}, H)
        if sp is None:
            return
        h, w = sp.shape[:2]
        if w > W or h > H:      # a very long line on a small frame
            k = min(W / max(1, w), H / max(1, h))
            sp = cv2.resize(sp, (max(1, int(w * k)), max(1, int(h * k))))
            h, w = sp.shape[:2]
        self._blend(frame, sp, (W - w) // 2, (H - h) // 2)

    def _composite(self, frame, intro: bool = False):
        now = time.monotonic()
        with self._lock:
            dead = [o for o in self._overlays
                    if o.get("dead") or (o.get("until") and o["until"] <= now)]
            self._overlays = [o for o in self._overlays if o not in dead]
            ovs = list(self._overlays)
        for o in dead:   # release finished video captures
            cap = o.get("cap")
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass
        for o in dead:   # a freed layer pulls in whoever was waiting for it
            key = o.get("layer")
            if not key or not self._queued.get(key):
                continue
            with self._lock:
                if any(x.get("layer") == key and not x.get("dead")
                       for x in self._overlays):
                    continue
                nxt = self._queued[key].pop(0)
                if not self._queued[key]:
                    self._queued.pop(key, None)
                self._arm(nxt, now)
                self._overlays.append(nxt)
        if not ovs:
            return frame
        H, W = frame.shape[:2]
        def _z(o):
            src_ = o.get("item") if o.get("kind") == "draw" else o
            try:
                return float((src_ or {}).get("z") or o.get("z") or 0)
            except (TypeError, ValueError):
                return 0.0
        ovs.sort(key=_z)        # stable: equal z keeps fire order (newest on top)
        for o in ovs:
            if intro and o.get("always_on"):
                continue                       # pre-show: the scene stays down
            if o.get("start_at") and now < o["start_at"]:
                continue                       # staggered — not its turn yet
            # flash: N seconds visible, N hidden (0/blank = always visible)
            fl = o.get("flash") or (o.get("item") or {}).get("flash")
            if fl:
                try:
                    period = max(0.1, float(fl))
                    if int(now / period) % 2:
                        continue
                except (TypeError, ValueError):
                    pass
            if o.get("kind") == "draw":
                try:
                    it = o.get("item") or {}
                    if it.get("kind") == "timer" and it.get("autovanish"):
                        left = (self._get_state().get("timers") or {}).get(str(it.get("id")))
                        if left is not None and float(left) <= 0:
                            fo = it.get("fade_out")
                            if fo and not o.get("until"):
                                o["fade_out"] = fo
                                o["until"] = now + float(fo)
                            elif not fo:
                                o["dead"] = True
                                continue
                    sp = self._render_item(it, W, H)
                    if sp is None:
                        continue
                    sp = self._rotate(sp, it.get("rot"))
                    ih, iw = sp.shape[:2]
                    x, y = self._spot(o, W, H, iw, ih)
                    ax, ay = self._anim_offset(o, now, iw, ih)
                    self._blend(frame, sp, x + ax, y + ay, self._fade_alpha(o, now))
                except Exception:  # noqa: BLE001 — a bad item never kills the pipe
                    o["dead"] = True
                continue
            if o.get("kind") == "video":
                ok, vf = o["cap"].read()
                if not ok and o.get("loop"):
                    o["cap"].set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, vf = o["cap"].read()
                if not ok:
                    o["dead"] = True   # play-once finished (or file went bad)
                    continue
                img = cv2.cvtColor(vf, cv2.COLOR_BGR2BGRA)   # opaque layer
                if o.get("chroma_on"):
                    img = self._chroma(img, o)
            else:
                img = o["img"]
                if o.get("chroma_on"):
                    img = self._chroma(img, o)
            tw = max(8, int(W * o["scale"]))
            if o.get("h") is not None:      # stage-designer stretch (w x h)
                try:
                    th = max(8, int(H * float(o["h"])))
                except (TypeError, ValueError):
                    th = max(8, int(img.shape[0] * tw / max(1, img.shape[1])))
            else:
                th = max(8, int(img.shape[0] * tw / max(1, img.shape[1])))
                if th > H:
                    th = H
                    tw = max(8, int(img.shape[1] * th / max(1, img.shape[0])))
            ov = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
            ov = self._rotate(ov, o.get("rot"))
            th, tw = ov.shape[:2]
            x, y = self._spot(o, W, H, tw, th)
            ax, ay = self._anim_offset(o, now, tw, th)
            self._blend(frame, ov, x + ax, y + ay, self._fade_alpha(o, now))
        return frame

    def _spot(self, o: dict, W: int, H: int, w: int, h: int) -> tuple[int, int]:
        """Top-left pixel for an overlay: exact fractional x/y when the stage
        designer set one, else the named anchor."""
        # DEAD CENTRE: stays centred however long the text renders, which a
        # fixed top-left x/y cannot — [owner] and [intro_timer] change width
        # every time they resolve.
        if o.get("center") or (o.get("item") or {}).get("center"):
            return self._place(W, H, w, h, "center")
        if o.get("x") is not None and o.get("y") is not None:
            try:
                return (int(W * float(o["x"])), int(H * float(o["y"])))
            except (TypeError, ValueError):
                pass
        return self._place(W, H, w, h, o.get("pos") or "center")

    def _run(self):
        cap = None
        try:
            cap = cv2.VideoCapture(self._device)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._size[1])
            ok, frame = cap.read()
            if not ok or frame is None:
                self._err = f"couldn't open camera #{self._device}"
                return
            H, W = frame.shape[:2]
            H -= H % 2   # I420 needs even dimensions
            W -= W % 2
            # Browsers reject RGB from v4l2loopback (Chrome/Discord-web hang
            # then error 2014) — on Linux feed I420 like OBS does. Windows'
            # OBS driver takes RGB and serves NV12 to apps itself.
            i420 = sys.platform.startswith("linux")
            fmt = (pyvirtualcam.PixelFormat.I420 if i420
                   else pyvirtualcam.PixelFormat.RGB)
            with pyvirtualcam.Camera(width=W, height=H, fps=self._fps,
                                     fmt=fmt, print_fps=False) as cam:
                self._info = f"{cam.device} · {W}x{H} @ {self._fps}fps"
                while not self._stop.is_set():
                    if self._frozen and self._raw is not None:
                        frame = self._raw.copy()      # hold the last picture
                    else:
                        ok, frame = cap.read()
                        if not ok or frame is None:
                            self._err = "camera read failed (unplugged / in use?)"
                            break
                        self._raw = frame
                    frame = frame[:H, :W]
                    mode = self._gate_mode()
                    if self._mirror:
                        # The CAMERA IMAGE only, before anything is drawn on it.
                        # Flipping the finished frame took the overlays with it,
                        # so turning Mirror on to fix your own Discord tile made
                        # every viewer read your text backwards. A mirrored face
                        # is imperceptible; mirrored text is not.
                        frame = cv2.flip(frame, 1)
                    if self._black or mode != "live":
                        frame = np.zeros_like(frame)
                    if mode == "off":
                        # a real black screen — nothing painted on it at all,
                        # except a standby card so a viewer who picks the
                        # virtual camera before you go live sees a deliberate
                        # "STARTING SOON" instead of assuming it's broken
                        self._reap_only()
                        self._standby(frame)
                    else:
                        frame = self._composite(frame, intro=(mode == "intro"))
                    self._last = frame   # cap.read() hands out fresh arrays
                    cam.send(cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420) if i420
                             else cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    cam.sleep_until_next_frame()
        except Exception as e:  # noqa: BLE001 — surfaced via status()
            self._err = str(e)
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass
            self._info = ""
            self._last = None

    def preview_jpeg(self):
        """The latest composited frame as JPEG bytes — None when not running.
        What the panel's live preview polls; exactly what Discord viewers see."""
        f = self._last
        if f is None:
            return None
        try:
            ok, buf = cv2.imencode(".jpg", f, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            return buf.tobytes() if ok else None
        except Exception:  # noqa: BLE001
            return None
