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
import fnmatch
import ipaddress
import json
import os
import signal
import socket
import subprocess
import uuid

import aiohttp
from aiohttp import web

import config_store
import pumpdirect_import
import kasa_legacy as kasa
import device_control
from engine import Engine
from discord_bot import BotManager

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.environ.get("DISCOFLATE_WEB_DIR") or os.path.join(HERE, "web")
IMAGES_DIR = os.path.join(config_store.DATA_DIR, "images")
# Shipped default game config (for "Restore Default Config"). On Android the host
# copies the bundled seed to a pristine path and points this env at it.
DEFAULT_CONFIG_PATH = os.environ.get("DISCOFLATE_DEFAULT_CONFIG") or os.path.join(HERE, "default_config.json")
PORT = int(os.getenv("DISCOFLATE_PORT", "8765"))
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
                        "pump_message", "cooldown_message", "cooldown_reset_message",
                        "system_buffer_seconds", "cooldown_seconds", "roll_enabled",
                        "max_roll_prize", "auto_report", "listener_message_on", "listener_message_off",
                        "pause_message", "resume_message", "paused_notice_message",
                        "always_on_enabled"]
# list key -> identity function. Restore KEEPS everything you already have (edited
# defaults + customs) and only ADDS shipped items whose key is missing.
_DEFAULT_LIST_KEYS = {
    "commands": lambda c: (c.get("name") or "").strip().lower(),
    "prizes": lambda p: (p.get("command") or "").strip().lower(),
    "modes": lambda m: (m.get("name") or "").strip().lower(),
    "events": lambda e: (e.get("name") or "").strip().lower(),
    "capacity_events": lambda e: ((e.get("name") or "").strip() or str(e.get("at") or "")).lower(),
    "polls": lambda p: (p.get("name") or "").strip().lower(),
    "capacity_ranges": lambda r: f"{r.get('min')}-{r.get('max')}",
    "always_on_commands": lambda a: (a.get("name") if isinstance(a, dict) else str(a) or "").strip().lower(),
}


# ── Shareable "gameplay settings": EVERYTHING on the Game, Commands, Events,
# and Templates tabs — the safe data people can trade. Deliberately EXCLUDES
# everything personal/connection: discord_token, devices, vendors (creds),
# listen targets / server IDs / announce channel, allow-lists, cooldown-exempt
# names/IDs, operator name, mock/pumpdirect/runtime state.
_GAMEPLAY_KEYS = [
    # Commands tab
    "command_prefix", "command_names", "roll", "roll_enabled", "system_buffer_seconds",
    "capacity_message", "pumptimer_message", "pump_message",
    "cooldown_message", "cooldown_reset_message",
    "commands", "broadcasts", "modes", "max_roll_prize", "prizes",
    # Game tab
    "cooldown_seconds", "auto_report",
    "listener_message_on", "listener_message_off",
    "pause_message", "resume_message", "paused_notice_message",
    "output_headers", "rich_output",
    "capacity_ranges", "always_on_enabled", "always_on_commands",
    # Events tab
    "events", "capacity_events", "polls",
    "event_in_process_message", "event_cooldown_message",
    # Templates tab
    "templates",
]
# List keys → identity fn, for "add missing only" additive merge (by name/key).
_GAMEPLAY_LIST_KEYS = {
    "commands": lambda c: (c.get("name") or "").strip().lower(),
    "broadcasts": lambda b: (b.get("name") or "").strip().lower(),
    "modes": lambda m: (m.get("name") or "").strip().lower(),
    "prizes": lambda p: (p.get("command") or "").strip().lower(),
    "events": lambda e: (e.get("name") or "").strip().lower(),
    "capacity_events": lambda e: ((e.get("name") or "").strip() or str(e.get("at") or "")).lower(),
    "polls": lambda p: (p.get("name") or "").strip().lower(),
    "capacity_ranges": lambda r: f"{r.get('min')}-{r.get('max')}",
    "always_on_commands": lambda a: (a.get("name") if isinstance(a, dict) else str(a) or "").strip().lower(),
}


def _gp_blank(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _gameplay_export(cfg: dict) -> dict:
    return {k: cfg[k] for k in _GAMEPLAY_KEYS if k in cfg}


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
        t = dict(cur.get("templates") or {"commands": [], "events": [], "ranges": []})
        for sub in ("commands", "events", "ranges"):
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
    cfg = config_store.load()
    snap = engine.snapshot()
    return {
        **snap,
        "prefix": cfg.get("command_prefix", "!"),
        "command_names": cfg.get("command_names", {}),
        "capacity_message": cfg.get("capacity_message", ""),
        "pumptimer_message": cfg.get("pumptimer_message", ""),
        "pump_message": cfg.get("pump_message", ""),
        "roll_enabled": cfg.get("roll_enabled", True),
        "system_buffer_seconds": cfg.get("system_buffer_seconds", 8),
        "cooldown_message": cfg.get("cooldown_message", ""),
        "cooldown_reset_message": cfg.get("cooldown_reset_message", ""),
        "roll": cfg.get("roll", {}),
        "max_roll_prize": cfg.get("max_roll_prize", {}),
        "prizes": cfg.get("prizes", []),
        "capacity_ranges": cfg.get("capacity_ranges", []),
        "commands": cfg.get("commands", []),
        "always_on_enabled": cfg.get("always_on_enabled", False),
        "always_on_commands": cfg.get("always_on_commands", []),
        "modes": cfg.get("modes", []),
        "events": cfg.get("events", []),
        "capacity_events": cfg.get("capacity_events", []),
        "polls": cfg.get("polls", []),
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
        "cooldown_seconds": cfg.get("cooldown_seconds", 0),
        "cooldown_exempt_user_ids": cfg.get("cooldown_exempt_user_ids", []),
        "cooldown_exempt_names": cfg.get("cooldown_exempt_names", []),
        "operator_name": cfg.get("operator_name", ""),
        "listener_message_on": cfg.get("listener_message_on", ""),
        "listener_message_off": cfg.get("listener_message_off", ""),
        "pause_message": cfg.get("pause_message", ""),
        "resume_message": cfg.get("resume_message", ""),
        "paused_notice_message": cfg.get("paused_notice_message", ""),
        "auto_report": cfg.get("auto_report", {}),
        "announce_channel_id": cfg.get("announce_channel_id", ""),
        "pumpdirect_path": cfg.get("pumpdirect_path"),
        "has_token": bool(cfg.get("discord_token")),
        "bot_error": botmgr.last_error,
        "config_rev": cfg.get("config_rev", 0),
        "recovered_config": config_store.RECOVERED_FROM,
        "version": VERSION,
        # preset NAMES only (the full data would bloat the 1s state poll)
        "gameplay_presets": [{"name": p.get("name", "")} for p in (cfg.get("gameplay_presets") or [])],
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
SENSITIVE_PATHS = {"/api/config/export", "/api/token", "/api/config/import", "/api/pull-updates"}


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
        resp = web.FileResponse(os.path.join(WEB_DIR, "index.html"))
        resp.set_cookie("df_auth", secret, httponly=True, samesite="Strict", path="/")
        return resp

    async def get_state(request):
        return web.json_response(_public_state(engine, botmgr))

    async def get_guilds(request):
        return web.json_response(botmgr.list_guilds())

    async def guard(request):
        if not _origin_ok(request):
            raise web.HTTPForbidden(text="bad origin")

    # ---- config -----------------------------------------------------------
    # Minimum shape per key — a patch with the wrong container type is refused
    # (a malformed import/tab can't put a string where the engine expects a list).
    _TYPE_FLOOR = {"commands": list, "events": list, "modes": list, "prizes": list,
                   "capacity_events": list, "polls": list,
                   "capacity_ranges": list, "listen_targets": list, "broadcasts": list,
                   "always_on_commands": list, "cooldown_exempt_user_ids": list,
                   "cooldown_exempt_names": list, "command_names": dict, "roll": dict,
                   "max_roll_prize": dict, "auto_report": dict, "templates": dict,
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
                    "roll_enabled", "system_buffer_seconds", "cooldown_message", "cooldown_reset_message", "pumptimer_message", "pump_message",
                    "roll", "max_roll_prize", "prizes", "capacity_ranges", "commands", "modes", "events",
                    "capacity_events", "polls",
                    "allow", "pumpdirect_path", "cooldown_seconds",
                    "cooldown_exempt_user_ids", "cooldown_exempt_names", "operator_name", "auto_report",
                    "announce_channel_id", "listen_guild_id", "listen_channel_id",
                    "listen_targets", "anon_user_label", "output_headers", "rich_output", "templates",
                    "allow_dms", "server_channels", "silence_onoff_log",
                    "mock_calibration_seconds_to_100",
                    "always_on_enabled", "always_on_commands",
                    "event_in_process_message", "event_cooldown_message", "broadcasts",
                    "listener_message_on", "listener_message_off",
                    "pause_message", "resume_message", "paused_notice_message"):
            if key in body:
                want = _TYPE_FLOOR.get(key)
                if want and not isinstance(body[key], want):
                    raise web.HTTPBadRequest(text=f"{key} must be a {want.__name__}")
                patch[key] = body[key]
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
        msg = (cfg.get("listener_message_on") if cfg["listener_enabled"]
               else cfg.get("listener_message_off")) or ""
        if msg.strip():
            # Always append a non-editable attribution footer (Discord subtext).
            footer = f"-# DiscoFlate v{VERSION} by AireGasm"
            await botmgr.announce(f"{engine.render(msg.strip())}\n{footer}", None)
        return web.json_response(_public_state(engine, botmgr))

    async def command_toggle(request):
        await guard(request)
        b = await _json(request)
        name, enabled = (b.get("name") or "").strip().lower(), bool(b.get("enabled"))
        cfg = config_store.load()
        for c in cfg.get("commands", []):
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
        mode = next((m for m in cfg.get("modes", [])
                     if (m.get("name") or "").strip().lower() == name), None)
        if mode is None:
            raise web.HTTPBadRequest(text="mode not found")
        mode["enabled"] = enabled
        members = {str(x).strip().lower() for x in (mode.get("commands") or [])}
        ev_members = {str(x).strip().lower() for x in (mode.get("events") or [])}
        for c in cfg.get("commands", []):
            if (c.get("name") or "").strip().lower() in members:
                c["enabled"] = enabled
        for ev in cfg.get("events", []):
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
            (b.get("who") or "").strip(), _num(b.get("seconds")) or 5.0))

    async def control_stop(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await botmgr.operator_stop((b.get("who") or "").strip()))

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

    async def control_broadcast(request):
        await guard(request)
        b = await _json(request)
        return web.json_response(await botmgr.operator_broadcast_custom((b.get("message") or "")))

    async def restore_defaults(request):
        await guard(request)
        try:
            with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as fh:
                dflt = json.load(fh)
        except (FileNotFoundError, ValueError) as e:
            raise web.HTTPBadRequest(text=f"default config not available: {e}")
        cfg = config_store.save(_merge_defaults(config_store.load(), dflt))
        engine.set_config(cfg)
        return web.json_response(_public_state(engine, botmgr))

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

        if action in ("save", "update", "import"):
            if not name:
                raise web.HTTPBadRequest(text="a preset name is required")
            if action == "import":
                data = b.get("data")
                if not isinstance(data, dict) or len(set(_GAMEPLAY_KEYS) & set(data)) < 2:
                    raise web.HTTPBadRequest(text="that doesn't look like a DiscoFlate gameplay/preset file")
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
            p = _find(name)
            if p is None:
                raise web.HTTPBadRequest(text="no such preset")
            merged = _gameplay_merge(cfg, p.get("data") or {}, "replace")
            cfg = config_store.save(config_store._coerce_numbers(merged))
            engine.set_config(cfg)
        elif action == "delete":
            cfg["gameplay_presets"] = [p for p in presets
                                       if (p.get("name") or "").strip().lower() != name.lower()]
            cfg = config_store.save(cfg)
            engine.set_config(cfg)
        elif action == "export":
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
        merged = _gameplay_merge(config_store.load(), data, mode)
        cfg = config_store.save(config_store._coerce_numbers(merged))
        engine.set_config(cfg)
        return web.json_response(_public_state(engine, botmgr))

    async def check_updates(request):
        await guard(request)
        result = {"current_version": VERSION, "current_code": VERSION_CODE,
                  "android": os.environ.get("DISCOFLATE_DEFAULT_CONFIG") is not None}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(VERSION_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
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

    async def upload(request):
        await guard(request)
        os.makedirs(IMAGES_DIR, exist_ok=True)
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            raise web.HTTPBadRequest(text="expected a 'file' field")
        ext = os.path.splitext(field.filename or "")[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            raise web.HTTPBadRequest(text="unsupported image type")
        name = f"{uuid.uuid4().hex}{ext}"
        dest = os.path.join(IMAGES_DIR, name)
        size = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > 8 * 1024 * 1024:  # 8 MB cap
                    fh.close()
                    os.remove(dest)
                    raise web.HTTPRequestEntityTooLarge(max_size=8 * 1024 * 1024, actual_size=size)
                fh.write(chunk)
        # `path` is what the bot uploads to Discord (relative — resolved against
        # the data dir at send time, so configs stay portable and the page never
        # learns the install path); `url` is for the UI preview.
        return web.json_response({"path": f"images/{name}", "url": f"/images/{name}"})

    async def snapshot(request):
        await guard(request)
        os.makedirs(IMAGES_DIR, exist_ok=True)
        reader = await request.multipart()
        caption = ""
        dest = None
        while True:
            field = await reader.next()
            if field is None:
                break
            if field.name == "caption":
                caption = (await field.text() or "").strip()
            elif field.name == "file":
                dest = os.path.join(IMAGES_DIR, f"snap-{uuid.uuid4().hex}.jpg")
                size = 0
                with open(dest, "wb") as fh:
                    while True:
                        chunk = await field.read_chunk()
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > 8 * 1024 * 1024:
                            fh.close(); os.remove(dest)
                            raise web.HTTPRequestEntityTooLarge(max_size=8 * 1024 * 1024, actual_size=size)
                        fh.write(chunk)
        if dest is None:
            raise web.HTTPBadRequest(text="no image received")
        # Post to every active listen channel, then remove the temp file.
        text = engine.render(caption) if caption else ""
        try:
            await botmgr.broadcast(text, dest)
            engine._log("bot", f"snapshot sent{' + caption' if caption else ''}")
        finally:
            try:
                os.remove(dest)
            except OSError:
                pass
        channels = len(botmgr._targets(config_store.load()))
        return web.json_response({"ok": True, "channels": channels})

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
        web.post("/api/devices/import", import_pumpdirect),
        web.post("/api/devices/discover", discover),
        web.post("/api/devices/add", add_device),
        web.post("/api/devices/remove", remove_device),
        web.post("/api/devices/active", set_active),
        web.post("/api/devices/test", test_device),
        web.post("/api/devices/on", device_on),
        web.post("/api/devices/off", device_off),
        web.post("/api/devices/type", set_device_type),
        web.post("/api/devices/calibration", set_calibration),
        web.post("/api/upload", upload),
        web.post("/api/snapshot", snapshot),
        web.post("/api/abort", abort),
        web.post("/api/capacity", set_capacity),
        web.post("/api/reset-users", reset_users),
        web.post("/api/reset-lifetime", reset_lifetime),
        web.post("/api/session-reset", session_reset),
        web.post("/api/control/roll", control_roll),
        web.post("/api/control/pump", control_pump),
        web.post("/api/control/stop", control_stop),
        web.post("/api/control/resume", control_resume),
        web.post("/api/control/poll", control_poll),
        web.post("/api/control/capacity", control_capacity),
        web.post("/api/control/leaderboard", control_leaderboard),
        web.post("/api/control/broadcast", control_broadcast),
        web.post("/api/restore-defaults", restore_defaults),
        web.post("/api/config/export", export_config),
        web.post("/api/config/import", import_config),
        web.post("/api/gameplay/export", export_gameplay),
        web.post("/api/gameplay/import", import_gameplay),
        web.post("/api/gameplay/preset", gameplay_preset),
        web.post("/api/check-updates", check_updates),
        web.post("/api/pull-updates", pull_updates),
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
