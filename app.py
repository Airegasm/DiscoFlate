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
import json
import os
import signal
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

# App version — keep in sync with android versionCode + version.json in the repo.
VERSION = "1.7"
VERSION_CODE = 21
VERSION_URL = "https://raw.githubusercontent.com/Airegasm/DiscoFlate/main/version.json"

# Config keys "Restore Default Config" touches (game content). Everything else —
# token, devices, listen targets, operator, vendors, etc. — is left untouched.
_DEFAULT_SCALAR_KEYS = ["roll", "command_names", "capacity_message", "pumptimer_message",
                        "pump_message", "cooldown_message", "cooldown_reset_message",
                        "system_buffer_seconds", "cooldown_seconds", "roll_enabled",
                        "max_roll_prize", "auto_report", "listener_message_on", "listener_message_off"]
# list key -> identity function (default items win on a key match; user extras kept)
_DEFAULT_LIST_KEYS = {
    "commands": lambda c: (c.get("name") or "").strip().lower(),
    "prizes": lambda p: (p.get("command") or "").strip().lower(),
    "modes": lambda m: (m.get("name") or "").strip().lower(),
    "events": lambda e: (e.get("name") or "").strip().lower(),
    "capacity_ranges": lambda r: f"{r.get('min')}-{r.get('max')}",
}


def _merge_defaults(cur: dict, dflt: dict) -> dict:
    """Overlay the shipped default game content ON TOP of the current config:
    scalar/message keys reset to default; list keys become default-items +
    any user-added items the default doesn't have. Connection/personal keys
    (not listed) are never touched."""
    out = dict(cur)
    for k in _DEFAULT_SCALAR_KEYS:
        if k in dflt:
            out[k] = dflt[k]
    for k, keyfn in _DEFAULT_LIST_KEYS.items():
        dlist = list(dflt.get(k) or [])
        seen = {keyfn(x) for x in dlist}
        extras = [x for x in (cur.get(k) or []) if keyfn(x) not in seen]
        merged = dlist + extras
        if k == "capacity_ranges":
            merged.sort(key=lambda r: (r.get("min", 0), r.get("max", 0)))
        out[k] = merged
    return out


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _public_state(engine: Engine, botmgr: BotManager) -> dict:
    cfg = config_store.load()
    snap = engine.snapshot()
    return {
        **snap,
        "prefix": cfg.get("command_prefix", "!"),
        "command_names": cfg.get("command_names", {}),
        "capacity_message": cfg.get("capacity_message", ""),
        "pumptimer_message": cfg.get("pumptimer_message", ""),
        "roll_enabled": cfg.get("roll_enabled", True),
        "system_buffer_seconds": cfg.get("system_buffer_seconds", 8),
        "cooldown_message": cfg.get("cooldown_message", ""),
        "cooldown_reset_message": cfg.get("cooldown_reset_message", ""),
        "roll": cfg.get("roll", {}),
        "max_roll_prize": cfg.get("max_roll_prize", {}),
        "prizes": cfg.get("prizes", []),
        "capacity_ranges": cfg.get("capacity_ranges", []),
        "commands": cfg.get("commands", []),
        "modes": cfg.get("modes", []),
        "events": cfg.get("events", []),
        "devices": cfg.get("devices", []),
        "active_device_id": cfg.get("active_device_id"),
        "vendors": cfg.get("vendors", {}),
        "allow": cfg.get("allow", {}),
        "listen_guild_id": cfg.get("listen_guild_id", ""),
        "listen_channel_id": cfg.get("listen_channel_id", ""),
        "listen_targets": cfg.get("listen_targets", []),
        "anon_user_label": cfg.get("anon_user_label", ""),
        "allow_dms": cfg.get("allow_dms", False),
        "server_channels": cfg.get("server_channels", {}),
        "invite_url": botmgr.invite_url(),
        "cooldown_seconds": cfg.get("cooldown_seconds", 0),
        "cooldown_exempt_user_ids": cfg.get("cooldown_exempt_user_ids", []),
        "cooldown_exempt_names": cfg.get("cooldown_exempt_names", []),
        "operator_name": cfg.get("operator_name", ""),
        "listener_message_on": cfg.get("listener_message_on", ""),
        "listener_message_off": cfg.get("listener_message_off", ""),
        "auto_report": cfg.get("auto_report", {}),
        "announce_channel_id": cfg.get("announce_channel_id", ""),
        "pumpdirect_path": cfg.get("pumpdirect_path"),
        "has_token": bool(cfg.get("discord_token")),
        "bot_error": botmgr.last_error,
        "silence_onoff_log": cfg.get("silence_onoff_log", False),
        "mock_mode": cfg.get("mock_mode", False),
        "mock_calibration_seconds_to_100": cfg.get("mock_calibration_seconds_to_100", 60),
    }


async def _json(request: web.Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def _origin_ok(request: web.Request) -> bool:
    # Loopback bind already blocks remote hosts; also reject cross-origin POSTs
    # so a malicious page in the user's browser can't drive the API via CSRF.
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    return origin in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")


# --------------------------------------------------------------------------- #
# route factory
# --------------------------------------------------------------------------- #
def build_app(engine: Engine, botmgr: BotManager) -> web.Application:
    app = web.Application()

    async def index(request):
        return web.FileResponse(os.path.join(WEB_DIR, "index.html"))

    async def get_state(request):
        return web.json_response(_public_state(engine, botmgr))

    async def get_guilds(request):
        return web.json_response(botmgr.list_guilds())

    async def guard(request):
        if not _origin_ok(request):
            raise web.HTTPForbidden(text="bad origin")

    # ---- config -----------------------------------------------------------
    async def set_config(request):
        await guard(request)
        body = await _json(request)
        patch = {}
        for key in ("command_prefix", "command_names", "capacity_message",
                    "roll_enabled", "system_buffer_seconds", "cooldown_message", "cooldown_reset_message", "pumptimer_message", "pump_message",
                    "roll", "max_roll_prize", "prizes", "capacity_ranges", "commands", "modes", "events",
                    "allow", "pumpdirect_path", "cooldown_seconds",
                    "cooldown_exempt_user_ids", "cooldown_exempt_names", "operator_name", "auto_report",
                    "announce_channel_id", "listen_guild_id", "listen_channel_id",
                    "listen_targets", "anon_user_label",
                    "allow_dms", "server_channels", "vendors", "silence_onoff_log",
                    "mock_calibration_seconds_to_100",
                    "listener_message_on", "listener_message_off"):
            if key in body:
                patch[key] = body[key]
        cfg = config_store.update(patch)
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
            await botmgr.announce(engine.render(msg.strip()), None)
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
    async def fire(request):
        await guard(request)
        b = await _json(request)
        secs = _num(b.get("seconds")) or 3.0
        res = await engine.fire(secs, "web test-fire")
        return web.json_response(res)

    async def roll(request):
        await guard(request)
        res = await engine.roll_and_fire("web")
        return web.json_response(res)

    async def abort(request):
        await guard(request)
        await engine.abort("web")
        return web.json_response({"ok": True})

    async def reset(request):
        await guard(request)
        engine.reset_capacity()
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

    async def control_capacity(request):
        await guard(request)
        return web.json_response(await botmgr.operator_broadcast_capacity())

    async def control_leaderboard(request):
        await guard(request)
        return web.json_response(await botmgr.operator_broadcast_leaderboard())

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
            ok = out.returncode == 0
            return web.json_response({"ok": ok, "output": (out.stdout + out.stderr).strip()[:2000],
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
        # `path` is what the bot uploads to Discord; `url` is for the UI preview.
        return web.json_response({"path": dest, "url": f"/images/{name}"})

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
        web.post("/api/fire", fire),
        web.post("/api/roll", roll),
        web.post("/api/abort", abort),
        web.post("/api/reset", reset),
        web.post("/api/capacity", set_capacity),
        web.post("/api/reset-users", reset_users),
        web.post("/api/session-reset", session_reset),
        web.post("/api/control/roll", control_roll),
        web.post("/api/control/pump", control_pump),
        web.post("/api/control/stop", control_stop),
        web.post("/api/control/capacity", control_capacity),
        web.post("/api/control/leaderboard", control_leaderboard),
        web.post("/api/restore-defaults", restore_defaults),
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

    async def _end_session():
        # Deactivate WITHOUT posting the activation-off message (End Sequence).
        cfg2 = config_store.update({"listener_enabled": False})
        engine.set_config(cfg2)
    engine.end_session_cb = _end_session
    if cfg.get("discord_token"):
        await botmgr.ensure(cfg["discord_token"])

    app = build_app(engine, botmgr)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    print(f"DiscoFlate UI  →  http://{HOST}:{PORT}   (loopback only)")

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
