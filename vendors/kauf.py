"""Kauf smart-plug driver for DiscoFlate.

Kauf plugs run ESPHome firmware. This controls them locally via the ESPHome
web-server REST API (port 80), which must be enabled on the plug (`web_server`).
The switch's object id defaults to ``relay`` — set the device's ``entity`` field
if yours differs. Optional web-server auth via the Kauf credentials
(web_username / web_password).

Stdlib + aiohttp only (Chaquopy-safe). Verified working on real Kauf plugs (2026-09).
"""
from __future__ import annotations

import asyncio
import json

import aiohttp


def _base(device: dict) -> str:
    host = (device.get("host") or "").strip()
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    return host.rstrip("/")


def _entity(device: dict) -> str:
    return (device.get("entity") or "relay").strip()


def _auth(creds: dict):
    user = (creds or {}).get("web_username") or ""
    pwd = (creds or {}).get("web_password") or ""
    return aiohttp.BasicAuth(user, pwd) if user else None


async def set_state(device: dict, on: bool, creds: dict) -> None:
    action = "turn_on" if on else "turn_off"
    base = _base(device)
    url = f"{base}/switch/{_entity(device)}/{action}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, auth=_auth(creds),
                              timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 401:
                    raise Exception(f"Kauf 401 at {base}: web server needs auth — "
                                    f"fill the Kauf web username/password in credentials.")
                if r.status == 404:
                    raise Exception(f"Kauf 404 at {base}: no switch named '{_entity(device)}' — "
                                    f"open {base} in a browser to see the real switch id and set it as the device's entity.")
                if r.status >= 300:
                    raise Exception(f"Kauf {r.status}: {(await r.text())[:200]}")
    except aiohttp.ClientConnectorError as e:
        # TCP refused / host unreachable — nothing is serving HTTP at that host:port.
        raise Exception(
            f"can't reach an ESPHome web server at {base} — the plug refused the connection. "
            f"Enable `web_server:` on the plug (it's off by default in ESPHome) and confirm the host/port. "
            f"Test by opening {base} in a browser. [{e.__class__.__name__}]") from e
    except (aiohttp.ServerTimeoutError, asyncio.TimeoutError) as e:
        raise Exception(
            f"the ESPHome web server at {base} didn't respond in time — check the IP and that the plug is online. "
            f"[{e.__class__.__name__}]") from e


async def get_state(device: dict, creds: dict):
    url = f"{_base(device)}/switch/{_entity(device)}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, auth=_auth(creds),
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status >= 300:
                    return None
                data = json.loads(await r.text())
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data.get("value"), bool):
        return data["value"]
    st = data.get("state")
    if isinstance(st, str):
        return st.strip().upper() in ("ON", "TRUE", "1")
    return None


async def discover(creds: dict) -> list[dict]:
    # ESPHome advertises over mDNS; not implemented here — add by IP.
    return []
