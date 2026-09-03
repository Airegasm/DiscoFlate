"""Tuya Cloud smart-plug driver for DiscoFlate.

Talks to the Tuya Cloud "IoT Core" OpenAPI (openapi.tuya*.com) for on/off
control and status. This mirrors PumpDirect's ``services/tuya-service.js``
implementation and protocol exactly -- in particular the HMAC-SHA256 request
signing (the ``stringToSign`` layout, the sign-key ordering for token vs.
authenticated requests, and the byte-for-byte body hashing).

Stdlib crypto only (hashlib, hmac, json, time) plus aiohttp, so it runs under
Chaquopy on Android -- no cryptography / pycryptodome / requests.

Tuya has no LAN discovery here; device IDs are entered manually, so
``discover()`` returns ``[]`` (matching PumpDirect).

Verified working on real Tuya/Geeni/Smart Life plugs (2026-09).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import aiohttp

TUYA_REGIONS = {
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}

# Module-level access-token cache keyed by accessId:
#   {accessId: {"token": <str>, "expiry": <float epoch seconds>}}
# Matches the JS instance cache (token reused until expire_time - 300s).
_TOKEN_CACHE: dict[str, dict] = {}


def _base_url(region: str) -> str:
    """Region base URL, defaulting to US (matches JS getBaseUrl)."""
    return TUYA_REGIONS.get(region, TUYA_REGIONS["us"])


def _sign(secret: str, payload: str) -> str:
    """HMAC-SHA256(secret, payload) as UPPERCASE hex (matches JS sign())."""
    return (
        hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
        .hexdigest()
        .upper()
    )


def _sha256_hex(payload: str) -> str:
    """SHA-256 of ``payload`` as lowercase hex (matches JS content hashing)."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now_ms() -> str:
    """Milliseconds since epoch as a string (matches JS Date.now().toString())."""
    return str(int(time.time() * 1000))


async def _get_access_token(session: aiohttp.ClientSession, creds: dict) -> str:
    """Return a valid access token, fetching a new one when the cache is stale.

    Sign order (per JS getAccessToken): ``accessId + t + stringToSign`` where
    ``stringToSign = method \\n sha256('') \\n '' \\n path``.
    """
    access_id = creds["accessId"]
    access_secret = creds["accessSecret"]
    region = creds.get("region", "us")

    cached = _TOKEN_CACHE.get(access_id)
    if cached and time.time() < cached["expiry"]:
        return cached["token"]

    t = _now_ms()
    method = "GET"
    sign_url = "/v1.0/token?grant_type=1"

    content_hash = _sha256_hex("")
    string_to_sign = "\n".join([method, content_hash, "", sign_url])
    sign_str = access_id + t + string_to_sign
    signature = _sign(access_secret, sign_str)

    headers = {
        "t": t,
        "sign": signature,
        "client_id": access_id,
        "sign_method": "HMAC-SHA256",
    }

    url = _base_url(region) + sign_url
    async with session.get(url, headers=headers) as response:
        text = await response.text()
        try:
            data = json.loads(text) if text else {}
        except ValueError as exc:
            raise Exception(
                f"Tuya auth error: invalid JSON response (HTTP {response.status}) - {text}"
            ) from exc

        if not data.get("success"):
            raise Exception(
                f"Tuya auth error: {data.get('msg')} "
                f"(code: {data.get('code')}, HTTP {response.status})"
            )

    result = data.get("result") or {}
    token = result["access_token"]
    expire_time = result["expire_time"]  # seconds

    _TOKEN_CACHE[access_id] = {
        "token": token,
        "expiry": time.time() + (expire_time - 300),
    }
    return token


async def _request(
    session: aiohttp.ClientSession,
    creds: dict,
    method: str,
    path: str,
    body: dict | None = None,
):
    """Perform a signed, authenticated Tuya API call and return ``result``.

    Sign order (per JS request): ``accessId + access_token + t + stringToSign``
    where ``stringToSign = method \\n sha256(bodyStr) \\n '' \\n path`` and
    ``bodyStr`` is '' for GET else the exact JSON string that is sent. There is
    NO nonce.
    """
    access_id = creds["accessId"]
    access_secret = creds["accessSecret"]
    region = creds.get("region", "us")

    token = await _get_access_token(session, creds)
    t = _now_ms()

    is_get = method == "GET"
    # Hash exactly what we send, byte-for-byte.
    body_str = "" if is_get else json.dumps(body, separators=(",", ":"))
    content_hash = _sha256_hex(body_str)
    string_to_sign = "\n".join([method, content_hash, "", path])
    sign_str = access_id + token + t + string_to_sign
    signature = _sign(access_secret, sign_str)

    headers = {
        "t": t,
        "sign": signature,
        "client_id": access_id,
        "sign_method": "HMAC-SHA256",
        "access_token": token,
        "Content-Type": "application/json",
    }

    url = _base_url(region) + path
    data = None if is_get else body_str.encode("utf-8")

    async with session.request(method, url, headers=headers, data=data) as response:
        text = await response.text()
        try:
            payload = json.loads(text) if text else {}
        except ValueError as exc:
            raise Exception(
                f"Tuya API error: invalid JSON response (HTTP {response.status}) - {text}"
            ) from exc

        if not payload.get("success"):
            raise Exception(
                f"Tuya API error: {payload.get('msg')} "
                f"(code: {payload.get('code')}, HTTP {response.status})"
            )

    return payload.get("result")


async def set_state(device: dict, on: bool, creds: dict) -> None:
    """Turn ``device`` ON (on=True) or OFF (on=False).

    device uses ``device["device_id"]``. Sends both ``switch_1`` and ``switch``
    codes (matches JS turnOn/turnOff) to cover single- and multi-gang plugs.
    """
    value = bool(on)
    body = {
        "commands": [
            {"code": "switch_1", "value": value},
            {"code": "switch", "value": value},
        ]
    }
    path = f"/v1.0/iot-03/devices/{device['device_id']}/commands"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        await _request(session, creds, "POST", path, body)


async def get_state(device: dict, creds: dict):
    """Return the power state of ``device``.

    True == on, False == off, None == unknown (no matching switch code found).
    Reads ``.result[]`` and returns the first ``switch_1``/``switch`` value as a
    bool (matches JS getPowerState).
    """
    path = f"/v1.0/devices/{device['device_id']}/status"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        result = await _request(session, creds, "GET", path)

    for item in result or []:
        if item.get("code") in ("switch_1", "switch"):
            return bool(item.get("value"))

    return None


async def discover(creds: dict) -> list[dict]:
    """Tuya has no LAN discovery here; device IDs are entered manually.

    Returns ``[]`` to match PumpDirect (listDevices only works against known,
    manually-added IDs).
    """
    return []
