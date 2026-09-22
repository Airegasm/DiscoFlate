"""
app.py — DiscoFlate entry point.

Runs, in one asyncio process:
  * the capacity/dice Engine (engine.py)
  * a loopback-only web UI + JSON API (aiohttp)
  * the Discord listener (discord_bot.py)

Start:  python3 app.py     then open http://127.0.0.1:8765
"""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import io
import ipaddress
import json
import os
import zipfile
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import uuid

import aiohttp
from aiohttp import web

import camera
import config_store
import stage
import pumpdirect_import
import kasa_legacy as kasa
import device_control
from engine import Engine
from discord_bot import BotManager

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.environ.get("DISCOFLATE_WEB_DIR") or os.path.join(HERE, "web")
IMAGES_DIR = os.path.join(config_store.DATA_DIR, "images")
# Shipped default game config (fresh-install seed). On Android the host copies
# the bundled seed to a pristine path and points this env at it.
DEFAULT_CONFIG_PATH = os.environ.get("DISCOFLATE_DEFAULT_CONFIG") or os.path.join(HERE, "default_config.json")
# The immutable built-in Gameplay Preset (shipped starter setup — "Basic
# Session"). Appears in the preset list; can be loaded but never overwritten.
DEFAULT_PRESET_PATH = os.path.join(HERE, "default_preset.json")
BUILTIN_PRESET_NAME = "Defaults (built-in)"
PORT = int(os.getenv("DISCOFLATE_PORT", "8765"))


def _builtin_preset_data() -> dict:
    """The shipped default preset's gameplay data (empty dict if unavailable)."""
    try:
        with open(DEFAULT_PRESET_PATH, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return {k: v for k, v in d.items() if k in _GAMEPLAY_KEYS}
    except (FileNotFoundError, ValueError, OSError):
        return {}
HOST = "127.0.0.1"

# App version — single-sourced from version.json (android build.gradle.kts and
# the GitHub release tag are the only other places that carry it).
try:
    with open(os.path.join(HERE, "version.json"), "r", encoding="utf-8") as _vf:
        _v = json.load(_vf)
    VERSION = str(_v.get("version") or "0.0.0")
    VERSION_CODE = int(_v.get("versionCode") or 0)
except (OSError, ValueError):
    VERSION, VERSION_CODE = "0.0.0", 0
VERSION_URL = "https://raw.githubusercontent.com/Airegasm/DiscoFlate/main/version.json"

# Scalar/message keys that "Restore Default Config" RESETS to the shipped default.
# (Commands and system-command NAMES are handled additively below so your edits
# and customs are never clobbered.) Connection/personal keys are never touched.
_DEFAULT_SCALAR_KEYS = ["roll", "capacity_message", "pumptimer_message",
                        "pump_message", "cooldown_message",
                        "system_buffer_seconds", "cooldown_seconds",
                        "auto_report", "listener_message_on", "listener_message_off",
                        "pause_message", "resume_message", "paused_notice_message",
                        "always_on_enabled"]
# list key -> identity function. Restore KEEPS everything you already have (edited
# defaults + customs) and only ADDS shipped items whose key is missing.
_DEFAULT_LIST_KEYS = {
    "commands": lambda c: (c.get("name") or "").strip().lower(),
    "prizes": lambda p: ((p.get("name") if isinstance(p, dict) else "") or "").strip().lower(),
    "owner_commands": lambda o: (o.get("name") or "").strip().lower(),
    "chat_buttons": lambda bt: (bt.get("label") or "").strip().lower(),
    "modes": lambda m: (m.get("name") or "").strip().lower(),
    "events": lambda e: (e.get("name") or "").strip().lower(),
    "capacity_events": lambda e: ((e.get("name") or "").strip() or str(e.get("at") or "")).lower(),
    "polls": lambda p: (p.get("name") or "").strip().lower(),
    "competitions": lambda c: (c.get("name") or "").strip().lower(),
    "bonus_rounds": lambda b: (b.get("name") or "").strip().lower(),
    "capacity_ranges": lambda r: f"{r.get('min')}-{r.get('max')}",
    "always_on_commands": lambda a: (a.get("name") if isinstance(a, dict) else str(a) or "").strip().lower(),
}


# ── Shareable "gameplay settings": EVERYTHING on the Game, Commands, Events,
# and Templates tabs — the safe data people can trade. Deliberately EXCLUDES
# everything personal/connection: discord_token, devices, vendors (creds),
# listen targets / server IDs / announce channel, allow-lists, cooldown-exempt
# names/IDs, operator name, mock/pumpdirect/runtime state.
# The gameplay key set lives in config_store now, because the store is what
# lays a scene's block over the config. One definition, both sides.
_GAMEPLAY_KEYS = config_store.GAMEPLAY_KEYS
# List keys → identity fn, for "add missing only" additive merge (by name/key).
_GAMEPLAY_LIST_KEYS = {
    "commands": lambda c: (c.get("name") or "").strip().lower(),
    "broadcasts": lambda b: (b.get("name") or "").strip().lower(),
    "modes": lambda m: (m.get("name") or "").strip().lower(),
    "prizes": lambda p: ((p.get("name") if isinstance(p, dict) else "") or "").strip().lower(),
    "owner_commands": lambda o: (o.get("name") or "").strip().lower(),
    "chat_buttons": lambda bt: (bt.get("label") or "").strip().lower(),
    "events": lambda e: (e.get("name") or "").strip().lower(),
    "capacity_events": lambda e: ((e.get("name") or "").strip() or str(e.get("at") or "")).lower(),
    "polls": lambda p: (p.get("name") or "").strip().lower(),
    "competitions": lambda c: (c.get("name") or "").strip().lower(),
    "bonus_rounds": lambda b: (b.get("name") or "").strip().lower(),
    "capacity_ranges": lambda r: f"{r.get('min')}-{r.get('max')}",
    "always_on_commands": lambda a: (a.get("name") if isinstance(a, dict) else str(a) or "").strip().lower(),
}


def _gp_blank(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _gameplay_export(cfg: dict) -> dict:
    """The gameplay actually in play — the live scene's block, since that is
    what the operator sees on the tabs and what a preset should capture."""
    r = config_store.resolved(cfg)
    return {k: r[k] for k in _GAMEPLAY_KEYS if k in r}


def _gameplay_merge(cur: dict, incoming: dict, mode: str) -> dict:
    """Fold shared gameplay settings into the live config. `incoming` is
    filtered to the safe keys first (a tampered file can't smuggle a token,
    devices, or creds). mode 'replace' overwrites those keys; 'add' keeps
    everything you have and only adds missing list items / fills blank fields."""
    inc = {k: v for k, v in (incoming or {}).items() if k in _GAMEPLAY_KEYS}
    out = dict(cur)
    if mode == "replace":
        out.update(inc)
        return out
    # ---- add missing / blank only ----
    for k, keyfn in _GAMEPLAY_LIST_KEYS.items():
        if k not in inc:
            continue
        current = list(cur.get(k) or [])
        have = {keyfn(x) for x in current}
        current += [x for x in (inc[k] or []) if keyfn(x) not in have]
        out[k] = current
    if "templates" in inc:
        t = dict(cur.get("templates") or {})
        # merge every template kind present in either side (ranges key by band,
        # everything else by name) so new kinds — polls, competitions, capevents —
        # merge without a code change here.
        for sub in set(t) | set(inc["templates"] or {}):
            cl = list(t.get(sub) or [])
            kf = (lambda x: f"{x.get('min')}-{x.get('max')}") if sub == "ranges" \
                else (lambda x: (x.get("name") or "").strip().lower())
            have = {kf(x) for x in cl}
            cl += [x for x in ((inc["templates"] or {}).get(sub) or []) if kf(x) not in have]
            t[sub] = cl
        out["templates"] = t
    handled = set(_GAMEPLAY_LIST_KEYS) | {"templates"}
    for k in _GAMEPLAY_KEYS:
        if k in handled or k not in inc:
            continue
        cv = cur.get(k)
        if _gp_blank(cv):
            out[k] = inc[k]
        elif isinstance(cv, dict) and isinstance(inc[k], dict):
            merged = dict(cv)   # fill only blank/missing leaves (command_names, roll, auto_report, …)
            for sk, sv in inc[k].items():
                if _gp_blank(merged.get(sk)):
                    merged[sk] = sv
            out[k] = merged
    return out


def _merge_defaults(cur: dict, dflt: dict) -> dict:
    """Additively top up the config with shipped defaults: reset the scalar/message
    keys, KEEP every command/range/event/etc. you already have (an edited default or
    a custom with the same name is never overwritten), and ADD only the shipped
    items you're missing. Connection/personal keys (not listed) are untouched."""
    out = dict(cur)
    for k in _DEFAULT_SCALAR_KEYS:
        if k in dflt:
            out[k] = dflt[k]
    # system command names: keep your renames, add any NEW built-in names.
    out["command_names"] = {**(dflt.get("command_names") or {}), **(cur.get("command_names") or {})}
    for k, keyfn in _DEFAULT_LIST_KEYS.items():
        current = list(cur.get(k) or [])
        have = {keyfn(x) for x in current}
        missing = [x for x in (dflt.get(k) or []) if keyfn(x) not in have]
        merged = current + missing
        if k == "capacity_ranges":
            merged.sort(key=lambda r: (r.get("min", 0), r.get("max", 0)))
        out[k] = merged
    return out


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
# Vendor credential fields that are NOT secrets — their saved value is shown
# in the UI (a region picker / base URL is useless as a filled/not-filled bool).
_VENDOR_PUBLIC_FIELDS = {"tuya": {"region"}, "homeassistant": {"baseUrl"}}


def _mask_vendors(vendors: dict) -> dict:
    """Which credential fields are filled, per vendor — never the secret
    values (GET /api/state used to ship every cloud password to the page; a
    DNS-rebinding page or any local process could read them). Non-secret
    fields (_VENDOR_PUBLIC_FIELDS) pass their value through for the UI."""
    out = {}
    for v, creds in (vendors or {}).items():
        pub = _VENDOR_PUBLIC_FIELDS.get(v, set())
        out[v] = {f: (str(val or "") if f in pub else bool(str(val or "").strip()))
                  for f, val in (creds or {}).items()}
    return out


def _public_state(engine: Engine, botmgr: BotManager) -> dict:
    # RESOLVED: the panel must show the rules the live scene is actually
    # playing by, not the stale top-level copy they were migrated from.
    # Non-gameplay keys (scenes, devices, channels) pass through untouched.
    raw = config_store.load()
    cfg = config_store.resolved(raw)
    snap = engine.snapshot()
    return {
        **snap,
        "prefix": cfg.get("command_prefix", "!"),
        "command_names": cfg.get("command_names", {}),
        "capacity_message": cfg.get("capacity_message", ""),
        "capacity_embed": bool(cfg.get("capacity_embed")), "capacity_title": cfg.get("capacity_title", ""),
        "pumptimer_embed": bool(cfg.get("pumptimer_embed")), "pumptimer_title": cfg.get("pumptimer_title", ""),
        "cooldown_embed": bool(cfg.get("cooldown_embed")), "cooldown_title": cfg.get("cooldown_title", ""),
        "pump_embed": bool(cfg.get("pump_embed")), "pump_title": cfg.get("pump_title", ""),
        "pause_embed": bool(cfg.get("pause_embed")), "pause_title": cfg.get("pause_title", ""),
        "pumptimer_message": cfg.get("pumptimer_message", ""),
        "pump_message": cfg.get("pump_message", ""),
        "system_buffer_seconds": cfg.get("system_buffer_seconds", 8),
        "cooldown_message": cfg.get("cooldown_message", ""),
        "roll": cfg.get("roll", {}),
        "prizes": cfg.get("prizes", []),
        "owner_commands": cfg.get("owner_commands", []),
        "chat_buttons": cfg.get("chat_buttons", []),
        "capacity_ranges": cfg.get("capacity_ranges", []),
        "commands": cfg.get("commands", []),
        "always_on_enabled": cfg.get("always_on_enabled", False),
        "always_on_commands": cfg.get("always_on_commands", []),
        "modes": cfg.get("modes", []),
        "events": cfg.get("events", []),
        "capacity_events": cfg.get("capacity_events", []),
        "polls": cfg.get("polls", []),
        "competitions": cfg.get("competitions", []),
        "competition_active": engine.competition_active(),
        "event_in_process_message": cfg.get("event_in_process_message", ""),
        "event_cooldown_message": cfg.get("event_cooldown_message", ""),
        "broadcasts": cfg.get("broadcasts", []),
        "devices": cfg.get("devices", []),
        "active_device_id": cfg.get("active_device_id"),
        "vendors_set": _mask_vendors(cfg.get("vendors", {})),
        "allow": cfg.get("allow", {}),
        "listen_guild_id": cfg.get("listen_guild_id", ""),
        "listen_channel_id": cfg.get("listen_channel_id", ""),
        "listen_targets": cfg.get("listen_targets", []),
        "anon_user_label": cfg.get("anon_user_label", ""),
        "output_headers": cfg.get("output_headers", False),
        "rich_output": cfg.get("rich_output", False),
        "templates": cfg.get("templates", {"commands": [], "events": [], "ranges": []}),
        "allow_dms": cfg.get("allow_dms", False),
        "server_channels": cfg.get("server_channels", {}),
        "invite_url": botmgr.invite_url(),
        "invite_url_min": botmgr.invite_url(minimal=True),
        "cooldown_seconds": cfg.get("cooldown_seconds", 0),
        "cooldown_exempt_user_ids": cfg.get("cooldown_exempt_user_ids", []),
        "cooldown_exempt_names": cfg.get("cooldown_exempt_names", []),
        "operator_name": cfg.get("operator_name", ""),
        "listener_message_on": cfg.get("listener_message_on", ""),
        "listener_on_embed": bool(cfg.get("listener_on_embed")),
        "listener_on_title": cfg.get("listener_on_title", ""),
        "listener_off_embed": bool(cfg.get("listener_off_embed")),
        "listener_off_title": cfg.get("listener_off_title", ""),
        "listener_message_off": cfg.get("listener_message_off", ""),
        "pause_message": cfg.get("pause_message", ""),
        "resume_message": cfg.get("resume_message", ""),
        "paused_notice_message": cfg.get("paused_notice_message", ""),
        "auto_report": cfg.get("auto_report", {}),
        "pumpdirect_path": cfg.get("pumpdirect_path"),
        "has_token": bool(cfg.get("discord_token")),
        "bot_error": botmgr.last_error,
        "config_rev": cfg.get("config_rev", 0),
        "recovered_config": config_store.RECOVERED_FROM,
        "version": VERSION,
        # preset NAMES only (the full data would bloat the 1s state poll). The
        # immutable built-in "Defaults" preset is always listed first.
        "preset_loaded": cfg.get("preset_loaded", ""),
        "notify_overlay": engine.session_overlay("notify_overlay"),
        # the RESOLVED show, not the frozen top-level block: a scene that has
        # its own wins, one that doesn't still falls back. The UI reads the
        # scene first and only uses these as the fallback, so they must agree.
        "pause_overlay": engine.session_overlay("pause_overlay"),
        "bonus_rounds": cfg.get("bonus_rounds") or [],
        "scenes": raw.get("scenes") or [],
        # which keys the SCENE owns — the panel needs this to tell a scene-scoped
        # edit from a global one, and one definition beats two that drift
        "gameplay_keys": list(config_store.GAMEPLAY_KEYS),
        "minigames": cfg.get("minigames") or [],
        "scene_globals": cfg.get("scene_globals") or [],
        "scene_globals_meta": cfg.get("scene_globals_meta") or {},
        "chat_scene": cfg.get("chat_scene", ""),
        "vcam_mirror": bool(cfg.get("vcam_mirror", True)),
        "standby_text": cfg.get("standby_text", "STARTING SOON"),
        "vcam_device": cfg.get("vcam_device", 0),
        "vcam_size": cfg.get("vcam_size", "1280x720"),
        "golive": engine.golive(),
        "chat_isolate": bool(cfg.get("chat_isolate")),
        "chat_isolate_channel": cfg.get("chat_isolate_channel", ""),
        "gameplay_presets": ([{"name": BUILTIN_PRESET_NAME, "builtin": True}]
                             + [{"name": p.get("name", "")} for p in (cfg.get("gameplay_presets") or [])]),
        "remote_access": cfg.get("remote_access", {"enabled": False, "allowed_ips": []}),
        "lan_ips": _lan_ips(),
        "port": PORT,
        "silence_onoff_log": cfg.get("silence_onoff_log", False),
        "mock_mode": cfg.get("mock_mode", False),
        "mock_calibration_seconds_to_100": cfg.get("mock_calibration_seconds_to_100", 60),
    }


async def _json(request: web.Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


# ---- remote access (System tab) — same model as SwellDreams: client-IP
# whitelist, loopback always allowed, empty list fails closed ------------------
def _clean_ip(ip: str | None) -> str:
    """Normalize a peer address (strip the IPv4-mapped ::ffff: prefix)."""
    return str(ip or "").strip().removeprefix("::ffff:")


def _is_loopback(ip: str | None) -> bool:
    a = _clean_ip(ip)
    if a in ("::1", "localhost"):
        return True
    return a.startswith("127.")


def _ip_whitelisted(ip: str | None, cfg: dict) -> bool:
    """True if this client IP may talk to the server. Loopback always may.
    Remote clients need remote_access.enabled AND a whitelist match — exact IP,
    wildcard pattern (192.168.1.*), or CIDR (192.168.1.0/24)."""
    a = _clean_ip(ip)
    if _is_loopback(a):
        return True
    ra = cfg.get("remote_access") or {}
    if not ra.get("enabled"):
        return False
    for entry in (ra.get("allowed_ips") or []):
        e = str(entry or "").strip()
        if not e:
            continue
        if a == e:
            return True
        if "/" in e:
            try:
                if ipaddress.ip_address(a) in ipaddress.ip_network(e, strict=False):
                    return True
            except ValueError:
                continue
        elif "*" in e or "?" in e:
            if fnmatch.fnmatch(a, e):
                return True
    return False


def _valid_ip_entry(e: str) -> bool:
    """A whitelist entry must be an IP, a CIDR block, or a *-wildcard pattern."""
    e = (e or "").strip()
    if not e:
        return False
    if "/" in e:
        try:
            ipaddress.ip_network(e, strict=False)
            return True
        except ValueError:
            return False
    if "*" in e or "?" in e:
        # crude shape check: dotted quads with wildcards, e.g. 192.168.1.*
        return all(p == "*" or p == "?" or p.isdigit() for p in e.replace("?", "*").split("."))
    try:
        ipaddress.ip_address(e)
        return True
    except ValueError:
        return False


def _host_ok(host: str) -> bool:
    """Host-header pinning that still allows LAN clients: localhost forms, or
    any IP-LITERAL host (DNS rebinding needs a domain name, so requiring a
    literal kills it while http://192.168.x.y:8765 keeps working)."""
    h = (host or "").lower()
    if h.startswith("[") and "]" in h:          # [ipv6]:port
        h = h[1:h.index("]")]
    elif h.count(":") == 1:                     # host:port
        h = h.split(":", 1)[0]
    if h in ("localhost", ""):
        return bool(h)
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def _lan_ips() -> list[str]:
    """This machine's non-loopback IPs (for the 'open this on your phone' hint)."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))          # no traffic sent — just routes
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def _origin_ok(request: web.Request) -> bool:
    # Reject cross-origin POSTs so a malicious page in a browser can't drive
    # the API via CSRF. Same-origin is judged against the Host actually used,
    # so whitelisted LAN clients (http://<lan-ip>:8765) pass too.
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    if origin in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
        return True
    return origin == f"http://{request.headers.get('Host', '')}"


# Endpoints that reveal or replace secrets: beyond the origin check they need
# the per-install browser cookie, so another local OS user can't just curl them.
SENSITIVE_PATHS = {"/api/config/export", "/api/token", "/api/token/reveal",
                   "/api/config/import", "/api/pull-updates", "/api/repair-repo"}


def _web_secret() -> str:
    """Per-install secret handed to the browser as a cookie when the UI loads.
    Stored 0600 next to the config, so only this OS user can read it."""
    path = os.path.join(config_store.DATA_DIR, ".web-secret")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            s = fh.read().strip()
        if s:
            return s
    except OSError:
        pass
    s = uuid.uuid4().hex + uuid.uuid4().hex
    os.makedirs(config_store.DATA_DIR, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(s)
    return s


# --------------------------------------------------------------------------- #
# route factory
# --------------------------------------------------------------------------- #
def build_app(engine: Engine, botmgr: BotManager, net: dict | None = None) -> web.Application:
    secret = _web_secret()
    net = net if net is not None else {}
    vcam = camera.VirtualCam(IMAGES_DIR)   # the Chat tab's OBS-style overlay pipe
    vcam.set_mirror(config_store.load().get("vcam_mirror", False))
    # overlay text keeps re-rendering its placeholders while it's on screen
    vcam.render_cb = lambda t: engine.render(t)

    def _picture_gate() -> str:
        """What the camera is allowed to send, asked fresh every frame:
          'off'   — session isn't LIVE: a genuinely black screen, no overlays
                    at all, so you can start the camera and get set up first
          'intro' — LIVE with the pre-show running: black picture, and only
                    what the intro plays; the scene's always-on overlays wait
          'live'  — the real picture and every overlay
        """
        cfg0 = engine.cfg or {}
        if not cfg0.get("listener_enabled"):
            # keep the standby line in step while we're here — it is a global
            # camera setting, the same whichever scene happens to be selected
            want = cfg0.get("standby_text", "STARTING SOON")
            if want != getattr(vcam, "_standby_text", None):
                vcam.set_standby(want)
            return "off"
        # 'pending' matters: LIVE flips on BEFORE start_intro runs, and the ON
        # message + its [!command]s post to Discord in between. Without this
        # the room went out for those seconds, every single go-live.
        if ((engine.intro_active() or engine.intro_pending())
                and engine.golive().get("blackout", True)):
            return "intro"
        return "live"
    vcam.gate_cb = _picture_gate
    net["vcam"] = vcam
    # live game state for stage widgets (capacity gauge / pump timer);
    # camera.py throttles how often it calls this
    def _timer_map() -> dict:
        return {oid: round(_timer_value(oid, t.get("seconds")), 1)
                for oid, t in _timers.items()}

    def _vcam_state():
        s = engine.snapshot()
        return {"capacity": s.get("capacity"), "firing": s.get("firing"),
                "remaining": s.get("remaining"),
                "device_timers": s.get("device_timers") or [],
                "poll": s.get("poll"),
                "timers": _timer_map()}
    vcam.state_cb = _vcam_state
    stg = stage.Stage(IMAGES_DIR)   # /stage overlay registry (phone screen-share path)
    net["stage"] = stg

    # ---- Timer overlays: countdowns you start/stop from action blocks ------
    # One registry, read by BOTH surfaces (virtual cam + /stage) so a timer
    # reads the same everywhere. Keyed by overlay id.
    _timers: dict = {}

    def _timer_value(oid: str, configured) -> float:
        """Seconds left on a timer overlay: counting down while running,
        frozen when stopped, and its configured length before it ever ran."""
        t = _timers.get(str(oid))
        try:
            base = float(configured or 0)
        except (TypeError, ValueError):
            base = 0.0
        if not t:
            return base
        if t.get("running"):
            return max(0.0, t["ends_at"] - time.monotonic())
        return max(0.0, float(t.get("remaining", base)))

    def _timer_op(oid: str, op: str, seconds=None, configured=None) -> dict:
        oid = str(oid or "")
        if not oid:
            return {"ok": True}
        try:
            secs = float(seconds or 0) or float(configured or 0)
        except (TypeError, ValueError):
            secs = 0.0
        t = _timers.get(oid)
        if op == "start":
            # restarting from a paused timer resumes; otherwise a fresh run
            left = (float(t.get("remaining", 0)) if t and not t.get("running")
                    and float(t.get("remaining", 0)) > 0 else secs)
            if seconds:            # an explicit duration always wins
                left = secs
            _timers[oid] = {"running": True, "ends_at": time.monotonic() + max(0.0, left),
                            "remaining": left, "seconds": secs}
        elif op == "stop":
            if t and t.get("running"):
                t["remaining"] = max(0.0, t["ends_at"] - time.monotonic())
                t["running"] = False
            elif not t:
                _timers[oid] = {"running": False, "remaining": secs, "seconds": secs}
        elif op == "reset":
            _timers.pop(oid, None)
        return {"ok": True}

    def _bake(text, ctx):
        """Render [user]/[mention]/[result]… into overlay text ONCE, at fire
        time, so a command's actor shows up on the overlay. [capacity] and
        [secs] are left alone — those stay live, re-rendered every frame."""
        text = str(text or "")
        if "[" not in text:
            return text
        keep = {}
        for i, tok in enumerate(("capacity", "secs")):
            ph = f"\x00{i}\x00"
            if f"[{tok}]" in text:
                keep[ph] = f"[{tok}]"
                text = text.replace(f"[{tok}]", ph)
        try:
            text = engine.render(text, dict(ctx or {}))
        except Exception:  # noqa: BLE001 — bad token must never break an overlay
            pass
        for ph, orig in keep.items():
            text = text.replace(ph, orig)
        return text

    # `overlay` action rows + /api/overlay/fire → every live surface: the
    # virtual camera when it's running (desktop) AND the /stage registry
    # (always recorded, so a Stage page shows the current scene the moment it
    # opens). Ok when either surface is actually watched; quiet skip otherwise.
    def _group_hidden(scn, group) -> bool:
        """A group switched OFF on the Scenes tab. It means "not on the stream
        right now", so it doesn't come up when the scene loads — but firing it
        explicitly still works, because that's an explicit instruction."""
        g = str(group or "").strip().lower()
        if not g:
            return False
        return any(str(x).strip().lower() == g
                   for x in ((scn or {}).get("hidden_groups") or []))

    def _group_is_intro(scn, group) -> bool:
        """A group flagged 🎬 INTRO on the Scenes tab: it belongs to the
        pre-show and nothing else."""
        g = str(group or "").strip().lower()
        if not g:
            return False
        return any(str(x).strip().lower() == g
                   for x in ((scn or {}).get("intro_groups") or []))

    _ON_TOP_BASE = 1000   # "on top": above any z you'd set by hand

    def _group_is_pause(cfg0, group) -> bool:
        """The group picked as the PAUSE overlay. The session drives it, so it
        must never come up with the scene — only when you actually pause."""
        g = str(group or "").strip().lower()
        return bool(g) and g == engine.session_overlay("pause_overlay").strip().lower()

    def _intro_groups_allowed(cfg0) -> bool:
        """Intro groups only exist while Go Live is set to open with an intro.
        With that unticked they never mount and never play — which is what
        lets intro cards be ordinary always-on overlays."""
        return bool(engine.golive().get("intro_enabled"))

    def _scene_group(cfg0, scene_name, group):
        """Every overlay tagged with this group name, in the linked SCENE then
        the globals — the batch a scene_group action fires or kills. A HIDDEN
        group returns nothing, so firing it is a quiet no-op."""
        g = str(group or "").strip().lower()
        if not g:
            return []
        scn = _find_scene(cfg0, scene_name)
        if _group_is_intro(scn, g) and not _intro_groups_allowed(cfg0):
            return []      # intro group, but Go Live isn't opening with one
        pool = (list((scn or {}).get("overlays") or [])
                + list(cfg0.get("scene_globals") or []))
        # The NOTIFY overlay is driven only by a command's notification — it is
        # never part of a group play, or the after-group at the end of the
        # intro would paint its placeholder text on the stream for no reason.
        skip = engine.session_overlay("notify_overlay").strip()
        return [o for o in pool
                if str(o.get("group") or "").strip().lower() == g
                and not (skip and str(o.get("id") or "") == skip)]

    def _overlay_action(spec: dict) -> dict:
        mode = spec.get("mode") or "timed"   # may be refined per-item below
        # A SCENE GROUP: fire or kill a whole named set at once. Empty group =
        # quiet no-op, same rule as a missing overlay id.
        if spec.get("group"):
            cfg0 = config_store.load()
            scene_name = (spec.get("stage") or "").strip() or cfg0.get("chat_scene", "")
            if not scene_name:      # nothing linked to Chat: search every scene
                for s_ in (cfg0.get("scenes") or []):
                    if _scene_group(cfg0, s_.get("name"), spec.get("group")):
                        scene_name = s_.get("name")
                        break
            # Scene groups are SCOPED TO THE LOADED SCENE (plus the globals).
            # We deliberately do NOT hunt other scenes for a matching name —
            # two scenes may reuse "intro" for completely different looks.
            items = _scene_group(cfg0, scene_name, spec.get("group"))
            if not items:
                scn0 = _find_scene(cfg0, scene_name)
                if _group_is_intro(scn0, spec.get("group")) and not _intro_groups_allowed(cfg0):
                    why = "is an INTRO group and Go Live isn't opening with an intro"
                else:
                    why = "is empty"
                return {"ok": True, "skipped": f"scene group '{spec.get('group')}' {why}"}
            for it in items:
                sub = {k: v for k, v in spec.items() if k != "group"}
                sub["on_top"] = spec.get("on_top")
                sub["id"] = it.get("id")
                sub["stage"] = scene_name
                if mode != "clear" and not spec.get("mode"):
                    sub.pop("mode", None)      # let each item keep its own mode
                try:
                    _overlay_action(sub)
                except Exception:  # noqa: BLE001 — one bad item can't kill the batch
                    pass
            return {"ok": True, "group": spec.get("group"), "count": len(items)}
        # Action rows CALL overlays designed on the Stages tab: resolve the id
        # against the stage the Chat tab is linked to (or the named one), then
        # the globals. An unknown id is a quiet no-op, by design.
        oid = spec.get("id")
        if oid:
            cfg0 = config_store.load()
            scene_name = (spec.get("stage") or "").strip() or cfg0.get("chat_scene", "")
            found = _scene_item(cfg0, scene_name, oid)
            if found is None and not scene_name:   # not linked? search every stage
                for s_ in (cfg0.get("scenes") or []):
                    found = _scene_item(cfg0, s_.get("name"), oid)
                    if found is not None:
                        break
            if found is None:
                return {"ok": True, "skipped": f"overlay {oid} not in any scene"}
            scn_ = _find_scene(cfg0, scene_name)
            if _group_is_intro(scn_, found.get("group")) and not _intro_groups_allowed(cfg0):
                return {"ok": True,
                        "skipped": f"group '{found.get('group')}' is an intro group "
                                   f"and Go Live isn't set to open with an intro"}
            if (found.get("kind") or "") == "audio":
                # A sound cue: nothing is composited, it just plays here. Fires
                # from a group like any other overlay, so an intro can open with
                # a sting without a second mechanism.
                if spec.get("mode") == "clear":
                    return {"ok": True, "skipped": "audio cues can't be cleared"}
                return _play_audio(found.get("media"), found.get("volume"))
            lay = found.get("layer") or f"itm-{found.get('id')}"
            if spec.get("mode") == "update":      # update_overlay_text
                txt = _bake(spec.get("text"), spec.get("ctx"))
                fields = {"text": txt}
                if (found.get("kind") or "") in ("pump_timer", "device_timers"):
                    fields = {"fmt_on": txt, "fmt_off": txt}
                vcam.update_item(found.get("id"), fields)
                stg.update_item(found.get("id"), fields)
                return {"ok": True}
            if spec.get("mode") == "timer":       # start_timer / stop_timer
                _timer_op(found.get("id"), spec.get("timer") or "start",
                          seconds=spec.get("seconds"), configured=found.get("seconds"))
                return {"ok": True}
            # An ALWAYS-ON overlay holds until something clears it — its
            # mode/seconds fields are meaningless (the panel doesn't even show
            # them), so firing one as a 5-second timed layer was just wrong.
            if not spec.get("mode"):
                mode = "hold" if found.get("visible") else (found.get("mode") or "timed")
            if spec.get("seconds") in (None, "", 0):
                spec = {**spec, "seconds": found.get("seconds")}
            spec = {**spec, "mode": mode, "layer": lay}
            if mode == "clear":
                vcam.clear_overlays(lay, fade_out=spec.get("fade_out"))
                stg.clear(lay)
                return {"ok": True}
            if spec.get("on_top"):
                # Keep the group's own internal order, just move the whole set
                # above everything else on screen.
                found = {**found, "z": _ON_TOP_BASE + int(found.get("z") or 0)}
            if (found.get("kind") or "media") != "media":
                item_ = {**found,
                         "fade_in": spec.get("fade_in") or found.get("fade_in"),
                         "fade_out": spec.get("fade_out") or found.get("fade_out")}
                # bake [user]/[mention]/… from the firing command or event
                if spec.get("text") is not None:
                    item_["text"] = spec["text"]
                ctx_ = spec.get("ctx")
                if ctx_:
                    for k_ in ("text", "label", "fmt_on", "fmt_off"):
                        if item_.get(k_):
                            item_[k_] = _bake(item_[k_], ctx_)
                if item_.get("kind") == "timer" and item_.get("autostart"):
                    _timer_op(item_.get("id"), "start", configured=item_.get("seconds"))
                spec["item"] = item_
            else:
                spec = {**spec, "media": found.get("media"), "scale": found.get("w"),
                        "x": found.get("x"), "y": found.get("y"),
                        "rot": found.get("rot"), "flash": found.get("flash"),
                        "h": found.get("h"), "anim": found.get("anim"),
                        "anim_dir": found.get("anim_dir"), "queue": found.get("queue"),
                        "chroma_on": found.get("chroma_on"), "chroma": found.get("chroma"),
                        "chroma_tol": found.get("chroma_tol"),
                        "chroma_soft": found.get("chroma_soft"), "z": found.get("z"),
                        "opacity": found.get("opacity"), "delay": found.get("delay")}
        elif mode == "clear":
            vcam.clear_overlays(spec.get("layer"), fade_out=spec.get("fade_out"))
            stg.clear(spec.get("layer"))
            if spec.get("purge_queue"):
                vcam.clear_queue(spec.get("layer"))
            return {"ok": True}
        item = spec.get("item")   # a drawn overlay (text / gauge / timer)
        vres = {"ok": False}
        if mode == "clear" or vcam.status()["running"]:
            try:
                if item is not None:
                    vres = vcam.add_item(
                        item, layer=spec.get("layer"),
                        seconds=(spec.get("seconds") if mode == "timed" else None))
                else:
                    vres = vcam.fire_overlay(spec.get("media"), spec.get("seconds"),
                                             spec.get("pos") or "center", spec.get("scale"),
                                             mode=mode, layer=spec.get("layer"),
                                             x=spec.get("x"), y=spec.get("y"),
                                             rot=spec.get("rot"), flash=spec.get("flash"),
                                             h=spec.get("h"),
                                             fade_in=spec.get("fade_in"),
                                             fade_out=spec.get("fade_out"),
                                             anim=spec.get("anim"),
                                             anim_dir=spec.get("anim_dir"),
                                             queue=spec.get("queue"),
                                             chroma_on=spec.get("chroma_on"),
                                             chroma=spec.get("chroma"),
                                             chroma_tol=spec.get("chroma_tol"),
                                             chroma_soft=spec.get("chroma_soft"),
                                             z=spec.get("z"),
                                             opacity=spec.get("opacity"),
                                             delay=spec.get("delay"))
            except Exception as ex:  # noqa: BLE001
                vres = {"ok": False, "error": str(ex)}
        sres = stg.fire(spec.get("media"), spec.get("seconds"),
                        spec.get("pos") or "center", spec.get("scale"),
                        mode=mode, layer=spec.get("layer"),
                        x=spec.get("x"), y=spec.get("y"), item=item,
                        z=spec.get("z"), opacity=spec.get("opacity"),
                        rot=spec.get("rot"), flash=spec.get("flash"),
                        anim=spec.get("anim"), anim_dir=spec.get("anim_dir"))
        if mode == "clear" or vres.get("ok") or (sres.get("ok") and stg.watching()):
            return {"ok": True}
        return {"ok": False, "error": sres.get("error") if not sres.get("ok")
                else "no virtual camera running and no Stage open"}
    engine.overlay_cb = _overlay_action

    async def _camera_action(op: str) -> dict:
        """The `camera` action: start or stop the virtual camera, freeze the
        picture, or let it run again. `start` repeats however you last started
        it from the Chat tab (device, size, fps, linked scene)."""
        op = (op or "").lower()
        if op in ("black", "blackout"):
            return vcam.set_blackout(True)
        if op in ("unblack", "reveal"):
            return vcam.set_blackout(False)
        if op == "freeze":
            return vcam.set_frozen(True)
        if op in ("resume", "unfreeze"):
            return vcam.set_frozen(False)
        if op == "stop":
            vcam.set_frozen(False)
            vcam.set_blackout(False)
            await asyncio.get_event_loop().run_in_executor(None, vcam.stop)
            return {"ok": True}
        if op == "start":
            if vcam.status()["running"]:
                vcam.set_frozen(False)
                return {"ok": True, "already": True}
            cfg0 = config_store.load()
            last = cfg0.get("vcam_last") or {}
            res = vcam.start(last.get("device") or 0, last.get("width") or 1280,
                             last.get("height") or 720, last.get("fps") or 30,
                             mirror=cfg0.get("vcam_mirror", False))
            if not res.get("ok"):
                return res
            await asyncio.sleep(0.8)
            if not vcam.status()["running"]:
                return {"ok": False, "error": vcam.status().get("error") or "camera didn't start"}
            scene = (last.get("scene") or "").strip() or cfg0.get("chat_scene", "")
            scn = _find_scene(cfg0, scene)
            for o in ((scn or {}).get("overlays") or []):
                # A Poll Viewer gates ITSELF — it draws nothing unless a poll is
                # running — so it mounts whenever its group is on screen. Its
                # always-on/callable flag would only ever be a way to make polls
                # silently invisible, so it is not consulted.
                if (not o.get("visible") and (o.get("kind") or "") != "poll_viewer") \
                        or _group_hidden(scn, o.get("group")):
                    continue
                if _group_is_intro(scn, o.get("group")):
                    continue     # the pre-show mounts these, not the camera
                if _group_is_pause(cfg0, o.get("group")):
                    continue     # pausing mounts these, not the camera
                lay = o.get("layer") or f"itm-{o.get('id')}"
                if (o.get("kind") or "media") != "media":
                    if o.get("kind") == "timer" and o.get("autostart"):
                        _timer_op(o.get("id"), "start", configured=o.get("seconds"))
                    vcam.add_item(o, layer=lay, always_on=True)
                elif o.get("media"):
                    vcam.fire_overlay(o["media"], mode="hold", layer=lay,
                                      scale=o.get("w"), x=o.get("x"), y=o.get("y"),
                                      rot=o.get("rot"), flash=o.get("flash"),
                                      h=o.get("h"), z=o.get("z"),
                                      opacity=o.get("opacity"), delay=o.get("delay"),
                                      chroma_on=o.get("chroma_on"), chroma=o.get("chroma"),
                                      chroma_tol=o.get("chroma_tol"),
                                      chroma_soft=o.get("chroma_soft"))
            return {"ok": True, "scene": scene}
        return {"ok": False, "error": f"unknown camera op '{op}'"}

    def _play_audio(name: str, volume=None) -> dict:
        """Play a sound cue on THIS machine's default output. It never touches
        the video pipe — route your system audio into Discord (a loopback /
        monitor source) if you want viewers to hear it."""
        fn = os.path.basename(str(name or "").strip())
        if not fn:
            return {"ok": False, "error": "no audio file set"}
        path = fn if os.path.isabs(fn) else os.path.join(IMAGES_DIR, fn)
        if not os.path.isfile(path):
            return {"ok": False, "error": f"no such audio file: {fn}"}
        try:
            vol = max(0.0, min(1.0, float(volume) / 100.0)) if volume is not None else 1.0
        except (TypeError, ValueError):
            vol = 1.0
        for exe, argv in (
            ("ffplay",  ["-nodisp", "-autoexit", "-loglevel", "quiet",
                         "-volume", str(int(vol * 100)), path]),
            ("paplay",  [f"--volume={int(vol * 65536)}", path]),
            ("afplay",  ["-v", f"{vol:.2f}", path]),
            ("cvlc",    ["--play-and-exit", "--intf", "dummy",
                         f"--gain={vol:.2f}", path]),
            ("aplay",   [path]),
        ):
            if shutil.which(exe):
                try:
                    subprocess.Popen([exe, *argv],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    return {"ok": True, "player": exe, "file": fn}
                except OSError as e:  # noqa: PERF203 — try the next player
                    engine._log("error", f"audio: {exe} failed ({e})")
        return {"ok": False,
                "error": "no audio player found — install ffmpeg (ffplay) or "
                         "pulseaudio-utils (paplay)"}

    def _notify_overlay(cfg0):
        """The ONE overlay every command's notification reuses: whichever
        overlay in the linked scene (or the globals) has its layer slot set to
        `notify`. Nothing to designate per command, nothing to duplicate."""
        scene_name = cfg0.get("chat_scene", "")
        linked = list((_find_scene(cfg0, scene_name) or {}).get("overlays") or [])
        linked += list(cfg0.get("scene_globals") or [])
        want = engine.session_overlay("notify_overlay").strip()
        if want:
            # An explicit id is explicit: honour it even when the overlay lives
            # in a scene the Chat tab isn't currently linked to — otherwise
            # switching scenes silently kills every command's notification.
            every = list(linked)
            for sc in (cfg0.get("scenes") or []):
                every += list(sc.get("overlays") or [])
            for o in every:
                if str(o.get("id") or "") == want:
                    return o
        for o in linked:    # legacy: an overlay whose layer slot says "notify"
            if str(o.get("layer") or "").strip().lower() == "notify":
                return o
        return None

    async def _notify_action(text: str, ctx: dict) -> dict:
        """Play a command's notification line on the shared notify overlay.
        Quiet no-op when the scene has no notify overlay — the same rule every
        other camera-dependent action follows."""
        cfg0 = config_store.load()
        o = _notify_overlay(cfg0)
        if o is None:
            return {"ok": False, "error": "no overlay has its layer set to 'notify'"}
        return _overlay_action({"id": o.get("id"), "stage": cfg0.get("chat_scene", ""),
                                "mode": "timed",
                                "seconds": o.get("seconds") or 5,
                                "text": text, "ctx": ctx})
    engine.notify_cb = _notify_action

    async def _pause_overlay(on: bool) -> dict:
        """Cover the stream while the session is paused, and uncover it on
        resume. It's an ordinary scene group — picked in Go Live Options."""
        cfg0 = config_store.load()
        grp = engine.session_overlay("pause_overlay").strip()
        if not grp:
            return {"ok": True, "skipped": "no pause overlay set"}
        # Look in the linked scene first, then anywhere — the group you picked
        # is the group you meant, whichever scene you built it in.
        owner = cfg0.get("chat_scene", "")
        if not _scene_group(cfg0, owner, grp):
            for sc in (cfg0.get("scenes") or []):
                if any(str(o.get("group") or "").strip().lower() == grp.lower()
                       for o in (sc.get("overlays") or [])):
                    owner = sc.get("name") or ""
                    break
        return _overlay_action({"group": grp, "stage": owner,
                                "mode": "clear" if not on else ""})
    engine.pause_overlay_cb = _pause_overlay

    async def _snapshot_action(caption: str) -> dict:
        """The `snapshot` action: grab the CURRENT virtual-camera frame —
        overlays and all — and post it to chat. Quiet when the camera isn't
        running, like every other camera-dependent action."""
        data = await asyncio.get_event_loop().run_in_executor(None, vcam.preview_jpeg)
        if not data:
            return {"ok": False, "error": "virtual camera isn't running"}
        os.makedirs(IMAGES_DIR, exist_ok=True)
        name = f"snap-{uuid.uuid4().hex}.jpg"
        with open(os.path.join(IMAGES_DIR, name), "wb") as fh:
            fh.write(data)
        await botmgr.announce((caption or "").strip(), f"images/{name}")
        return {"ok": True, "file": name}
    engine.snapshot_cb = _snapshot_action
    engine.camera_cb = _camera_action

    @web.middleware
    async def security_mw(request, handler):
        # 1. Client-IP gate (the remote-access whitelist). Loopback always
        #    passes; with remote access off, the 127.0.0.1 bind means nothing
        #    else can even connect — this check is the enforcement layer once
        #    the bind is 0.0.0.0. Judged on the SOCKET peer, never a header.
        if not _ip_whitelisted(request.remote, engine.cfg):
            raise web.HTTPForbidden(text="your IP is not whitelisted for remote access")
        # 2. Host-header pinning kills DNS rebinding: a page on attacker.com
        #    whose DNS flips to this server still arrives with Host:
        #    attacker.com — only localhost / IP-literal hosts are served.
        if not _host_ok(request.headers.get("Host") or ""):
            raise web.HTTPForbidden(text="bad host")
        if request.path in SENSITIVE_PATHS:
            token = request.cookies.get("df_auth") or request.headers.get("X-DiscoFlate-Auth")
            if token != secret:
                raise web.HTTPForbidden(
                    text="missing local auth — open the DiscoFlate UI in this browser first")
        return await handler(request)

    app = web.Application(middlewares=[security_mw])

    async def index(request):
        with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as fh:
            html = fh.read()
        # The local-auth secret ALSO rides in the page (meta tag): browsers that
        # block or strip cookies on localhost (Opera's blocker, embedded docks/
        # webviews) still authorize — api() echoes it as X-DiscoFlate-Auth.
        # Same trust boundary as the cookie: only a page load that passed the
        # IP-whitelist + host-pinning gates can obtain it.
        html = html.replace("<head>", f'<head><meta name="df-auth" content="{secret}">', 1)
        resp = web.Response(text=html, content_type="text/html")
        # Never let a browser hold a stale panel: the UI ships as one file
        # that changes every update, and a cached copy silently mixes old
        # JavaScript with a new server — which reads as random features
        # breaking rather than as a cache. It's a local read; costs nothing.
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.set_cookie("df_auth", secret, httponly=True, samesite="Strict", path="/")
        return resp

    async def get_state(request):
        st = _public_state(engine, botmgr)
        st["timers"] = _timer_map()      # live Timer-overlay countdowns
        st["vars"] = dict(engine._vars)  # so /stage can substitute [var:x] live
        return web.json_response(st)

    async def get_guilds(request):
        return web.json_response(botmgr.list_guilds())

    async def guard(request):
        if not _origin_ok(request):
            raise web.HTTPForbidden(text="bad origin")

    # ---- config -----------------------------------------------------------
    # Minimum shape per key — a patch with the wrong container type is refused
    # (a malformed import/tab can't put a string where the engine expects a list).
    _TYPE_FLOOR = {"commands": list, "events": list, "modes": list, "prizes": list,
                   "owner_commands": list, "chat_buttons": list,
                   "scenes": list, "scene_globals": list,
                   "capacity_events": list, "polls": list, "competitions": list,
                   "capacity_ranges": list, "listen_targets": list, "broadcasts": list,
                   "always_on_commands": list, "cooldown_exempt_user_ids": list,
                   "cooldown_exempt_names": list, "command_names": dict, "roll": dict,
                   "auto_report": dict, "templates": dict,
                   "vendors": dict, "allow": dict, "server_channels": dict}

    async def set_config(request):
        await guard(request)
        body = await _json(request)
        # Optimistic concurrency: a client that sends the rev it last saw is
        # rejected if someone else saved since — the fix for the stale-tab
        # full-snapshot clobber. Clients that send no rev skip the check.
        if "config_rev" in body:
            try:
                client_rev = int(body.get("config_rev") or 0)
            except (TypeError, ValueError):
                client_rev = -1
            if client_rev != config_store.load().get("config_rev", 0):
                raise web.HTTPConflict(text="config changed elsewhere — reload and retry")
        patch = {}
        for key in ("command_prefix", "command_names", "capacity_message",
                    "capacity_embed", "capacity_title", "pumptimer_embed", "pumptimer_title",
                    "cooldown_embed", "cooldown_title", "pump_embed", "pump_title",
                    "pause_embed", "pause_title",
                    "system_buffer_seconds", "cooldown_message", "pumptimer_message", "pump_message",
                    "roll", "prizes", "owner_commands", "chat_buttons",
                    "scenes", "scene_globals", "scene_globals_meta", "chat_scene",
                    # golive / notify_overlay / pause_overlay are NOT accepted
                    # at the top level any more: the scene owns them, and
                    # /api/state reports the RESOLVED values, so a client that
                    # round-trips state back into config would otherwise flatten
                    # the live scene's show into the global fallback.
                    "chat_isolate", "chat_isolate_channel",
                    "vcam_device", "vcam_size", "standby_text",
                    "capacity_ranges", "commands", "modes", "events",
                    "capacity_events", "polls", "competitions", "minigames",
                    "bonus_rounds",
                    "allow", "pumpdirect_path", "cooldown_seconds",
                    "cooldown_exempt_user_ids", "cooldown_exempt_names", "operator_name", "auto_report",
                    "listen_guild_id", "listen_channel_id",
                    "listen_targets", "anon_user_label", "output_headers", "rich_output", "templates",
                    "allow_dms", "server_channels", "silence_onoff_log",
                    "mock_calibration_seconds_to_100",
                    "always_on_enabled", "always_on_commands",
                    "event_in_process_message", "event_cooldown_message", "broadcasts",
                    "listener_message_on", "listener_message_off",
                    "listener_on_embed", "listener_on_title", "listener_off_embed", "listener_off_title",
                    "pause_message", "resume_message", "paused_notice_message"):
            if key in body:
                want = _TYPE_FLOOR.get(key)
                if want and not isinstance(body[key], want):
                    raise web.HTTPBadRequest(text=f"{key} must be a {want.__name__}")
                patch[key] = body[key]
        # Gameplay belongs to the SCENE. Route those keys into the live scene's
        # block instead of the top level, so editing Commands or Game while
        # "Tuesday Show" is selected changes Tuesday Show and nothing else.
        # `scenes` itself is sent whole by the panel, so a patch carrying both
        # must fold the gameplay INTO the scenes it also just sent.
        gp = {k: patch.pop(k) for k in list(patch) if k in config_store.GAMEPLAY_KEYS}
        if gp:
            base = config_store.load()
            scenes = copy.deepcopy(patch.get("scenes")
                                   if isinstance(patch.get("scenes"), list)
                                   else (base.get("scenes") or []))
            live = str(patch.get("chat_scene", base.get("chat_scene")) or "").strip().lower()
            hit = next((sc for sc in scenes
                        if str(sc.get("name") or "").strip().lower() == live), None)
            if hit is not None:
                blk = hit.get("gameplay")
                hit["gameplay"] = {**(blk if isinstance(blk, dict) else {}), **gp}
                patch["scenes"] = scenes
            else:
                # no scene to own it (none selected yet) — keep the old behaviour
                # rather than dropping the edit on the floor
                patch.update(gp)
        cfg = config_store.update(patch)
        engine.set_config(cfg)
        return web.json_response(_public_state(engine, botmgr))

    async def set_remote_access(request):
        """Toggle LAN remote access + edit the IP whitelist. Rebinds the web
        server live: ON → 0.0.0.0 (whitelist enforced per request), OFF → back
        to loopback-only. Only reachable from loopback OR an already-whitelisted
        client (the middleware) — a stranger can't whitelist themselves."""
        await guard(request)
        b = await _json(request)
        enabled = bool(b.get("enabled"))
        ips = [str(x).strip() for x in (b.get("allowed_ips") or []) if str(x).strip()]
        bad = [x for x in ips if not _valid_ip_entry(x)]
        if bad:
            raise web.HTTPBadRequest(text=f"not a valid IP / CIDR / wildcard: {', '.join(bad)}")
        cfg = config_store.update({"remote_access": {"enabled": enabled, "allowed_ips": ips}})
        engine.set_config(cfg)
        # Rebind if the desired interface changed (0.0.0.0 covers loopback, so
        # the swap is stop-old → start-new; revert to loopback on failure).
        want = "0.0.0.0" if enabled else "127.0.0.1"
        runner, site = net.get("runner"), net.get("site")
        if runner is not None and net.get("host") != want:
            try:
                if site is not None:
                    await site.stop()
                new_site = web.TCPSite(runner, want, PORT)
                await new_site.start()
                net["site"], net["host"] = new_site, want
                engine._log("bot", f"REMOTE ACCESS {'ON — listening on the LAN (whitelist enforced)' if enabled else 'off — loopback only'}")
            except OSError as e:
                fallback = web.TCPSite(runner, "127.0.0.1", PORT)
                await fallback.start()
                net["site"], net["host"] = fallback, "127.0.0.1"
                cfg = config_store.update({"remote_access": {"enabled": False, "allowed_ips": ips}})
                engine.set_config(cfg)
                engine._log("error", f"couldn't bind the LAN interface: {e} — remote access stayed OFF")
                raise web.HTTPBadRequest(text=f"couldn't open the LAN port: {e}")
        return web.json_response(_public_state(engine, botmgr))

    async def set_vendors(request):
        """Vendor credential writes — separated from the generic config patch so
        credentials never ride along in (or come back from) full-config saves.
        Sends only changed fields; an empty string clears a field."""
        await guard(request)
        b = await _json(request)
        vendor = (b.get("vendor") or "").strip().lower()
        creds = b.get("creds")
        if not vendor or not isinstance(creds, dict):
            raise web.HTTPBadRequest(text="expected {vendor, creds:{field:value}}")
        cfg = config_store.load()
        cur = cfg.setdefault("vendors", {}).setdefault(vendor, {})
        for f, val in creds.items():
            cur[str(f)] = str(val or "")
        cfg = config_store.save(cfg)
        engine.set_config(cfg)
        return web.json_response(_public_state(engine, botmgr))

    async def set_mock(request):
        await guard(request)
        body = await _json(request)
        cfg = config_store.update({"mock_mode": bool(body.get("enabled"))})
        engine.set_config(cfg)
        engine._log("bot", f"MOCK MODE {'ON — devices will NOT fire' if cfg['mock_mode'] else 'off'}")
        return web.json_response(_public_state(engine, botmgr))

    async def set_listener(request):
        await guard(request)
        body = await _json(request)
        cfg = config_store.update({"listener_enabled": bool(body.get("enabled"))})
        engine.set_config(cfg)
        engine._log("bot", f"listener {'ENABLED' if cfg['listener_enabled'] else 'muted'}")
        enabled = cfg["listener_enabled"]
        msg = ((cfg.get("listener_message_on") if enabled
               else cfg.get("listener_message_off")) or "").strip()
        footer = f"-# DiscoFlate v{VERSION} by AireGasm"
        footer_plain = f"DiscoFlate v{VERSION} by AireGasm"
        if enabled:
            # STRICT activation order: 1) the ON message posts, 2) its
            # [!command] tokens fire, 3) events release their first rounds.
            # (Events are held by the engine's activation hold until step 3.)
            if msg:
                text = engine.render(engine.strip_inline(msg))
                if text.strip():
                    if cfg.get("listener_on_embed"):
                        ttl = engine.render((cfg.get("listener_on_title") or "").strip()) or "🟢 Activation ON"
                        await botmgr.post_embed(ttl, text, footer=footer_plain)
                    else:
                        await botmgr.announce(f"{text}\n{footer}", None)
                await engine.fire_inline(msg)
            # Go Live: an intro HOLDS the game (commands + events) until it
            # ends; otherwise the session starts right now.
            async def _go_live():
                engine.finish_activation()
            engine.intro_done_cb = _go_live
            started = await engine.start_intro(
                announce_cb=lambda t, img=None: botmgr.announce(t, img))
            if not started:
                engine.finish_activation()
        elif msg:
            # OFF message renders text only — no fires, the session is closing.
            text = engine.render(msg)
            if text.strip():
                if cfg.get("listener_off_embed"):
                    ttl = engine.render((cfg.get("listener_off_title") or "").strip()) or "🔴 Activation OFF"
                    await botmgr.post_embed(ttl, text, footer=footer_plain)
                else:
                    await botmgr.announce(f"{text}\n{footer}", None)
        return web.json_response(_public_state(engine, botmgr))

    async def command_toggle(request):
        await guard(request)
        b = await _json(request)
        name, enabled = (b.get("name") or "").strip().lower(), bool(b.get("enabled"))
        cfg = config_store.load()
        gp = config_store.live_gameplay(cfg)   # the live scene owns the commands
        for c in gp.get("commands", []):
            if (c.get("name") or "").strip().lower() == name:
                c["enabled"] = enabled
        config_store.save(cfg)
        engine.set_config(cfg)
        engine._log("bot", f"command '{name}' {'enabled' if enabled else 'disabled'}")
        return web.json_response(_public_state(engine, botmgr))

    async def mode_toggle(request):
        await guard(request)
        b = await _json(request)
        name, enabled = (b.get("name") or "").strip().lower(), bool(b.get("enabled"))
        cfg = config_store.load()
        gp = config_store.live_gameplay(cfg)   # the live scene owns modes too
        mode = next((m for m in gp.get("modes", [])
                     if (m.get("name") or "").strip().lower() == name), None)
        if mode is None:
            raise web.HTTPBadRequest(text="mode not found")
        mode["enabled"] = enabled
        members = {str(x).strip().lower() for x in (mode.get("commands") or [])}
        ev_members = {str(x).strip().lower() for x in (mode.get("events") or [])}
        for c in gp.get("commands", []):
            if (c.get("name") or "").strip().lower() in members:
                c["enabled"] = enabled
        for ev in gp.get("events", []):
            if (ev.get("name") or "").strip().lower() in ev_members:
                ev["enabled"] = enabled
        config_store.save(cfg)
        engine.set_config(cfg)
        engine._log("bot", f"mode '{name}' {'ON' if enabled else 'OFF'} → "
                    f"{len(members)} cmd(s), {len(ev_members)} event(s)")
        msg = (mode.get("message_on") if enabled else mode.get("message_off")) or ""
        if msg.strip():
            await botmgr.announce(engine.render(msg.strip()), None)
        return web.json_response(_public_state(engine, botmgr))

    async def set_token(request):
        await guard(request)
        body = await _json(request)
        cfg = config_store.update({"discord_token": (body.get("token") or "").strip()})
        await botmgr.ensure(cfg["discord_token"], force=True)
        return web.json_response(_public_state(engine, botmgr))

    async def reveal_token(request):
        """The 👁 next to the token field: hand the saved token back so it can
        be copied to another install. SENSITIVE_PATHS-gated like token save."""
        await guard(request)
        return web.json_response({"ok": True,
                                  "token": config_store.load().get("discord_token", "")})

    async def reconnect(request):
        await guard(request)
        await botmgr.reconnect()
        return web.json_response(_public_state(engine, botmgr))

    # ---- devices ----------------------------------------------------------
    async def import_pumpdirect(request):
        await guard(request)
        cfg = config_store.load()
        found = pumpdirect_import.load_kasa_devices(cfg.get("pumpdirect_path", ""))
        devices = cfg.get("devices", [])
        existing = {d["id"] for d in devices}
        added = 0
        for d in found:
            if d["id"] not in existing:
                devices.append({**{k: d[k] for k in
                                ("id", "label", "host", "child_id",
                                 "calibration_seconds_to_100", "source")},
                                "type": "pump"})
                added += 1
        cfg["devices"] = devices
        if cfg.get("active_device_id") is None and devices:
            cfg["active_device_id"] = devices[0]["id"]
        config_store.save(cfg)
        engine.set_config(cfg)
        return web.json_response({"added": added, "found": len(found),
                                  "state": _public_state(engine, botmgr)})

    # id fields required per vendor (for validation + which key becomes the label)
    _VENDOR_REQ = {"kasa": "host", "tapo": "host", "tuya": "device_id",
                   "govee": "device_id", "wyze": "mac", "homeassistant": "entity_id",
                   "kauf": "host"}

    async def discover(request):
        await guard(request)
        b = await _json(request)
        vendor = (b.get("vendor") or "kasa").strip().lower()
        cfg = config_store.load()
        creds = (cfg.get("vendors") or {}).get(vendor, {}) if vendor != "kasa" else {}
        try:
            found = await device_control.discover(vendor, creds)
        except Exception as e:  # noqa: BLE001
            raise web.HTTPBadRequest(text=f"discover failed: {e}")
        return web.json_response(found)

    async def probe_device(request):
        """Ask ONE address what it is. This is how you add a POWER STRIP whose
        outlets never showed up in Discover: broadcast can be dropped by mesh
        APs, client isolation or a separate VLAN, and some strips answer the
        broadcast with a trimmed record that omits their children entirely."""
        await guard(request)
        b = await _json(request)
        host = (b.get("host") or "").strip()
        if not host:
            raise web.HTTPBadRequest(text="an address is required")
        try:
            found = await device_control.probe(b.get("vendor") or "kasa", host)
        except Exception as e:  # noqa: BLE001
            raise web.HTTPBadRequest(text=f"nothing answered at {host}: {e}")
        return web.json_response(found)

    async def add_device(request):
        await guard(request)
        b = await _json(request)
        vendor = (b.get("vendor") or "kasa").strip().lower()
        dev = {
            "id": f"dev:{uuid.uuid4().hex[:8]}",
            "vendor": vendor,
            "host": (b.get("host") or None),
            "child_id": (b.get("child_id") or None),
            "device_id": (b.get("device_id") or None),
            "sku": (b.get("sku") or None),
            "mac": (b.get("mac") or None),
            "model": (b.get("model") or None),
            "entity_id": (b.get("entity_id") or None),
            "entity": (b.get("entity") or None),   # Kauf/ESPHome switch object id
            "calibration_seconds_to_100": _num(b.get("calibration_seconds_to_100")),
            "source": b.get("source") or "manual",
            "type": b.get("type") or "pump",
        }
        req = _VENDOR_REQ.get(vendor)
        if req and not dev.get(req):
            raise web.HTTPBadRequest(text=f"{vendor} device needs {req}")
        dev["label"] = (b.get("label") or (dev.get(req) if req else None) or vendor).strip()
        _id = dev.get(req) if req else dev.get("id")
        device_control._dbg(f"ADD vendor={vendor} label={dev['label']!r} target={_id} "
                            f"type={dev['type']} source={dev['source']}")
        cfg = config_store.load()
        cfg.setdefault("devices", []).append(dev)
        if cfg.get("active_device_id") is None:
            cfg["active_device_id"] = dev["id"]
        config_store.save(cfg)
        engine.set_config(cfg)
        return web.json_response(_public_state(engine, botmgr))

    async def device_on(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await engine.device_on(b.get("id")))

    async def device_off(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await engine.device_off(b.get("id")))

    async def remove_device(request):
        await guard(request)
        b = await _json(request)
        did = b.get("id")
        cfg = config_store.load()
        cfg["devices"] = [d for d in cfg.get("devices", []) if d.get("id") != did]
        if cfg.get("active_device_id") == did:
            cfg["active_device_id"] = cfg["devices"][0]["id"] if cfg["devices"] else None
        config_store.save(cfg)
        engine.set_config(cfg)
        return web.json_response(_public_state(engine, botmgr))

    async def set_active(request):
        await guard(request)
        b = await _json(request)
        cfg = config_store.update({"active_device_id": b.get("id")})
        engine.set_config(cfg)
        return web.json_response(_public_state(engine, botmgr))

    async def test_device(request):
        await guard(request)
        b = await _json(request)
        cfg = config_store.load()
        dev = next((d for d in cfg.get("devices", []) if d.get("id") == b.get("id")), None)
        if dev is None:
            raise web.HTTPBadRequest(text="device not found")
        return web.json_response(await engine.test_device(dev, 2.0))

    # Every field that says WHERE a device is. A re-point writes the new
    # vendor's fields and clears the rest, because set_state() dispatches on
    # `vendor` and then reads that vendor's field directly — a leftover from
    # the previous brand is at best confusing and at worst a KeyError.
    _ADDRESS_FIELDS = ("host", "child_id", "device_id", "sku", "mac",
                       "entity_id", "entity", "model")

    async def repoint_device(request):
        """Point an existing device at a DIFFERENT outlet.

        The pump is the record: its id, name and calibration stay, so every
        command, range, overlay and timer that references it keeps working.
        Only the address changes. This is what you use when a plug gets
        swapped — no relabelling the hardware, no recalibrating.
        """
        await guard(request)
        b = await _json(request)
        did = b.get("id")
        cfg = config_store.load()
        dev = next((d for d in cfg.get("devices", []) if d.get("id") == did), None)
        if dev is None:
            raise web.HTTPBadRequest(text="no such device")

        # Mid-fire, the relay that's ON is the OLD one. Swapping the address
        # now would send the abort somewhere else and leave it stuck on.
        if engine.is_firing(did):
            raise web.HTTPBadRequest(
                text="that pump is firing right now — stop it first, "
                     "otherwise the outlet it's using would be left on")

        vendor = (b.get("vendor") or dev.get("vendor") or "kasa").strip().lower()
        req = _VENDOR_REQ.get(vendor)
        if req and not (b.get(req) or "").strip():
            raise web.HTTPBadRequest(text=f"a {vendor} outlet needs {req}")

        # already bound elsewhere? say so rather than quietly double-driving it
        def addr(d):
            return (str(d.get("vendor") or "kasa").lower(), str(d.get("host") or ""),
                    str(d.get("child_id") or ""), str(d.get("device_id") or ""),
                    str(d.get("mac") or ""), str(d.get("entity_id") or ""))
        want = (vendor, str(b.get("host") or ""), str(b.get("child_id") or ""),
                str(b.get("device_id") or ""), str(b.get("mac") or ""),
                str(b.get("entity_id") or ""))
        clash = next((d for d in cfg.get("devices", [])
                      if d.get("id") != did and addr(d) == want), None)
        if clash and not b.get("force"):
            raise web.HTTPBadRequest(
                text=f"that outlet is already used by \"{clash.get('name') or clash.get('label')}\" "
                     f"— two pumps on one relay. Re-point that one first, or resend with force.")

        was = device_control._ident(dev)
        for k in _ADDRESS_FIELDS:
            dev.pop(k, None)
        for k in _ADDRESS_FIELDS:
            v = b.get(k)
            if v not in (None, ""):
                dev[k] = v
        dev["vendor"] = vendor
        # `label` is the OUTLET's own name; `name` is what YOU called the pump.
        # A re-point moves the pump, so the label follows and the name never does.
        if (b.get("label") or "").strip():
            dev["label"] = b["label"].strip()
        dev["source"] = "repointed"

        cfg = config_store.save(cfg)
        engine.set_config(cfg)
        device_control._dbg(f"REPOINT {did} {was} -> {device_control._ident(dev)} "
                            f"vendor={vendor} (kept name={dev.get('name')!r} "
                            f"cal={dev.get('calibration_seconds_to_100')})")
        return web.json_response(_public_state(engine, botmgr))

    async def rename_device(request):
        """The ✏️ next to a device: give it a friendly name. Devices are
        referenced by id everywhere, so renaming is purely cosmetic and can't
        break commands, ranges or the Device Timer List. Blank restores the
        original vendor label."""
        await guard(request)
        b = await _json(request)
        did, nm = b.get("id"), (b.get("name") or "").strip()[:60]
        cfg = config_store.load()
        for d in cfg.get("devices", []):
            if d.get("id") == did:
                if nm:
                    d["name"] = nm
                else:
                    d.pop("name", None)
        cfg = config_store.save(cfg)
        engine.set_config(cfg)
        return web.json_response(_public_state(engine, botmgr))

    async def set_device_type(request):
        await guard(request)
        b = await _json(request)
        did, typ = b.get("id"), (b.get("type") or "pump")
        if typ not in ("pump", "other"):
            typ = "pump"
        cfg = config_store.load()
        for d in cfg.get("devices", []):
            if d.get("id") == did:
                d["type"] = typ
        config_store.save(cfg)
        engine.set_config(cfg)
        return web.json_response(_public_state(engine, botmgr))

    async def set_calibration(request):
        await guard(request)
        b = await _json(request)
        did, secs = b.get("id"), _num(b.get("seconds_to_100"))
        cfg = config_store.load()
        for d in cfg.get("devices", []):
            if d.get("id") == did:
                d["calibration_seconds_to_100"] = secs
        config_store.save(cfg)
        engine.set_config(cfg)
        return web.json_response(_public_state(engine, botmgr))

    # ---- actions ----------------------------------------------------------
    async def abort(request):
        # Kept for the Android wrapper: android_boot.force_off() POSTs here as
        # the swipe-away / service-stop safety shutoff. (The UI uses
        # /api/control/stop, which pauses the whole session instead.)
        await guard(request)
        await engine.abort(reason="web")
        return web.json_response({"ok": True})

    async def set_capacity(request):
        await guard(request)
        b = await _json(request)
        engine.set_capacity(b.get("value"))
        return web.json_response({"ok": True, "capacity": engine.capacity})

    async def reset_users(request):
        await guard(request)
        engine.reset_users()
        return web.json_response({"ok": True})

    async def reset_lifetime(request):
        await guard(request)
        engine.reset_lifetime()
        return web.json_response({"ok": True})

    async def reset_session_leaderboard(request):
        await guard(request)
        engine.reset_current_leaderboard()
        return web.json_response({"ok": True})

    # ---- operator controls (act as the owner, posting into the channel) -----
    async def control_roll(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await botmgr.operator_roll(
            (b.get("who") or "").strip(), _num(b.get("dice")), _num(b.get("sides"))))

    async def control_pump(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await botmgr.operator_pump(
            (b.get("who") or "").strip(), _num(b.get("seconds")) or 5.0,
            device_id=(b.get("device_id") or None),
            untimed=bool(b.get("untimed"))))

    async def control_pump_stop(request):
        """Stop the pump — NOT the session pause. Whatever is firing, ends."""
        await guard(request)
        b = await _json(request)
        return web.json_response(
            await botmgr.operator_pump_stop((b.get("who") or "").strip()))

    async def control_stop(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await botmgr.operator_stop((b.get("who") or "").strip()))

    async def end_intro(request):
        """The Chat tab's 'Start now' — close the pre-show early and begin."""
        await guard(request)
        b = await _json(request)
        if b.get("next") and await engine.intro_next():
            # walk the chain: this stage ends, the next one plays
            return web.json_response({"ok": True, "advanced": True,
                                      **_public_state(engine, botmgr)})
        ended = await engine.end_intro(reason="operator")
        return web.json_response({"ok": True, "ended": ended,
                                  **_public_state(engine, botmgr)})

    async def control_resume(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await botmgr.operator_resume((b.get("who") or "").strip()))

    async def control_poll(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await botmgr.operator_start_poll((b.get("name") or "").strip()))

    async def control_capacity(request):
        await guard(request)
        return web.json_response(await botmgr.operator_broadcast_capacity())

    async def control_leaderboard(request):
        await guard(request)
        return web.json_response(await botmgr.operator_broadcast_leaderboard())

    async def control_leaderboard_life(request):
        await guard(request)
        return web.json_response(await botmgr.operator_broadcast_leaderboard_life())

    async def control_broadcast(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await botmgr.operator_broadcast_custom((b.get("message") or "")))

    async def control_cleanup(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await botmgr.operator_cleanup(_num(b.get("count")) or 1))

    async def export_config(request):
        # Full config (incl. token) for backup, plus the all-time leaderboard —
        # so a reinstall/restore brings the stats back too.
        await guard(request)
        return web.json_response({**config_store.load(),
                                  "_lifetime_leaderboard": engine.lifetime_board()})

    async def import_config(request):
        await guard(request)
        body = await _json(request)
        known = len(set(config_store.DEFAULTS) & set(body)) if isinstance(body, dict) else 0
        if known < 3:
            raise web.HTTPBadRequest(text="that doesn't look like a DiscoFlate config backup")
        # Lifetime leaderboard travels inside the backup (newer exports) — pull
        # it out before the merge so it never lands in config.json itself.
        board = body.pop("_lifetime_leaderboard", None)
        if isinstance(board, dict):
            engine.set_lifetime(board)
        body = config_store._migrate(body)   # backups can be from any older version
        cfg = config_store.save(config_store._coerce_numbers(
            config_store._deep_merge(config_store.DEFAULTS, body)))
        engine.set_config(cfg)
        await botmgr.ensure(cfg.get("discord_token"), force=True)
        return web.json_response({"ok": True})

    async def gameplay_preset(request):
        """Named gameplay presets (System tab). Presets are managed ONLY here —
        never through the generic config save — so ongoing autosaved edits can't
        overwrite them. action: save|update (upsert current live gameplay under
        name) | load (swap the four tabs to a preset) | delete | export (return
        one preset's data) | import (store a gameplay file as a named preset)."""
        await guard(request)
        b = await _json(request)
        action = (b.get("action") or "").strip().lower()
        name = (b.get("name") or "").strip()
        cfg = config_store.load()
        presets = list(cfg.get("gameplay_presets") or [])

        def _find(n):
            return next((p for p in presets if (p.get("name") or "").strip().lower() == n.lower()), None)

        _bi = BUILTIN_PRESET_NAME.lower()
        new_name = (b.get("new_name") or "").strip()
        if action in ("save", "update", "delete", "import", "rename") and (
                name.strip().lower() == _bi or new_name.lower() == _bi):
            raise web.HTTPBadRequest(text="the built-in Defaults preset is read-only")
        if action in ("save", "update", "import"):
            if not name:
                raise web.HTTPBadRequest(text="a preset name is required")
            if action == "import":
                data = b.get("data")
                if not isinstance(data, dict) or len(set(_GAMEPLAY_KEYS) & set(data)) < 2:
                    raise web.HTTPBadRequest(text="that doesn't look like a DiscoFlate gameplay/preset file")
                data = config_store._migrate(dict(data))   # files can be any older version
                snap = {k: v for k, v in data.items() if k in _GAMEPLAY_KEYS}
            else:
                snap = _gameplay_export(cfg)   # snapshot the current live gameplay
            existing = _find(name)
            if existing:
                existing["data"] = snap
            else:
                presets.append({"name": name, "data": snap})
            cfg["gameplay_presets"] = presets
            cfg = config_store.save(cfg)
            engine.set_config(cfg)
        elif action == "load":
            if name.strip().lower() == BUILTIN_PRESET_NAME.lower():
                data = _builtin_preset_data()
                if not data:
                    raise web.HTTPBadRequest(text="the built-in Defaults preset isn't available")
            else:
                p = _find(name)
                if p is None:
                    raise web.HTTPBadRequest(text="no such preset")
                data = p.get("data") or {}
            # stored presets were migrated with the config, but belt & braces:
            # the collapse is idempotent and cheap
            data = config_store._collapse_v10(dict(data))
            # A preset is a TEMPLATE now: it loads into the scene you have
            # selected, and stops there. That is what keeps one axis — there is
            # no live "preset layer" that can drift out of step with the scene.
            gp = config_store.live_gameplay(cfg)
            merged = _gameplay_merge(gp, data, "replace")
            # Remember WHICH preset this scene's gameplay came from. A load is
            # one-way — editing the tabs afterwards never writes back — so the
            # panel shows this as a readout, not as a link you can re-point.
            merged["preset_loaded"] = name
            if gp is cfg:                       # no scene yet: old flat behaviour
                cfg = config_store._coerce_numbers(merged)
            else:
                gp.clear()
                gp.update(config_store._coerce_numbers(merged))
            cfg = config_store.save(cfg)
            engine.set_config(cfg)
        elif action == "delete":
            cfg["gameplay_presets"] = [p for p in presets
                                       if (p.get("name") or "").strip().lower() != name.lower()]
            cfg = config_store.save(cfg)
            engine.set_config(cfg)
        elif action == "rename":
            if not new_name:
                raise web.HTTPBadRequest(text="a new name is required")
            p = _find(name)
            if p is None:
                raise web.HTTPBadRequest(text="no such preset")
            if _find(new_name):
                raise web.HTTPBadRequest(text="a preset with that name already exists")
            p["name"] = new_name
            cfg["gameplay_presets"] = presets
            cfg = config_store.save(cfg)
            engine.set_config(cfg)
        elif action == "export":
            if name.strip().lower() == BUILTIN_PRESET_NAME.lower():
                return web.json_response({"name": BUILTIN_PRESET_NAME, "data": _builtin_preset_data()})
            p = _find(name)
            if p is None:
                raise web.HTTPBadRequest(text="no such preset")
            return web.json_response({"name": p.get("name"), "data": p.get("data") or {}})
        else:
            raise web.HTTPBadRequest(text="unknown preset action")
        return web.json_response(_public_state(engine, botmgr))

    async def export_gameplay(request):
        # Shareable: the whole Game/Commands/Events/Templates set, no secrets.
        await guard(request)
        return web.json_response({"discoflate_gameplay": VERSION,
                                  **_gameplay_export(config_store.load())})

    async def import_gameplay(request):
        await guard(request)
        body = await _json(request)
        data = body.get("data") if isinstance(body, dict) else None
        mode = "replace" if (body or {}).get("mode") == "replace" else "add"
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="expected {mode, data}")
        # must look like a gameplay file (share at least a couple safe keys)
        if len(set(_GAMEPLAY_KEYS) & set(data)) < 2:
            raise web.HTTPBadRequest(text="that doesn't look like a DiscoFlate gameplay file")
        # a gameplay file can be from ANY older version — run the (idempotent,
        # shape-guarded) migrations over it before merging
        data = config_store._migrate(dict(data))
        merged = _gameplay_merge(config_store.load(), data, mode)
        cfg = config_store.save(config_store._coerce_numbers(merged))
        engine.set_config(cfg)
        return web.json_response(_public_state(engine, botmgr))

    REPO_URL = "https://github.com/Airegasm/DiscoFlate.git"

    def _git(*args, timeout=60):
        return subprocess.run(["git", "-C", HERE, *args],
                              capture_output=True, text=True, timeout=timeout)

    def _looks_like_discoflate() -> bool:
        """Never run repo surgery on a directory that isn't this app."""
        return all(os.path.exists(os.path.join(HERE, f))
                   for f in ("app.py", "version.json", os.path.join("web", "index.html")))

    def _repo_state() -> dict:
        """Is this install a real clone we can update from?

        Downloading the GitHub ZIP gives you the code but NO .git, so the
        in-app updater has nothing to pull into — the usual way people end up
        stranded on an old version without realising."""
        if shutil.which("git") is None:
            return {"ok": False, "kind": "no-git",
                    "why": "git isn't installed on this machine"}
        top = _git("rev-parse", "--show-toplevel", timeout=15)
        if top.returncode != 0:
            return {"ok": False, "kind": "not-a-repo",
                    "why": "this folder isn't a git clone — it looks like the "
                           "GitHub ZIP was downloaded and extracted instead of "
                           "being cloned, so in-app updates can't work"}
        root = os.path.realpath((top.stdout or "").strip())
        if root and root != os.path.realpath(HERE):
            return {"ok": False, "kind": "nested",
                    "why": f"this folder sits inside another git repo ({root}) — "
                           f"updating here would touch that repo, not DiscoFlate"}
        rem = _git("remote", "get-url", "origin", timeout=15)
        origin = (rem.stdout or "").strip()
        if rem.returncode != 0 or not origin:
            return {"ok": False, "kind": "no-origin", "origin": "",
                    "why": "this clone has no 'origin' remote to update from"}
        return {"ok": True, "kind": "clone", "origin": origin}

    def _repair_repo() -> dict:
        """Turn a ZIP install into a proper clone, in place.

        Nothing you own is at risk: data/ (token, config, uploads), .venv/ and
        dist/ are all gitignored, and .gitignore ships in the ZIP — so adopting
        the upstream tree leaves every one of them untouched."""
        if not _looks_like_discoflate():
            return {"ok": False, "output": "this folder doesn't look like a "
                                           "DiscoFlate install — refusing to touch it"}
        if shutil.which("git") is None:
            return {"ok": False, "output": "git isn't installed — install git, then retry"}
        steps = []
        try:
            if _git("rev-parse", "--git-dir", timeout=15).returncode != 0:
                r = _git("init", timeout=30)
                steps.append(("git init", r))
                if r.returncode != 0:
                    return {"ok": False, "output": _fmt(steps)}
            if _git("remote", "get-url", "origin", timeout=15).returncode == 0:
                steps.append(("set origin", _git("remote", "set-url", "origin", REPO_URL)))
            else:
                steps.append(("add origin", _git("remote", "add", "origin", REPO_URL)))
            f = _git("fetch", "origin", "main", timeout=180)
            steps.append(("fetch", f))
            if f.returncode != 0:
                return {"ok": False, "output": _fmt(steps)}
            r = _git("reset", "--hard", "FETCH_HEAD", timeout=60)
            steps.append(("adopt latest", r))
            if r.returncode != 0:
                return {"ok": False, "output": _fmt(steps)}
            b = _git("checkout", "-B", "main", "FETCH_HEAD", timeout=60)
            steps.append(("on main", b))
            _git("branch", "--set-upstream-to=origin/main", "main", timeout=30)
            return {"ok": True, "restart_needed": True,
                    "output": ("repaired — this is a real clone now, tracking "
                               "origin/main. Your data/ folder was never touched.\n\n"
                               + _fmt(steps))}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "output": f"{e}\n\n{_fmt(steps)}"}

    def _fmt(steps) -> str:
        out = []
        for label, r in steps:
            tail = ((r.stdout or "") + (r.stderr or "")).strip()
            out.append(f"$ {label}" + (f"\n{tail}" if tail else ""))
        return "\n".join(out)[:2000]

    async def repair_repo(request):
        """Help -> Updates: 'Fix my install'."""
        await guard(request)
        if os.environ.get("DISCOFLATE_DEFAULT_CONFIG") is not None:
            return web.json_response({"ok": False,
                                      "output": "Android updates via the APK — nothing to repair here"})
        res = await asyncio.get_event_loop().run_in_executor(None, _repair_repo)
        return web.json_response(res)

    async def check_updates(request):
        await guard(request)
        result = {"current_version": VERSION, "current_code": VERSION_CODE,
                  "android": os.environ.get("DISCOFLATE_DEFAULT_CONFIG") is not None}
        if not result["android"]:
            try:
                result["repo"] = _repo_state()
            except Exception as e:  # noqa: BLE001
                result["repo"] = {"ok": False, "kind": "unknown", "why": str(e)}
        try:
            async with aiohttp.ClientSession() as s:
                # cache-buster: raw.githubusercontent's CDN caches for 5 min per
                # edge — a unique query string skips it so checks are always live
                async with s.get(f"{VERSION_URL}?cb={int(time.time())}",
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = json.loads(await r.text())
            latest = int(data.get("versionCode", 0))
            result.update({"latest_version": data.get("version", "?"), "latest_code": latest,
                           "apk_url": data.get("apk_url", ""), "notes": data.get("notes", ""),
                           "update_available": latest > VERSION_CODE})
        except Exception as e:  # noqa: BLE001
            result["error"] = f"couldn't check: {e}"
        return web.json_response(result)

    async def pull_updates(request):
        # Desktop: git pull the latest code. (Android updates via APK install.)
        await guard(request)
        # A ZIP install has no repo to pull into. Rather than failing with
        # "not a git repository", make it one and carry on.
        state = _repo_state()
        if not state.get("ok") and state.get("kind") in ("not-a-repo", "no-origin"):
            fixed = await asyncio.get_event_loop().run_in_executor(None, _repair_repo)
            if not fixed.get("ok"):
                return web.json_response(fixed)
            return web.json_response({**fixed, "repaired": True})
        try:
            out = subprocess.run(["git", "-C", HERE, "pull", "--ff-only", "origin", "main"],
                                 capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                return web.json_response({"ok": True, "output": (out.stdout + out.stderr).strip()[:2000],
                                          "restart_needed": True})
            # A fast-forward can fail when upstream history was rewritten (e.g.
            # the 2026-09 APK purge). Self-heal: fetch succeeded is implied by
            # the pull attempt reaching the ff check, so adopt origin/main.
            # data/ is untracked and untouched; local edits to app source are
            # discarded (the updater's job is "run the latest code").
            fetch = subprocess.run(["git", "-C", HERE, "fetch", "origin", "main"],
                                   capture_output=True, text=True, timeout=60)
            if fetch.returncode != 0:
                return web.json_response({"ok": False,
                                          "output": (out.stdout + out.stderr + fetch.stderr).strip()[:2000]})
            reset = subprocess.run(["git", "-C", HERE, "reset", "--hard", "origin/main"],
                                   capture_output=True, text=True, timeout=60)
            ok = reset.returncode == 0
            note = ("history diverged (upstream was rewritten) — adopted the latest code\n"
                    if ok else "")
            return web.json_response({"ok": ok,
                                      "output": (note + reset.stdout + reset.stderr).strip()[:2000],
                                      "restart_needed": ok})
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "output": str(e)})

    async def session_reset(request):
        await guard(request)
        engine.session_reset()
        return web.json_response({"ok": True, "capacity": engine.capacity})

    # ---- Chat tab (owner cockpit) -----------------------------------------
    async def chat_channels(request):
        await guard(request)
        return web.json_response({"channels": botmgr.chat_channels()})

    async def chat_log(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await botmgr.chat_log(b.get("channel_id"),
                                                       str(b.get("after") or "")))

    async def chat_send(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await botmgr.owner_chat(b.get("channel_id"), b.get("text")))

    # ---- virtual camera + overlays (Chat tab, video channels) -------------
    # Android can't host a virtual camera: registering a camera device needs a
    # system driver (root), and Discord mobile only lists the real cameras.
    _IS_ANDROID = os.environ.get("DISCOFLATE_DEFAULT_CONFIG") is not None
    _VCAM_ANDROID = ("virtual camera is desktop-only — Android apps can't register "
                     "a camera device (needs a system driver / root), so Discord "
                     "mobile only ever sees the real front/back cameras. Run "
                     "DiscoFlate on the PC you join video calls from and pick the "
                     "virtual cam in Discord desktop.")

    async def camera_status(request):
        await guard(request)
        st = vcam.status()
        if _IS_ANDROID:
            st.update({"android": True, "error": _VCAM_ANDROID})
        return web.json_response(st)

    async def camera_start(request):
        await guard(request)
        if _IS_ANDROID:
            return web.json_response({"ok": False, "error": _VCAM_ANDROID})
        b = await _json(request)
        res = vcam.start(b.get("device") or 0, b.get("width") or 1280,
                         b.get("height") or 720, b.get("fps") or 30,
                         mirror=b.get("mirror"))
        if res.get("ok"):   # remember it, so a `camera: start` action can repeat it
            cfgv = config_store.update({"vcam_last": {
                "device": b.get("device") or 0, "width": b.get("width") or 1280,
                "height": b.get("height") or 720, "fps": b.get("fps") or 30,
                "scene": (b.get("stage") or "")}})
            engine.set_config(cfgv)
        if not res.get("ok"):
            return web.json_response(res)
        await asyncio.sleep(0.8)   # let the pipeline surface open errors
        st = vcam.status()
        # linked Stage design: put its always-on items on the compositor
        scene_name = (b.get("stage") or "").strip()
        if st["running"] and scene_name:
            cfg_now = config_store.load()
            scn = _find_scene(cfg_now, scene_name)
            for o in ((scn or {}).get("overlays") or []):
                # A Poll Viewer gates ITSELF — it draws nothing unless a poll is
                # running — so it mounts whenever its group is on screen. Its
                # always-on/callable flag would only ever be a way to make polls
                # silently invisible, so it is not consulted.
                if (not o.get("visible") and (o.get("kind") or "") != "poll_viewer") \
                        or _group_hidden(scn, o.get("group")):
                    continue
                if _group_is_intro(scn, o.get("group")):
                    continue     # the pre-show mounts these, not the camera
                if _group_is_pause(cfg_now, o.get("group")):
                    continue     # pausing mounts these, not the camera
                lay = o.get("layer") or f"itm-{o.get('id')}"
                if (o.get("kind") or "media") != "media":
                    if o.get("kind") == "timer" and o.get("autostart"):
                        _timer_op(o.get("id"), "start", configured=o.get("seconds"))
                    vcam.add_item(o, layer=lay, always_on=True)
                elif o.get("media"):
                    vcam.fire_overlay(o["media"], mode="hold", layer=lay,
                                      scale=o.get("w"), x=o.get("x"), y=o.get("y"),
                                      rot=o.get("rot"), flash=o.get("flash"),
                                      h=o.get("h"), anim=o.get("anim"),
                                      anim_dir=o.get("anim_dir"),
                                      chroma_on=o.get("chroma_on"), chroma=o.get("chroma"),
                                      chroma_tol=o.get("chroma_tol"),
                                      chroma_soft=o.get("chroma_soft"),
                                      opacity=o.get("opacity"), delay=o.get("delay"),
                                      always_on=True)
        return web.json_response({"ok": st["running"], **st})

    async def camera_detect(request):
        await guard(request)
        if _IS_ANDROID:
            return web.json_response({"ok": False, "error": _VCAM_ANDROID})
        res = await asyncio.get_event_loop().run_in_executor(None, vcam.detect)
        return web.json_response(res)

    # ---- virtual-cam DRIVER: probe + assisted install ---------------------
    # The driver itself can't be bundled: Linux's v4l2loopback is a kernel
    # module built against the running kernel, and Windows' filter needs an
    # admin-registered system install either way. What we CAN do: definitively
    # probe (actually open a pyvirtualcam device) and run the fix ourselves.
    def _driver_probe() -> dict:
        if camera.pyvirtualcam is None:
            return {"ok": False,
                    "error": "pyvirtualcam isn't installed — rerun start.bat/start.sh "
                             "(it installs requirements), then restart DiscoFlate"}
        try:
            with camera.pyvirtualcam.Camera(width=160, height=120, fps=20,
                                            print_fps=False):
                pass
            return {"ok": True}
        except Exception as e:  # noqa: BLE001 — "no backend" = driver missing
            return {"ok": False, "error": str(e)}

    def _linux_manual() -> str:
        pm = next((cmd for exe, cmd in (
            ("apt", "sudo apt install v4l2loopback-dkms"),
            ("dnf", "sudo dnf install v4l2loopback"),
            ("pacman", "sudo pacman -S v4l2loopback-dkms"),
        ) if shutil.which(exe)), "install the v4l2loopback package for your distro")
        return (pm + '  &&  sudo modprobe v4l2loopback exclusive_caps=1 '
                     'card_label="DiscoFlate Cam"')

    def _linux_module_installed() -> bool:
        try:
            return subprocess.run(["modinfo", "v4l2loopback"], capture_output=True,
                                  timeout=10).returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    async def camera_driver(request):
        await guard(request)
        if _IS_ANDROID:
            return web.json_response({"ok": False, "error": _VCAM_ANDROID})
        b = await _json(request)
        act = (b.get("action") or "status").lower()
        loop = asyncio.get_event_loop()
        if vcam.status()["running"]:
            return web.json_response({"ok": True})
        probe = await loop.run_in_executor(None, _driver_probe)
        if probe["ok"]:
            return web.json_response({"ok": True})
        if act == "status":
            if sys.platform == "win32":
                return web.json_response({"ok": False, "error": probe["error"],
                    "plan": "install OBS Studio via winget — its installer registers the "
                            "virtual-camera driver (~a few minutes; a UAC prompt may appear)"})
            if _linux_module_installed():
                return web.json_response({"ok": False, "error": probe["error"],
                    "plan": "load the v4l2loopback kernel module (your system password "
                            "prompt will appear)"})
            return web.json_response({"ok": False, "error": probe["error"],
                                      "manual": _linux_manual()})
        # ---- action: install -------------------------------------------------
        def _run(cmd, timeout):
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        try:
            if sys.platform == "win32":
                out = await loop.run_in_executor(None, lambda: _run(
                    ["winget", "install", "-e", "OBSProject.OBSStudio",
                     "--accept-package-agreements", "--accept-source-agreements"], 900))
            elif _linux_module_installed():
                out = await loop.run_in_executor(None, lambda: _run(
                    ["pkexec", "modprobe", "v4l2loopback", "exclusive_caps=1",
                     'card_label=DiscoFlate Cam'], 120))
            else:
                return web.json_response({"ok": False,
                    "error": "the v4l2loopback package isn't installed",
                    "manual": _linux_manual()})
        except FileNotFoundError as e:
            hint = ("winget isn't available — install OBS Studio from obsproject.com, "
                    "then press Start again") if sys.platform == "win32" else \
                   f"couldn't run the installer ({e}) — do it manually: {_linux_manual()}"
            return web.json_response({"ok": False, "error": hint})
        except subprocess.TimeoutExpired:
            return web.json_response({"ok": False,
                "error": "the install is taking too long — finish it in its own window, "
                         "then press ▶ Start again"})
        probe = await loop.run_in_executor(None, _driver_probe)
        if probe["ok"]:
            return web.json_response({"ok": True})
        tail = ((out.stdout or "") + "\n" + (out.stderr or "")).strip()[-400:]
        extra = (" Installed, but the driver didn't register — open OBS once, click "
                 "'Start Virtual Camera', close it, and retry.") if sys.platform == "win32" else ""
        return web.json_response({"ok": False,
                                  "error": (probe["error"] + "." + extra).strip(),
                                  "detail": tail})

    async def camera_preview(request):
        await guard(request)
        data = await asyncio.get_event_loop().run_in_executor(None, vcam.preview_jpeg)
        if not data:
            return web.json_response({"ok": False, "error": "virtual camera not running"},
                                     status=404)
        return web.Response(body=data, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def camera_freeze(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(vcam.set_frozen(bool(b.get("frozen"))))

    async def camera_mirror(request):
        """Flip the camera horizontally, live (the 🪞 toggle). Persisted so the
        pipeline comes back the same way next start."""
        await guard(request)
        b = await _json(request)
        on = bool(b.get("mirror"))
        cfg = config_store.update({"vcam_mirror": on})
        engine.set_config(cfg)
        return web.json_response(vcam.set_mirror(on))

    async def camera_stop(request):
        await guard(request)
        await asyncio.get_event_loop().run_in_executor(None, vcam.stop)
        return web.json_response(vcam.status())

    async def overlay_fire(request):
        await guard(request)
        b = await _json(request)
        # One router for everything: `id` calls a Stage-designed overlay (the
        # design supplies geometry/style), `media` is the legacy direct path.
        return web.json_response(_overlay_action({
            "id": b.get("id"), "stage": b.get("stage") or "",
            "media": b.get("media") or b.get("image"), "seconds": b.get("seconds"),
            "pos": b.get("pos"), "scale": b.get("scale"),
            # pass mode through UNSET when the caller didn't give one — the
            # router needs to tell "unspecified" from an explicit "timed", or
            # an always-on overlay can never be recognised as a hold
            "mode": b.get("mode"), "layer": b.get("layer"),
            "x": b.get("x"), "y": b.get("y"),
            "fade_in": b.get("fade_in"), "fade_out": b.get("fade_out"),
            "timer": b.get("timer"), "ctx": b.get("ctx"), "group": b.get("group"),
            "text": b.get("text"), "purge_queue": b.get("purge_queue")}))

    async def overlay_clear(request):
        await guard(request)
        b = await _json(request)
        vcam.clear_overlays(b.get("layer"))
        stg.clear(b.get("layer"))
        return web.json_response({"ok": True})

    # ---- the Stage: fullscreen camera + overlays, screen-shared from a phone --
    async def stage_page(request):
        with open(os.path.join(WEB_DIR, "stage.html"), "r", encoding="utf-8") as fh:
            html = fh.read()
        html = html.replace("<head>", f'<head><meta name="df-auth" content="{secret}">', 1)
        resp = web.Response(text=html, content_type="text/html")
        # Never let a browser hold a stale panel: the UI ships as one file
        # that changes every update, and a cached copy silently mixes old
        # JavaScript with a new server — which reads as random features
        # breaking rather than as a cache. It's a local read; costs nothing.
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.set_cookie("df_auth", secret, httponly=True, samesite="Strict", path="/")
        return resp

    async def stage_active(request):
        await guard(request)
        return web.json_response({"ok": True, "overlays": stg.active()})

    async def stage_done(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(stg.done(b.get("id")))

    async def stage_media(request):
        await guard(request)
        name = os.path.basename(request.match_info.get("name") or "")
        path = os.path.join(IMAGES_DIR, name)
        if not name or not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    # ---- Stages: media browser + stage designs (Stages tab) -----------------
    _AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus", ".aac")
    _VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v")

    async def media_list(request):
        await guard(request)
        out = []
        try:
            for n in sorted(os.listdir(IMAGES_DIR)):
                p = os.path.join(IMAGES_DIR, n)
                if os.path.isfile(p):
                    ext_ = os.path.splitext(n)[1].lower()
                    out.append({"name": n, "size": os.path.getsize(p),
                                "video": ext_ in _VIDEO_EXTS,
                                "audio": ext_ in _AUDIO_EXTS})
        except FileNotFoundError:
            pass
        return web.json_response({"ok": True, "media": out})

    async def media_delete(request):
        await guard(request)
        b = await _json(request)
        name = os.path.basename(str(b.get("name") or ""))
        p = os.path.join(IMAGES_DIR, name)
        if name and os.path.isfile(p):
            os.remove(p)
        return web.json_response({"ok": True})

    def _find_scene(cfg, name):
        name = (name or "").strip()
        return next((s for s in (cfg.get("scenes") or [])
                     if (s.get("name") or "").strip() == name), None)

    def _scene_item(cfg, scene_name, item_id):
        """An overlay item by id — searched in the scene, then the globals."""
        scn = _find_scene(cfg, scene_name)
        pool = (list(scn.get("overlays") or []) if scn else []) \
            + list(cfg.get("scene_globals") or [])
        return next((o for o in pool if str(o.get("id")) == str(item_id)), None)

    async def scene_design(request):
        """The /stage page (and Chat tab) fetch a design + the globals here."""
        await guard(request)
        b = await _json(request)
        cfg0 = config_store.load()
        scn = _find_scene(cfg0, b.get("name"))
        return web.json_response({"ok": scn is not None, "stage": scn,
                                  "globals": cfg0.get("scene_globals") or []})

    async def scene_export(request):
        """Download one scene as a .zip bundle: scene.json plus EVERY image and
        video it references, so it can be imported anywhere with its media."""
        await guard(request)
        name = (request.query.get("name") or "").strip()
        scn = _find_scene(config_store.load(), name)
        if scn is None:
            raise web.HTTPNotFound(text="no such stage")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("scene.json",
                       json.dumps({"discoflate_scene": 2, "scene": scn}, indent=2))
            for o in (scn.get("overlays") or []):
                n = os.path.basename(str(o.get("media") or ""))
                p = os.path.join(IMAGES_DIR, n)
                if n and os.path.isfile(p):
                    z.write(p, "media/" + n)
        buf.seek(0)
        safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip() or "stage"
        return web.Response(body=buf.read(), content_type="application/zip",
                            headers={"Content-Disposition":
                                     f'attachment; filename="{safe}.dfscene.zip"'})

    async def scene_import(request):
        """Upload a .dfscene.zip (or an older .dfstage.zip): every bundled
        image/video lands in data/images and the scene is added, renamed with a
        suffix when that name is already taken."""
        await guard(request)
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            raise web.HTTPBadRequest(text="expected a 'file' field")
        raw = io.BytesIO()
        size = 0
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            size += len(chunk)
            if size > 512 * 1024 * 1024:
                raise web.HTTPRequestEntityTooLarge(max_size=512 << 20, actual_size=size)
            raw.write(chunk)
        raw.seek(0)
        try:
            with zipfile.ZipFile(raw) as z:
                inner = ("scene.json" if "scene.json" in z.namelist()
                         else "stage.json")          # pre-3.52 bundles
                meta = json.loads(z.read(inner).decode("utf-8"))
                scn = (meta.get("stage") or meta.get("scene") or {}) \
                    if isinstance(meta, dict) else {}
                if not (scn.get("name") or "").strip():
                    raise KeyError("stage name")
                os.makedirs(IMAGES_DIR, exist_ok=True)
                for zi in z.infolist():
                    if zi.is_dir() or not zi.filename.startswith("media/"):
                        continue
                    n = os.path.basename(zi.filename)   # traversal-proof
                    if n:
                        with z.open(zi) as src, \
                                open(os.path.join(IMAGES_DIR, n), "wb") as dst:
                            shutil.copyfileobj(src, dst)
        except (zipfile.BadZipFile, KeyError, ValueError) as e:
            return web.json_response({"ok": False,
                                      "error": f"not a valid stage bundle: {e}"})
        cfg0 = config_store.load()
        scenes = list(cfg0.get("scenes") or [])
        base = (scn.get("name") or "Imported").strip()
        name, n = base, 2
        while any((s.get("name") or "").strip() == name for s in scenes):
            name = f"{base} ({n})"
            n += 1
        scn["name"] = name
        scenes.append(scn)
        cfg = config_store.save(config_store._coerce_numbers(
            {**cfg0, "scenes": scenes}))
        engine.set_config(cfg)
        return web.json_response({"ok": True, "name": name,
                                  **_public_state(engine, botmgr)})

    # ---- Device Sync: pull gameplay + media from another DiscoFlate ----------
    # Both sides speak this. The SOURCE just answers export (and serves files
    # via /api/stage/media); the TARGET's server does the pulling, so the
    # browser never fights CORS. The source must have Remote access enabled
    # with the target's IP whitelisted (same setup VC Remote uses).
    async def sync_export(request):
        await guard(request)
        cfg = config_store.load()
        media = []
        try:
            for n in sorted(os.listdir(IMAGES_DIR)):
                p = os.path.join(IMAGES_DIR, n)
                if os.path.isfile(p):
                    media.append({"name": n, "size": os.path.getsize(p)})
        except FileNotFoundError:
            pass
        return web.json_response({"ok": True, "version": VERSION,
                                  "gameplay": _gameplay_export(cfg),
                                  "gameplay_presets": cfg.get("gameplay_presets") or [],
                                  "scenes": cfg.get("scenes") or [],
                                  "scene_globals": cfg.get("scene_globals") or [],
                                  "scene_globals_meta": cfg.get("scene_globals_meta") or {},
                                  "media": media})

    async def sync_push(request):
        """PUSH this device's gameplay + stages + media to the other one.

        Implemented as "ask them to pull from me": our server calls THEIR
        /api/sync/pull with our own LAN address. Server-to-server, so the
        browser never hits CORS, and it reuses the one transfer path. Needs
        remote access enabled on BOTH (they must accept our request, and we
        must accept the fetch they make back)."""
        await guard(request)
        b = await _json(request)
        addr = str(b.get("addr") or "").strip().rstrip("/")
        if not addr:
            return web.json_response({"ok": False, "error": "no address given"})
        if not addr.lower().startswith(("http://", "https://")):
            addr = "http://" + addr
        if ":" not in addr.split("//", 1)[1]:
            addr += ":8765"
        me = _lan_ips()
        if not me:
            return web.json_response({"ok": False, "error":
                "couldn't work out this device's LAN address — is Wi-Fi on?"})
        cfg = config_store.load()
        ra = cfg.get("remote_access") or {}
        if not ra.get("enabled"):
            return web.json_response({"ok": False, "error":
                "turn on Remote access here first (System → Remote access) — the "
                "other device has to be able to fetch from this one"})
        back = f"{me[0]}:{PORT}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(addr + "/api/sync/pull",
                                  json={"addr": back, "mode": b.get("mode") or "replace"},
                                  timeout=aiohttp.ClientTimeout(total=900)) as r:
                    txt = await r.text()
                    if r.status != 200:
                        return web.json_response({"ok": False, "error":
                            f"HTTP {r.status} from {addr} — whitelist THIS device "
                            f"({me[0]}) in its Remote access list, and make sure it "
                            "runs a version with Device Sync"})
                    data = json.loads(txt)
        except Exception as e:  # noqa: BLE001 — unreachable, timeout, bad JSON
            return web.json_response({"ok": False, "error": f"couldn't reach {addr}: {e}"})
        if not data.get("ok"):
            return web.json_response({"ok": False, "error":
                (data.get("error") or "the other device refused the pull") +
                f" (it was told to fetch from {back})"})
        return web.json_response({"ok": True, "to": addr, "from": back, **data})

    async def sync_pull(request):
        await guard(request)
        b = await _json(request)
        addr = str(b.get("addr") or "").strip().rstrip("/")
        if not addr:
            return web.json_response({"ok": False, "error": "no address given"})
        if not addr.lower().startswith(("http://", "https://")):
            addr = "http://" + addr
        if ":" not in addr.split("//", 1)[1]:
            addr += ":8765"
        mode = (b.get("mode") or "replace").lower()
        new = kept = 0
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(addr + "/api/sync/export", json={},
                                  timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        return web.json_response({"ok": False, "error":
                            f"HTTP {r.status} from {addr} — enable Remote access "
                            "there and whitelist THIS device's IP (and make sure "
                            "both run a Device-Sync-capable version)"})
                    data = json.loads(await r.text())
                if not data.get("ok"):
                    return web.json_response({"ok": False,
                                              "error": data.get("error") or "export failed on the other device"})
                os.makedirs(IMAGES_DIR, exist_ok=True)
                for m in (data.get("media") or []):
                    name = os.path.basename(str(m.get("name") or ""))
                    if not name:
                        continue
                    dst = os.path.join(IMAGES_DIR, name)
                    if os.path.isfile(dst) and os.path.getsize(dst) == m.get("size"):
                        kept += 1
                        continue
                    async with s.get(f"{addr}/api/stage/media/{urllib.parse.quote(name)}",
                                     timeout=aiohttp.ClientTimeout(total=600)) as fr:
                        if fr.status != 200:
                            continue
                        tmp = dst + ".part"
                        with open(tmp, "wb") as fh:
                            async for chunk in fr.content.iter_chunked(1 << 16):
                                fh.write(chunk)
                        os.replace(tmp, dst)
                    new += 1
        except Exception as e:  # noqa: BLE001 — unreachable host, timeout, bad JSON
            return web.json_response({"ok": False, "error": f"couldn't sync from {addr}: {e}"})
        merged = _gameplay_merge(config_store.load(), data.get("gameplay") or {}, mode)
        inc_p = [p for p in (data.get("gameplay_presets") or [])
                 if isinstance(p, dict) and (p.get("name") or "").strip()]
        if inc_p:   # union by name — the other device's copy wins on a clash
            names = {(p.get("name") or "").strip().lower() for p in inc_p}
            merged["gameplay_presets"] = inc_p + [
                p for p in (merged.get("gameplay_presets") or [])
                if (p.get("name") or "").strip().lower() not in names]
        for k in ("scenes", "scene_globals"):   # scene designs ride along too
            if isinstance(data.get(k), list):
                merged[k] = data[k]
        # the global pool's group names are a dict, not a list — without this
        # a synced device would keep the overlays but lose their groups
        if isinstance(data.get("scene_globals_meta"), dict):
            merged["scene_globals_meta"] = data["scene_globals_meta"]
        cfg = config_store.save(config_store._coerce_numbers(merged))
        engine.set_config(cfg)
        return web.json_response({"ok": True, "from_version": data.get("version"),
                                  "media_new": new, "media_kept": kept,
                                  "presets": len(inc_p)})

    async def upload(request):
        await guard(request)
        os.makedirs(IMAGES_DIR, exist_ok=True)
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            raise web.HTTPBadRequest(text="expected a 'file' field")
        ext = os.path.splitext(field.filename or "")[1].lower()
        video = ext in (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v")
        audio = ext in _AUDIO_EXTS
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp") and not video and not audio:
            raise web.HTTPBadRequest(text="unsupported media type")
        name = f"{uuid.uuid4().hex}{ext}"
        dest = os.path.join(IMAGES_DIR, name)
        size = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                cap = (96 if video else 24 if audio else 8) * 1024 * 1024   # 8 MB images / 24 MB audio / 96 MB video
                if size > cap:
                    fh.close()
                    os.remove(dest)
                    raise web.HTTPRequestEntityTooLarge(max_size=cap, actual_size=size)
                fh.write(chunk)
        # `path` is what the bot uploads to Discord (relative — resolved against
        # the data dir at send time, so configs stay portable and the page never
        # learns the install path); `url` is for the UI preview.
        return web.json_response({"path": f"images/{name}", "url": f"/images/{name}"})

    async def set_avatar(request):
        await guard(request)
        reader = await request.multipart()
        data = b""
        while True:
            field = await reader.next()
            if field is None:
                break
            if field.name == "file":
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > 8 * 1024 * 1024:
                        raise web.HTTPRequestEntityTooLarge(max_size=8 * 1024 * 1024, actual_size=len(data))
        if not data:
            raise web.HTTPBadRequest(text="no image received")
        try:
            await botmgr.set_avatar(data)
        except RuntimeError as e:
            raise web.HTTPBadRequest(text=str(e))
        except Exception as e:  # noqa: BLE001 — discord HTTPException (rate limit / bad image)
            engine._log("error", f"set avatar failed: {e}")
            raise web.HTTPBadRequest(text=f"Discord rejected it ({e}). Avatar changes are rate-limited — wait a bit and try again.")
        engine._log("bot", "bot avatar updated")
        return web.json_response({"ok": True})

    async def serve_image(request):
        name = os.path.basename(request.match_info["name"])
        fp = os.path.join(IMAGES_DIR, name)
        if not os.path.exists(fp):
            raise web.HTTPNotFound()
        return web.FileResponse(fp)

    app.add_routes([
        web.get("/", index),
        web.get("/api/state", get_state),
        web.get("/api/guilds", get_guilds),
        web.get("/images/{name}", serve_image),
        web.post("/api/config", set_config),
        web.post("/api/vendors", set_vendors),
        web.post("/api/remote-access", set_remote_access),
        web.post("/api/listener", set_listener),
        web.post("/api/mock", set_mock),
        web.post("/api/reconnect", reconnect),
        web.post("/api/command-toggle", command_toggle),
        web.post("/api/mode-toggle", mode_toggle),
        web.post("/api/token", set_token),
        web.post("/api/token/reveal", reveal_token),
        web.post("/api/devices/import", import_pumpdirect),
        web.post("/api/devices/discover", discover),
        web.post("/api/devices/probe", probe_device),
        web.post("/api/devices/add", add_device),
        web.post("/api/devices/remove", remove_device),
        web.post("/api/devices/active", set_active),
        web.post("/api/devices/test", test_device),
        web.post("/api/devices/on", device_on),
        web.post("/api/devices/off", device_off),
        web.post("/api/devices/type", set_device_type),
        web.post("/api/devices/rename", rename_device),
        web.post("/api/devices/repoint", repoint_device),
        web.post("/api/devices/calibration", set_calibration),
        web.post("/api/upload", upload),
        web.post("/api/discord/avatar", set_avatar),
        web.post("/api/abort", abort),
        web.post("/api/capacity", set_capacity),
        web.post("/api/reset-users", reset_users),
        web.post("/api/reset-session-leaderboard", reset_session_leaderboard),
        web.post("/api/control/leaderboard-life", control_leaderboard_life),
        web.post("/api/reset-lifetime", reset_lifetime),
        web.post("/api/session-reset", session_reset),
        web.post("/api/control/roll", control_roll),
        web.post("/api/control/pump", control_pump),
        web.post("/api/control/pump-stop", control_pump_stop),
        web.post("/api/control/stop", control_stop),
        web.post("/api/control/resume", control_resume),
        web.post("/api/control/end-intro", end_intro),
        web.post("/api/control/poll", control_poll),
        web.post("/api/control/capacity", control_capacity),
        web.post("/api/control/leaderboard", control_leaderboard),
        web.post("/api/control/broadcast", control_broadcast),
        web.post("/api/control/cleanup", control_cleanup),
        web.post("/api/config/export", export_config),
        web.post("/api/config/import", import_config),
        web.post("/api/gameplay/export", export_gameplay),
        web.post("/api/gameplay/import", import_gameplay),
        web.post("/api/gameplay/preset", gameplay_preset),
        web.post("/api/chat/channels", chat_channels),
        web.post("/api/chat/log", chat_log),
        web.post("/api/chat/send", chat_send),
        web.post("/api/camera/status", camera_status),
        web.post("/api/camera/start", camera_start),
        web.post("/api/camera/stop", camera_stop),
        web.post("/api/camera/mirror", camera_mirror),
        web.post("/api/camera/freeze", camera_freeze),
        web.post("/api/camera/detect", camera_detect),
        web.post("/api/camera/driver", camera_driver),
        web.get("/api/camera/preview", camera_preview),
        web.post("/api/overlay/fire", overlay_fire),
        web.post("/api/overlay/clear", overlay_clear),
        web.get("/stage", stage_page),
        web.post("/api/stage/active", stage_active),
        web.post("/api/stage/done", stage_done),
        web.get("/api/stage/media/{name}", stage_media),
        web.post("/api/sync/export", sync_export),
        web.post("/api/sync/pull", sync_pull),
        web.post("/api/sync/push", sync_push),
        web.post("/api/media/list", media_list),
        web.post("/api/media/delete", media_delete),
        web.post("/api/scene-design", scene_design),
        web.get("/api/scene-design/export", scene_export),
        web.post("/api/scene-design/import", scene_import),
        web.post("/api/check-updates", check_updates),
        web.post("/api/pull-updates", pull_updates),
        web.post("/api/repair-repo", repair_repo),
    ])
    return app


def _num(v):
    try:
        n = float(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
async def main() -> None:
    cfg = config_store.load()
    engine = Engine()
    engine.set_config(cfg)
    engine.start()

    botmgr = BotManager(engine, config_store.load)
    engine.announce_cb = botmgr.announce  # milestone / image posting
    engine.cancel_games_cb = botmgr.cancel_all_games  # session pause kills live games
    engine.embed_cb = botmgr.post_embed               # polls post as rich embeds
    engine.broadcast_embed_cb = botmgr.post_broadcast_embed  # broadcast actions post as embeds
    engine.comp_embed_cb = botmgr.post_competition_embed      # competitions post an Enter Challenge embed
    engine.winner_button_cb = botmgr.post_winner_button       # Winner Button posts a one-press prize embed
    engine.bonus_round_cb = botmgr.post_bonus_round_embed      # Bonus Round posts a teamwork confirm embed
    engine.owner_say_cb = botmgr.owner_broadcast               # #owner-command rows speak with the owner's skin
    # (engine.overlay_cb is wired inside build_app — the overlay router lives
    # there so /api/overlay/fire and action rows share one path.)

    async def _end_session(post_off_message: bool = False):
        # Deactivate. End Sequence calls this WITHOUT the off-message;
        # the end_session action calls it WITH (same text + footer as the
        # manual OFF switch). Deactivate first so [uptime] renders frozen.
        cfg2 = config_store.update({"listener_enabled": False})
        engine.set_config(cfg2)
        if post_off_message:
            msg = (cfg2.get("listener_message_off") or "")
            if msg.strip():
                footer = f"-# DiscoFlate v{VERSION} by AireGasm"
                await botmgr.announce(f"{engine.render(msg.strip())}\n{footer}", None)
    engine.end_session_cb = _end_session
    if cfg.get("discord_token"):
        await botmgr.ensure(cfg["discord_token"])

    net: dict = {}
    app = build_app(engine, botmgr, net)
    runner = web.AppRunner(app)
    await runner.setup()
    ra = cfg.get("remote_access") or {}
    bind = "0.0.0.0" if ra.get("enabled") else HOST
    try:
        site = web.TCPSite(runner, bind, PORT)
        await site.start()
    except OSError as e:
        if bind == HOST:
            raise
        print(f"!! couldn't bind {bind}:{PORT} ({e}) — falling back to loopback only")
        bind = HOST
        site = web.TCPSite(runner, bind, PORT)
        await site.start()
    net.update({"runner": runner, "site": site, "host": bind})
    if bind == HOST:
        print(f"DiscoFlate UI  →  http://{HOST}:{PORT}   (loopback only)")
    else:
        lan = ", ".join(f"http://{ip}:{PORT}" for ip in _lan_ips()) or "(no LAN address found)"
        print(f"DiscoFlate UI  →  http://{HOST}:{PORT}   + LAN: {lan}  (IP whitelist enforced)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # POSIX: clean SIGINT/SIGTERM handling. Windows doesn't support
    # add_signal_handler, so there Ctrl+C surfaces as CancelledError /
    # KeyboardInterrupt — the finally below runs the safety shutdown either way.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError, RuntimeError):
            # NotImplementedError: Windows. ValueError/RuntimeError: not the main
            # thread (e.g. Android, where the server runs on a background thread).
            pass
    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("\nshutting down — forcing device off …")
        try:
            await engine.stop()      # aborts fires → forces the active device OFF
        except Exception:
            pass
        try:
            await botmgr.stop()
        except Exception:
            pass
        try:
            await runner.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
