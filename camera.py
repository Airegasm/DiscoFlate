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
        self.state_cb = None       # () -> {"capacity","firing","remaining"} for widgets
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
                "mirror": self._mirror, "overlays": len(self._overlays)}

    def set_mirror(self, on: bool) -> dict:
        """Flip the whole outgoing frame horizontally (live) — camera AND
        overlays. Exists to cancel Discord's un-disableable self-view mirror:
        ON = your own Discord tile reads correctly but viewers see everything
        mirrored; OFF (default) = viewers get the true image."""
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

    def clear_overlays(self, layer: str | None = None) -> dict:
        """Remove all overlays, or just the named layer."""
        key = (layer or "").strip().lower()
        with self._lock:
            for o in self._overlays:
                if not key or o.get("layer") == key:
                    o["dead"] = True
        return {"ok": True}

    def add_item(self, item: dict, seconds=None, layer: str | None = None) -> dict:
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
               "until": until, "pos": str(item.get("pos") or "center").lower(),
               "x": item.get("x"), "y": item.get("y"),
               "rot": item.get("rot"), "flash": item.get("flash")}
        with self._lock:
            if key:
                for o in self._overlays:
                    if o.get("layer") == key:
                        o["dead"] = True
            self._overlays.append(ent)
        return {"ok": True}

    def add_widget(self, widget: str, x=None, y=None, w=None,
                   layer: str | None = None) -> dict:
        """Back-compat shim for the plain gauge/timer call."""
        return self.add_item({"kind": str(widget or ""), "x": x, "y": y,
                              "w": w or 0.35}, layer=layer)

    def fire_overlay(self, media: str, seconds=5.0, pos: str = "center",
                     scale=0.5, mode: str = "timed", layer: str | None = None,
                     x=None, y=None, rot=None, flash=None, h=None) -> dict:
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
        with self._lock:
            if key:   # named slot: replace the previous holder
                for o in self._overlays:
                    if o.get("layer") == key:
                        o["dead"] = True
            self._overlays.append(entry)
        return {"ok": True, "overlays": len(self._overlays)}

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

    def _text_sprite(self, txt: str, item: dict, H: int):
        """An RGBA sprite of `txt` honouring color / size / font / bg."""
        txt = str(txt if txt is not None else "")
        if not txt:
            return None
        font = self._FONTS.get(str(item.get("font") or "sans").lower(), 0)
        try:
            size = max(0.01, min(0.9, float(item.get("size") or 0.06)))
        except (TypeError, ValueError):
            size = 0.06
        px = max(10, int(H * size))                 # target cap height in pixels
        scale = px / 22.0                           # Hershey units -> ~px
        thick = max(1, int(round(scale * 1.6)))
        (tw, th), base = cv2.getTextSize(txt, font, scale, thick)
        pad = max(4, int(px * 0.28))
        w, h = tw + pad * 2, th + base + pad * 2
        sp = np.zeros((h, w, 4), np.uint8)
        bg = str(item.get("bg") or "").strip()
        if bg:                                      # blank bg = transparent
            sp[:, :, :3] = self._bgr(bg, (0, 0, 0))
            sp[:, :, 3] = 255
        org = (pad, pad + th)
        col = self._bgr(item.get("color"), (255, 255, 255))
        if not bg:   # unbacked text gets a dark outline so it reads on any feed
            cv2.putText(sp, txt, org, font, scale, (0, 0, 0, 255),
                        thick + max(2, thick), cv2.LINE_AA)
        cv2.putText(sp, txt, org, font, scale, (*col, 255), thick, cv2.LINE_AA)
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
        fill = self._bgr(item.get("color"), None) or \
            (60, int(200 - 150 * frac), int(70 + 170 * frac))   # green -> red
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

    def _render_item(self, item: dict, W: int, H: int):
        """RGBA sprite for a non-media overlay, or None to draw nothing."""
        kind = str(item.get("kind") or "text")
        st = self._get_state()
        if kind == "capacity_gauge":
            try:
                pct = float(st.get("capacity") or 0)
            except (TypeError, ValueError):
                pct = 0.0
            return self._gauge_sprite(item, W, H, pct)
        if kind == "pump_timer":
            try:
                rem = float(st.get("remaining") or 0)
            except (TypeError, ValueError):
                rem = 0.0
            firing = bool(st.get("firing"))
            txt = (str(item.get("fmt_on") or "PUMP [secs]s").replace("[secs]", f"{rem:.0f}")
                   if firing else str(item.get("fmt_off") or "PUMP idle"))
            return self._text_sprite(txt, item, H)
        # plain text — [capacity] / [secs] stay live so a label can count
        txt = str(item.get("text") or "")
        if "[" in txt:
            try:
                txt = txt.replace("[capacity]", f"{float(st.get('capacity') or 0):.0f}")
                txt = txt.replace("[secs]", f"{float(st.get('remaining') or 0):.0f}")
            except (TypeError, ValueError):
                pass
        return self._text_sprite(txt, item, H)

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
    def _blend(frame, ov, x: int, y: int) -> None:
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
        roi[:] = (ov[:, :, :3].astype("float32") * a
                  + roi.astype("float32") * (1.0 - a)).astype("uint8")

    def _composite(self, frame):
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
        if not ovs:
            return frame
        H, W = frame.shape[:2]
        for o in ovs:
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
                    sp = self._render_item(o.get("item") or {}, W, H)
                    if sp is None:
                        continue
                    sp = self._rotate(sp, (o.get("item") or {}).get("rot"))
                    ih, iw = sp.shape[:2]
                    x, y = self._spot(o, W, H, iw, ih)
                    self._blend(frame, sp, x, y)
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
            else:
                img = o["img"]
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
            self._blend(frame, ov, x, y)
        return frame

    def _spot(self, o: dict, W: int, H: int, w: int, h: int) -> tuple[int, int]:
        """Top-left pixel for an overlay: exact fractional x/y when the stage
        designer set one, else the named anchor."""
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
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        self._err = "camera read failed (unplugged / in use?)"
                        break
                    frame = self._composite(frame[:H, :W])
                    if self._mirror:
                        # Flip the FINISHED frame (camera + overlays together).
                        # Discord mirrors your own self-view and offers no way
                        # to turn that off, so this exists to cancel it: ON =
                        # your Discord tile reads correctly, viewers see
                        # everything mirrored. OFF (default) = viewers correct.
                        frame = cv2.flip(frame, 1)
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
