"""Kauf smart-plug driver for DiscoFlate.

Kauf plugs run ESPHome firmware. This controls them locally via the ESPHome
web-server REST API (port 80), which must be enabled on the plug (`web_server`).
The switch's object id defaults to ``relay`` — set the device's ``entity`` field
if yours differs. Optional web-server auth via the Kauf credentials
(web_username / web_password).

Stdlib + aiohttp only (Chaquopy-safe). UNVERIFIED against real hardware.
"""
from __future__ import annotations

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
    url = f"{_base(device)}/switch/{_entity(device)}/{action}"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, auth=_auth(creds),
                          timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status >= 300:
                raise Exception(f"Kauf {r.status}: {(await r.text())[:200]}")


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
