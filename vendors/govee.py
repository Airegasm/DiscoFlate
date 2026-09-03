"""Govee smart-plug driver for DiscoFlate.

Talks to the Govee cloud "Developer API" (openapi.api.govee.com) for device
discovery and on/off control. This mirrors PumpDirect's
``services/govee-service.js`` implementation and protocol exactly.

Stdlib + aiohttp only (uuid, json, aiohttp) so it runs under Chaquopy on
Android -- no cryptography / pycryptodome / requests.

WARNING: UNVERIFIED against real Govee hardware. The request/response shapes
are transcribed from the PumpDirect JS service, not confirmed on-device.
"""

from __future__ import annotations

import json
import uuid

import aiohttp

GOVEE_API_BASE = "https://openapi.api.govee.com"

ON_OFF_TYPE = "devices.capabilities.on_off"
POWER_SWITCH_INSTANCE = "powerSwitch"


def _request_id() -> str:
    """Fresh request id for each POST body (matches JS generateUUID)."""
    return str(uuid.uuid4())


async def _request(method: str, endpoint: str, creds: dict, body: dict | None = None) -> dict:
    """Perform a Govee API call and return the decoded JSON.

    Raises a clear Exception when the API key is missing, the HTTP status is
    not OK, or the top-level ``code`` is not 200.
    """
    api_key = (creds or {}).get("apiKey")
    if not api_key:
        raise Exception("Govee API key not configured")

    headers = {
        "Content-Type": "application/json",
        "Govee-API-Key": api_key,
    }

    url = f"{GOVEE_API_BASE}{endpoint}"
    data = json.dumps(body).encode("utf-8") if body is not None else None

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.request(method, url, headers=headers, data=data) as response:
            text = await response.text()
            if response.status < 200 or response.status >= 300:
                raise Exception(f"Govee API error: {response.status} - {text}")

            try:
                payload = json.loads(text) if text else {}
            except ValueError as exc:
                raise Exception(f"Govee API error: invalid JSON response - {text}") from exc

    code = payload.get("code")
    if code != 200:
        message = payload.get("message", "unknown error")
        raise Exception(f"Govee API error: code {code} - {message}")

    return payload


async def set_state(device: dict, on: bool, creds: dict) -> None:
    """Turn ``device`` ON (on=True) or OFF (on=False).

    device uses ``device["device_id"]`` and ``device["sku"]``.
    """
    capability = {
        "type": ON_OFF_TYPE,
        "instance": POWER_SWITCH_INSTANCE,
        "value": 1 if on else 0,
    }
    body = {
        "requestId": _request_id(),
        "payload": {
            "sku": device["sku"],
            "device": device["device_id"],
            "capability": capability,
        },
    }
    await _request("POST", "/router/api/v1/device/control", creds, body)


async def get_state(device: dict, creds: dict):
    """Return the power state of ``device``.

    True == on, False == off, None == unknown (no matching capability found).
    """
    body = {
        "requestId": _request_id(),
        "payload": {
            "sku": device["sku"],
            "device": device["device_id"],
        },
    }
    response = await _request("POST", "/router/api/v1/device/state", creds, body)

    payload = response.get("payload") or {}
    capabilities = payload.get("capabilities") or []
    for cap in capabilities:
        if cap.get("type") == ON_OFF_TYPE and cap.get("instance") == POWER_SWITCH_INSTANCE:
            state = cap.get("state") or {}
            return state.get("value") == 1

    return None


async def discover(creds: dict) -> list[dict]:
    """List all Govee devices reachable with the given API key.

    Returns dicts of the form::

        {"label": <deviceName or device_id>, "vendor": "govee",
         "device_id": <device>, "sku": <sku>}
    """
    response = await _request("GET", "/router/api/v1/user/devices", creds)

    devices = response.get("data") or []
    result: list[dict] = []
    for entry in devices:
        device_id = entry.get("device")
        result.append(
            {
                "label": entry.get("deviceName") or device_id,
                "vendor": "govee",
                "device_id": device_id,
                "sku": entry.get("sku"),
            }
        )

    return result
