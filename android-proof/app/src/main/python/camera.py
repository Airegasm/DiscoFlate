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
    _CV_ERR = None
except Exception as e:  # noqa: BLE001 — missing/broken wheel
    cv2 = None
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
                "overlays": len(self._overlays)}

    def start(self, device=0, width=1280, height=720, fps=30) -> dict:
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

    def add_widget(self, widget: str, x=None, y=None, w=None,
                   layer: str | None = None) -> dict:
        """A live game widget layer (capacity_gauge / pump_timer), drawn each
        frame from state_cb. Named layers replace like media overlays."""
        key = (str(layer or "").strip().lower()) or None
        ent = {"kind": "widget", "widget": str(widget or ""), "layer": key,
               "dead": False, "until": None, "pos": "center",
               "scale": max(0.05, min(1.0, float(w or 0.35))),
               "x": x, "y": y}
        with self._lock:
            if key:
                for o in self._overlays:
                    if o.get("layer") == key:
                        o["dead"] = True
            self._overlays.append(ent)
        return {"ok": True}

    def fire_overlay(self, media: str, seconds=5.0, pos: str = "center",
                     scale=0.5, mode: str = "timed", layer: str | None = None,
                     x=None, y=None) -> dict:
        """Show a MEDIA layer over the camera. `media` = an image (PNG alpha
        welcome) or a video file from data/images (or an absolute path).
        mode: "timed"  = shown/looping for `seconds`
              "hold"   = stays until cleared or replaced (video loops)
              "once"   = a video plays through once, then removes itself
              "clear"  = remove the named layer (or ALL when no layer given)
        `layer` names the slot — firing the same layer name REPLACES it (scene
        building). pos/scale as before (anchor + fraction of frame width)."""
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

    def _draw_widget(self, frame, o, W: int, H: int) -> None:
        st = self._get_state()
        bw = max(60, int(W * float(o.get("scale") or 0.35)))
        if o.get("x") is not None and o.get("y") is not None:
            x, y = int(W * float(o["x"])), int(H * float(o["y"]))
        else:
            x, y = self._place(W, H, bw, int(bw * 0.14) + 4, o.get("pos") or "center")
        x = max(0, min(W - 20, x))
        y = max(0, min(H - 20, y))
        if o.get("widget") == "capacity_gauge":
            try:
                pct = float(st.get("capacity") or 0)
            except (TypeError, ValueError):
                pct = 0.0
            bh = min(max(16, int(bw * 0.11)), H - y - 2)
            bw = min(bw, W - x - 2)
            if bh <= 4 or bw <= 20:
                return
            frac = max(0.0, min(1.0, pct / 100.0))
            color = (60, int(200 - 150 * frac), int(70 + 170 * frac))   # green→red
            cv2.rectangle(frame, (x, y), (x + bw, y + bh), (30, 30, 30), -1)
            if frac > 0:
                cv2.rectangle(frame, (x, y), (x + int(bw * frac), y + bh), color, -1)
            cv2.rectangle(frame, (x, y), (x + bw, y + bh), (240, 240, 240), 2)
            fs = max(0.5, bh / 32.0)
            org = (x + 8, y + bh - max(4, int(bh * 0.25)))
            cv2.putText(frame, f"{pct:.0f}%", org, cv2.FONT_HERSHEY_SIMPLEX,
                        fs, (0, 0, 0), int(fs * 4) + 2, cv2.LINE_AA)
            cv2.putText(frame, f"{pct:.0f}%", org, cv2.FONT_HERSHEY_SIMPLEX,
                        fs, (255, 255, 255), max(1, int(fs * 2)), cv2.LINE_AA)
        else:   # pump_timer
            try:
                rem = float(st.get("remaining") or 0)
            except (TypeError, ValueError):
                rem = 0.0
            firing = bool(st.get("firing"))
            label = f"PUMP {rem:.0f}s" if firing else "PUMP idle"
            fs = max(0.6, bw / 260.0)
            (tw_, th_), _b = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX,
                                             fs, max(1, int(fs * 2)))
            pad = max(6, int(th_ * 0.5))
            x2 = min(W - 1, x + tw_ + pad * 2)
            y2 = min(H - 1, y + th_ + pad * 2)
            cv2.rectangle(frame, (x, y), (x2, y2), (25, 25, 25), -1)
            cv2.rectangle(frame, (x, y), (x2, y2),
                          (70, 200, 70) if firing else (110, 110, 110), 2)
            cv2.putText(frame, label, (x + pad, y + pad + th_),
                        cv2.FONT_HERSHEY_SIMPLEX, fs, (255, 255, 255),
                        max(1, int(fs * 2)), cv2.LINE_AA)

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
            if o.get("kind") == "widget":
                try:
                    self._draw_widget(frame, o, W, H)
                except Exception:  # noqa: BLE001 — a bad widget never kills the pipe
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
            th = max(8, int(img.shape[0] * tw / max(1, img.shape[1])))
            if th > H:
                th = H
                tw = max(8, int(img.shape[1] * th / max(1, img.shape[0])))
            ov = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
            if o.get("x") is not None and o.get("y") is not None:
                try:   # stage-designed spot: exact fractional coordinates
                    x = max(0, min(W - 8, int(W * float(o["x"]))))
                    y = max(0, min(H - 8, int(H * float(o["y"]))))
                except (TypeError, ValueError):
                    x, y = self._place(W, H, tw, th, o["pos"])
            else:
                x, y = self._place(W, H, tw, th, o["pos"])
            th = min(th, H - y)
            tw = min(tw, W - x)
            if th <= 0 or tw <= 0:
                continue
            ov = ov[:th, :tw]
            roi = frame[y:y + th, x:x + tw]
            a = ov[:, :, 3:4].astype("float32") / 255.0
            roi[:] = (ov[:, :, :3].astype("float32") * a
                      + roi.astype("float32") * (1.0 - a)).astype("uint8")
        return frame

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
