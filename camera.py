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

    # -- overlays --------------------------------------------------------------#
    def clear_overlays(self) -> dict:
        with self._lock:
            self._overlays.clear()
        return {"ok": True}

    def fire_overlay(self, image: str, seconds=5.0, pos: str = "center",
                     scale=0.5) -> dict:
        """Play an image (PNG w/ alpha welcome) over the camera for N seconds.
        `image` = a filename from data/images (or an absolute path). `pos` =
        center / top / bottom / left / right / top-left / top-right /
        bottom-left / bottom-right. `scale` = fraction of the frame WIDTH the
        overlay spans (0.05–1.0)."""
        why = self.available()
        if why:
            return {"ok": False, "error": why}
        name = str(image or "").strip()
        if not name:
            return {"ok": False, "error": "no overlay image set"}
        path = name if os.path.isabs(name) else os.path.join(self.images_dir,
                                                             os.path.basename(name))
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            return {"ok": False, "error": f"couldn't read image: {name}"}
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
        elif img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        try:
            secs = max(0.5, float(seconds or 5))
            sc = max(0.05, min(1.0, float(scale or 0.5)))
        except (TypeError, ValueError):
            secs, sc = 5.0, 0.5
        with self._lock:
            self._overlays.append({"img": img, "until": time.monotonic() + secs,
                                   "pos": str(pos or "center").lower(), "scale": sc})
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

    def _composite(self, frame):
        now = time.monotonic()
        with self._lock:
            self._overlays = [o for o in self._overlays if o["until"] > now]
            ovs = list(self._overlays)
        if not ovs:
            return frame
        H, W = frame.shape[:2]
        for o in ovs:
            img = o["img"]
            tw = max(8, int(W * o["scale"]))
            th = max(8, int(img.shape[0] * tw / max(1, img.shape[1])))
            if th > H:
                th = H
                tw = max(8, int(img.shape[1] * th / max(1, img.shape[0])))
            ov = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
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
            with pyvirtualcam.Camera(width=W, height=H, fps=self._fps,
                                     print_fps=False) as cam:
                self._info = f"{cam.device} · {W}x{H} @ {self._fps}fps"
                while not self._stop.is_set():
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        self._err = "camera read failed (unplugged / in use?)"
                        break
                    frame = self._composite(frame)
                    cam.send(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
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
