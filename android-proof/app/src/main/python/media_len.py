"""How long a media file plays, and how long a scene group needs on screen.

An intro STAGE holds a scene group for N seconds. If that group contains a 12s
video, a 5s stage cuts it off mid-sentence — so the panel needs to know the
group's real length to stop you setting a stage shorter than its own content.

What can be measured, honestly:
  * video  — cv2 frame count / fps (cv2 is already a dependency)
  * .wav   — the `wave` module (stdlib)
  * other audio (mp3/ogg/m4a/flac/opus/aac) — only if tinytag or mutagen
    happens to be installed. Neither is a dependency, and Chaquopy's package
    repo is old, so on Android these normally come back UNKNOWN rather than
    wrong. An unknown never contributes to a floor; the panel says which files
    it couldn't read instead of quietly pretending they're zero.
"""
from __future__ import annotations

import os
import wave

VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v")
AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus", ".aac")

# path -> (mtime, size, seconds|None). Probing a video opens and decodes a
# header; the canvas asks on every render, so never do it twice for one file.
_CACHE: dict[str, tuple] = {}


def _probe(path: str) -> float | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        try:
            import cv2
            cap = cv2.VideoCapture(path)
            try:
                n = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
            finally:
                cap.release()
            if n > 0 and fps > 0:
                return round(n / fps, 2)
        except Exception:  # noqa: BLE001 — a bad file is UNKNOWN, never fatal
            pass
        return None
    if ext == ".wav":
        try:
            with wave.open(path, "rb") as f:
                fr = f.getframerate()
                if fr:
                    return round(f.getnframes() / float(fr), 2)
        except Exception:  # noqa: BLE001
            pass
        return None
    if ext in AUDIO_EXTS:
        for mod in ("tinytag", "mutagen"):
            try:
                if mod == "tinytag":
                    from tinytag import TinyTag
                    d = TinyTag.get(path).duration
                else:
                    import mutagen
                    m = mutagen.File(path)
                    d = getattr(getattr(m, "info", None), "length", None)
                if d:
                    return round(float(d), 2)
            except Exception:  # noqa: BLE001 — not installed, or can't read it
                continue
        return None
    return None        # a still image has no length of its own


def duration(path: str) -> float | None:
    """Seconds this file plays for, or None when we can't tell."""
    if not path or not os.path.isfile(path):
        return None
    try:
        stt = os.stat(path)
        key = (stt.st_mtime_ns, stt.st_size)
    except OSError:
        return None
    hit = _CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    secs = _probe(path)
    _CACHE[path] = (key, secs)
    return secs


def overlay_seconds(o: dict, images_dir: str) -> tuple[float, str | None]:
    """(how long this overlay occupies the screen, the file we couldn't read).

    An overlay that names an explicit `seconds` is taken at its word — that is
    the operator's decision. Otherwise a video or sound plays for its own
    length. A still image or a text card has no length: it sits there until
    something clears it, so it never forces a longer stage.
    """
    delay = 0.0
    try:
        delay = max(0.0, float(o.get("delay") or 0))
    except (TypeError, ValueError):
        delay = 0.0
    try:
        explicit = float(o.get("seconds") or 0)
    except (TypeError, ValueError):
        explicit = 0.0
    if explicit > 0:
        return delay + explicit, None
    name = os.path.basename(str(o.get("media") or "").strip())
    if not name:
        return delay, None
    ext = os.path.splitext(name)[1].lower()
    if ext not in VIDEO_EXTS and ext not in AUDIO_EXTS:
        return delay, None                      # a still image
    d = duration(os.path.join(images_dir, name))
    if d is None:
        return delay, name                      # unreadable — report it
    return delay + d, None


def group_seconds(cfg: dict, scene_name: str, group: str, images_dir: str) -> dict:
    """The shortest a stage can be without cutting this group's own content off.

    Returns {"seconds", "unknown": [filenames], "items": n}. `seconds` is the
    longest single item in the group — they all start together, so the group is
    done when its longest piece is.
    """
    g = str(group or "").strip().lower()
    out = {"seconds": 0.0, "unknown": [], "items": 0}
    if not g:
        return out
    want = str(scene_name or "").strip().lower()
    scn = next((s for s in (cfg.get("scenes") or [])
                if str(s.get("name") or "").strip().lower() == want), None)
    pool = list((scn or {}).get("overlays") or []) + list(cfg.get("scene_globals") or [])
    longest = 0.0
    for o in pool:
        if str(o.get("group") or "").strip().lower() != g:
            continue
        out["items"] += 1
        secs, bad = overlay_seconds(o, images_dir)
        if bad and bad not in out["unknown"]:
            out["unknown"].append(bad)
        longest = max(longest, secs)
    out["seconds"] = round(longest, 2)
    return out
