"""Home Assistant smart-plug driver for DiscoFlate.

Talks to a Home Assistant install via its REST API
(https://developers.home-assistant.io/docs/api/rest/). Configured with:
    - baseUrl  e.g. https://homeassistant.local:8123
    - token    long-lived access token (Profile -> Security)

Devices are addressed by entity_id (e.g. switch.aquarium_pump, light.lamp).
set_state dispatches to the matching domain's `turn_on` / `turn_off` service;
get_state reads the entity's normalized state. Anything Home Assistant can
switch -- Zigbee, Z-Wave, Matter, Shelly, MQTT -- becomes addressable without
writing a per-vendor adapter.

This mirrors PumpDirect's services/homeassistant-service.js. It is stdlib +
aiohttp only (no cryptography / pycryptodome / requests) so it runs under
Chaquopy on Android. UNVERIFIED against real hardware.
"""

import aiohttp


def _base_url(creds: dict) -> str:
    """Return the configured base URL with any trailing slashes stripped."""
    base = (creds.get("baseUrl") or "").strip().rstrip("/")
    if not base:
        raise Exception("Home Assistant not configured (missing baseUrl)")
    return base


def _headers(creds: dict) -> dict:
    """Return the auth + content-type headers for a Home Assistant request."""
    token = (creds.get("token") or "").strip()
    if not token:
        raise Exception("Home Assistant not configured (missing token)")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _domain_of(entity_id: str) -> str:
    """Extract the <domain> from a <domain>.<name> entity_id."""
    if not entity_id or not isinstance(entity_id, str) or "." not in entity_id:
        raise Exception(
            f"HA entity_id must be of the form <domain>.<name>, got: {entity_id}"
        )
    return entity_id.split(".")[0]


async def _request(method: str, url: str, headers: dict, body=None):
    """Perform one HTTP request; raise a clear Exception on non-2xx."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.request(method, url, headers=headers, json=body) as res:
                if not (200 <= res.status < 300):
                    txt = ""
                    try:
                        txt = await res.text()
                    except Exception:
                        txt = ""
                    raise Exception(
                        f"HA {res.status} {res.reason}: {txt[:200]}"
                    )
                try:
                    return await res.json(content_type=None)
                except Exception:
                    return None
    except aiohttp.ClientError as e:
        raise Exception(f"HA fetch failed: {e}")


async def set_state(device: dict, on: bool, creds: dict) -> None:
    """Turn the entity on (on=True) or off. Raise Exception on failure."""
    entity_id = device["entity_id"]
    domain = _domain_of(entity_id)
    base = _base_url(creds)
    headers = _headers(creds)
    service = "turn_on" if on else "turn_off"
    url = f"{base}/api/services/{domain}/{service}"
    await _request("POST", url, headers, {"entity_id": entity_id})


async def get_state(device: dict, creds: dict):
    """Return True (on), False (off), or None if unknown."""
    entity_id = device["entity_id"]
    base = _base_url(creds)
    headers = _headers(creds)
    url = f"{base}/api/states/{entity_id}"
    data = await _request("GET", url, headers)
    raw = ((data or {}).get("state") or "unknown").lower()
    if raw == "on":
        return True
    if raw == "off":
        return False
    return None


async def discover(creds: dict) -> list:
    """GET /api/states, keep switch.* and light.* domains, return device dicts."""
    base = _base_url(creds)
    headers = _headers(creds)
    url = f"{base}/api/states"
    states = await _request("GET", url, headers)
    if not isinstance(states, list):
        return []
    devices = []
    for s in states:
        entity_id = s.get("entity_id")
        if not entity_id:
            continue
        if not (entity_id.startswith("switch.") or entity_id.startswith("light.")):
            continue
        attrs = s.get("attributes") or {}
        friendly_name = attrs.get("friendly_name") or entity_id
        devices.append({
            "label": friendly_name,
            "vendor": "homeassistant",
            "entity_id": entity_id,
        })
    return devices
