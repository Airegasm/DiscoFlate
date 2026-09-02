"""Wyze smart-plug driver for DiscoFlate.

*** BEST-EFFORT RECONSTRUCTION -- READ THIS FIRST ***

PumpDirect controls Wyze plugs by shelling out to the `wyze_sdk` pip package
(see PumpDirect/python/wyze_api.py and services/wyze-service.js). That package
is a large third-party dependency that will not run cleanly under Chaquopy on
Android, so this module instead speaks the raw Wyze cloud protocol directly.

The raw protocol is NOT documented by Wyze and is NOT present in the PumpDirect
repo. Everything here is reconstructed from the well-known, community-reverse-
engineered Wyze HTTP API (as used by wyze_sdk, ha-wyzeapi and wyzeapy). It has
NOT been verified against real hardware. This is by far the highest-risk of the
DiscoFlate vendor drivers: endpoint paths, the magic `sc`/`sv` constants, the
2FA/TOTP handshake and the property ids are all things Wyze can and does change.
Every place where the exact wire format is a guess is marked "INFERRED" below.

Design constraints (so it runs under Chaquopy on Android):
  - aiohttp.ClientSession for all HTTP; stdlib crypto only.
  - md5 via hashlib; TOTP via hmac + base64 + struct + time.
  - NO cryptography / pycryptodome / requests.

Contract (async):
    async def set_state(device: dict, on: bool, creds: dict) -> None
    async def get_state(device: dict, creds: dict)        # True / False / None
    async def discover(creds: dict) -> list[dict]

Field contracts:
    device -> {"mac": <device mac>, "model": <product model>}  (both required
              by Wyze to address a plug for control)
    creds  -> {"email", "password", "keyId", "apiKey", "totpKey"(optional)}
    discover() -> [{"label": <nickname>, "vendor": "wyze",
                    "mac": <mac>, "model": <product.model>}, ...]
"""

import base64
import hashlib
import hmac
import json
import struct
import time
import uuid

import aiohttp

# ---------------------------------------------------------------------------
# Well-known Wyze endpoints / constants.
#
# These are the community-known values baked into wyze_sdk and friends. They
# are effectively magic numbers Wyze's servers expect; none of them are secret
# and all of them are subject to change without notice on Wyze's side.
# ---------------------------------------------------------------------------

# Auth service -- exchanges email/password (+ developer API key) for a token.
_AUTH_BASE = "https://auth-prod.api.wyze.com"
_LOGIN_URL = _AUTH_BASE + "/api/user/login"

# App API -- device list, control and property reads run against this host.
_APP_API_BASE = "https://api.wyzecam.com"
_URL_OBJECT_LIST = "/app/v2/home_page/get_object_list"
_URL_RUN_ACTION = "/app/v2/auto/run_action"
_URL_PROPERTY_LIST = "/app/v2/device/get_property_list"

# Static "app identity" values the app API expects in every signed body. These
# come from the reverse-engineered Wyze mobile app. INFERRED: Wyze rotates the
# app version periodically; if calls start failing with a version/upgrade error
# these are the first thing to bump.
_APP_NAME = "com.hualai.WyzeCam"
_APP_VERSION = "2.19.14"
_APP_VER = "com.hualai.WyzeCam___2.19.14"
_PHONE_SYSTEM_TYPE = "1"  # 1 == Android in the reverse-engineered app.
_SC = "9f275790cab94a72bd206c8876429f3c"  # constant "service context".
_SV = "9d74946e652647e9b6c9d59326aef104"  # constant "service version".

# Static developer x-api-key the auth service expects alongside the caller's own
# Keyid/Apikey. This is the public app-level key shipped in wyze_sdk, NOT the
# user's personal API key.
_WYZE_APP_API_KEY = "WMXHYf79Nr5gIlt3r0r7p9Tcw5bvs6BB4U8O8nGJ"

# INFERRED: any plausible Wyze app user-agent works; the app rejects unknown or
# empty ones on some endpoints.
_USER_AGENT = "wyze_android_2.19.14"

# Wyze plug on/off is P3 in the property list: "1" == on, "0" == off.
_PID_POWER = "P3"

# product_type values we treat as controllable plugs during discovery.
_PLUG_PRODUCT_TYPES = ("Plug", "OutdoorPlug")

# ---------------------------------------------------------------------------
# Module-level caches (per-process). Tokens are cached per email and reused
# until a call comes back unauthorized, at which point we force a re-login.
# ---------------------------------------------------------------------------

# email -> {"access_token", "refresh_token", "user_id", "phone_id"}
_TOKEN_CACHE: dict = {}

# email -> stable phone_id. Wyze appears to bind a token to the phone_id used at
# login, so we generate one per account and reuse it for login *and* every app
# API call. INFERRED: a mismatched phone_id can invalidate the session.
_PHONE_IDS: dict = {}


# ---------------------------------------------------------------------------
# Small stdlib crypto helpers (no third-party crypto libraries).
# ---------------------------------------------------------------------------

def _triple_md5(password: str) -> str:
    """Wyze hashes the password as md5(md5(md5(password))) (hex each round)."""
    h = password
    for _ in range(3):
        h = hashlib.md5(h.encode("utf-8")).hexdigest()
    return h


def _totp(secret: str) -> str:
    """Generate a 6-digit RFC 6238 TOTP (HMAC-SHA1, 30s step) from a base32 key.

    Implemented with stdlib only (hmac/hashlib/base64/struct/time) so it works
    under Chaquopy without pyotp or cryptography.
    """
    # Normalize a user-pasted secret: strip spaces, upper-case, pad to a
    # multiple of 8 chars so base32 decoding succeeds.
    cleaned = (secret or "").strip().replace(" ", "").replace("-", "").upper()
    if not cleaned:
        raise Exception("Wyze TOTP secret is empty")
    padding = "=" * ((8 - len(cleaned) % 8) % 8)
    try:
        key = base64.b32decode(cleaned + padding)
    except Exception as e:  # noqa: BLE001 - surface a clear message
        raise Exception(f"Wyze totpKey is not valid base32: {e}")
    counter = int(time.time()) // 30
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


def _phone_id_for(email: str) -> str:
    """Return a stable per-account phone_id, generating one on first use."""
    pid = _PHONE_IDS.get(email)
    if not pid:
        pid = str(uuid.uuid4())
        _PHONE_IDS[email] = pid
    return pid


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def _require(device: dict, key: str) -> str:
    """Fetch a required device field or raise a clear error."""
    value = (device or {}).get(key)
    if not value:
        raise Exception(f"Wyze device is missing required field '{key}'")
    return value


def _require_creds(creds: dict) -> tuple:
    """Validate creds and return (email, password, keyId, apiKey)."""
    creds = creds or {}
    email = creds.get("email")
    password = creds.get("password")
    key_id = creds.get("keyId")
    api_key = creds.get("apiKey")
    missing = [
        name for name, val in (
            ("email", email), ("password", password),
            ("keyId", key_id), ("apiKey", api_key),
        ) if not val
    ]
    if missing:
        raise Exception(
            "Wyze not configured (missing: " + ", ".join(missing) + "). "
            "keyId/apiKey are the developer API credentials from "
            "https://developer-api-console.wyze.com/."
        )
    return email, password, key_id, api_key


# ---------------------------------------------------------------------------
# Low-level HTTP. Returns (status, parsed_json_or_text) instead of raising on
# non-2xx so callers can inspect a 401 and trigger a re-login.
# ---------------------------------------------------------------------------

async def _http_post(session: aiohttp.ClientSession, url: str,
                     headers: dict, body: dict):
    """POST JSON; return (status, parsed). `parsed` is the decoded JSON dict,
    or the raw text if the body is not JSON."""
    try:
        async with session.post(url, headers=headers, json=body) as res:
            status = res.status
            text = await res.text()
    except aiohttp.ClientError as e:
        raise Exception(f"Wyze request to {url} failed: {e}")
    try:
        return status, json.loads(text) if text else {}
    except (ValueError, TypeError):
        return status, text


def _is_auth_error(status: int, parsed) -> bool:
    """Decide whether a response means "token no longer valid, re-login".

    The task specifies re-login on HTTP 401. INFERRED: the Wyze app API almost
    always returns HTTP 200 and signals problems via a string `code` in the
    body, where "2001" is the classic "access token error". We treat both as an
    auth failure so an expired token still triggers exactly one re-login.
    """
    if status == 401:
        return True
    if isinstance(parsed, dict):
        code = str(parsed.get("code"))
        if code in ("2001", "2002", "2003", "UserIsNotExist"):
            return True
    return False


# ---------------------------------------------------------------------------
# Login / token management.
# ---------------------------------------------------------------------------

async def _login(creds: dict, session: aiohttp.ClientSession) -> dict:
    """Perform a fresh login, handling the TOTP 2FA path, and cache the token.

    Returns the cache entry {"access_token", "refresh_token", "user_id",
    "phone_id"}.
    """
    email, password, key_id, api_key = _require_creds(creds)
    phone_id = _phone_id_for(email)
    hashed = _triple_md5(password)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        "Keyid": key_id,
        "Apikey": api_key,
        # INFERRED: the auth service expects the static app-level developer key
        # in x-api-key in addition to the caller's Keyid/Apikey headers.
        "x-api-key": _WYZE_APP_API_KEY,
        "Phone-Id": phone_id,
        "Phone-Type": _PHONE_SYSTEM_TYPE,
    }
    body = {
        "email": email,
        "password": hashed,
        # INFERRED: nonce is a millisecond timestamp; the server tolerates it.
        "nonce": _now_ms(),
    }

    status, parsed = await _http_post(session, _LOGIN_URL, headers, body)
    if not isinstance(parsed, dict):
        raise Exception(
            f"Wyze login returned unexpected response (HTTP {status}): "
            f"{str(parsed)[:200]}"
        )

    # Happy path: a token came straight back.
    if not parsed.get("access_token"):
        # Otherwise a second factor is required.
        parsed = await _handle_mfa(session, headers, email, hashed, parsed, creds)

    token = parsed.get("access_token")
    if not token:
        # Bubble up whatever the server said (bad password, bad key, etc.).
        raise Exception(
            f"Wyze login failed (HTTP {status}): {json.dumps(parsed)[:300]}"
        )

    entry = {
        "access_token": token,
        "refresh_token": parsed.get("refresh_token"),
        "user_id": parsed.get("user_id"),
        "phone_id": phone_id,
    }
    _TOKEN_CACHE[email] = entry
    return entry


async def _handle_mfa(session: aiohttp.ClientSession, headers: dict,
                     email: str, hashed_password: str,
                     first_response: dict, creds: dict) -> dict:
    """Complete a 2FA login. Only the TOTP factor is supported.

    INFERRED wire format (from wyze_sdk's MFA handling):
      - The first login response carries `mfa_options` (e.g.
        ["TotpVerificationCode"]) and, for TOTP, `mfa_details.totp_apps[i].app_id`
        which becomes the `verification_id`.
      - We re-POST to the SAME /api/user/login endpoint with mfa_type,
        verification_id and a freshly generated verification_code.
    """
    mfa_options = first_response.get("mfa_options") or []
    if not mfa_options:
        # No token and no MFA options -> genuine failure, let caller report it.
        return first_response

    if "TotpVerificationCode" not in mfa_options:
        raise Exception(
            "Wyze 2FA required with an unsupported factor "
            f"{mfa_options}. This driver only supports TOTP (authenticator "
            "app). Switch your Wyze account to a TOTP authenticator and set "
            "creds['totpKey']."
        )

    totp_key = (creds or {}).get("totpKey")
    if not totp_key:
        raise Exception(
            "Wyze 2FA required: your account has TOTP two-factor enabled but "
            "no creds['totpKey'] was provided. Add the base32 TOTP secret "
            "(the key behind the authenticator QR code)."
        )

    details = first_response.get("mfa_details") or {}
    verification_id = None
    totp_apps = details.get("totp_apps") or []
    if totp_apps and isinstance(totp_apps, list):
        verification_id = (totp_apps[0] or {}).get("app_id")
    # INFERRED: older/newer response shapes place the id directly on
    # mfa_details; fall back to that before giving up.
    if not verification_id:
        verification_id = details.get("app_id")
    if not verification_id:
        raise Exception(
            "Wyze 2FA TOTP flow: could not find a verification_id (app_id) in "
            f"the login response: {json.dumps(first_response)[:300]}"
        )

    payload = {
        "email": email,
        "password": hashed_password,
        "mfa_type": "TotpVerificationCode",
        "verification_id": verification_id,
        "verification_code": _totp(totp_key),
        "nonce": _now_ms(),
    }
    status, parsed = await _http_post(session, _LOGIN_URL, headers, payload)
    if not isinstance(parsed, dict):
        raise Exception(
            f"Wyze TOTP verification returned unexpected response "
            f"(HTTP {status}): {str(parsed)[:200]}"
        )
    return parsed


async def _get_token(creds: dict, session: aiohttp.ClientSession,
                    force: bool = False) -> dict:
    """Return a cached token entry, logging in if absent or forced."""
    email = (creds or {}).get("email")
    if not force and email:
        cached = _TOKEN_CACHE.get(email)
        if cached and cached.get("access_token"):
            return cached
    return await _login(creds, session)


# ---------------------------------------------------------------------------
# App API request helper: build the signed body, send, and re-login once on an
# auth error.
# ---------------------------------------------------------------------------

def _app_body(entry: dict, extra: dict) -> dict:
    """Build the standard signed body every app API endpoint expects.

    The exact key set the app sends. NOTE: this is not a cryptographic
    signature -- `sc`/`sv` are fixed constants the server checks. INFERRED that
    phone_system_type is accepted (the app always includes it) even though the
    task's key list did not spell it out.
    """
    body = {
        "access_token": entry["access_token"],
        "app_name": _APP_NAME,
        "app_version": _APP_VERSION,
        "app_ver": _APP_VER,
        "phone_id": entry.get("phone_id"),
        "phone_system_type": _PHONE_SYSTEM_TYPE,
        "sc": _SC,
        "sv": _SV,
        "ts": _now_ms(),
    }
    body.update(extra or {})
    return body


async def _app_request(creds: dict, session: aiohttp.ClientSession,
                      path: str, extra: dict) -> dict:
    """Call an app API endpoint, re-logging in exactly once on an auth error.

    Returns the parsed response dict on Wyze success (code == "1").
    """
    headers = {"Content-Type": "application/json", "User-Agent": _USER_AGENT}
    url = _APP_API_BASE + path

    entry = await _get_token(creds, session)
    status, parsed = await _http_post(session, url, headers,
                                      _app_body(entry, extra))

    # Re-login once on a 401 (or a Wyze token-error code) and retry.
    if _is_auth_error(status, parsed):
        entry = await _get_token(creds, session, force=True)
        status, parsed = await _http_post(session, url, headers,
                                          _app_body(entry, extra))

    if not (200 <= status < 300):
        raise Exception(
            f"Wyze {path} failed: HTTP {status}: {str(parsed)[:300]}"
        )
    if not isinstance(parsed, dict):
        raise Exception(
            f"Wyze {path} returned non-JSON body: {str(parsed)[:200]}"
        )

    # The app API returns HTTP 200 with a string `code`; "1" means success.
    code = str(parsed.get("code"))
    if code != "1":
        msg = parsed.get("msg") or parsed.get("message") or ""
        raise Exception(
            f"Wyze {path} error: code={code} {msg} :: "
            f"{json.dumps(parsed)[:300]}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Public contract.
# ---------------------------------------------------------------------------

async def set_state(device: dict, on: bool, creds: dict) -> None:
    """Turn a plug on (on=True) or off. Raise a clear Exception on failure.

    Control is issued via the `run_action` endpoint with:
      provider_key = plug model, instance_id = plug mac,
      action_key   = "power_on" / "power_off".
    """
    mac = _require(device, "mac")
    model = _require(device, "model")
    action_key = "power_on" if on else "power_off"
    extra = {
        "provider_key": model,
        "instance_id": mac,
        "action_key": action_key,
        # INFERRED: run_action expects these two fields present (empty is fine
        # for a simple power toggle).
        "action_params": {},
        "custom_string": "",
    }
    async with aiohttp.ClientSession() as session:
        await _app_request(creds, session, _URL_RUN_ACTION, extra)


async def get_state(device: dict, creds: dict):
    """Return True (on), False (off), or None if the state is unknown.

    Reads the device property list and inspects P3 ("1" == on, "0" == off).
    """
    mac = _require(device, "mac")
    model = _require(device, "model")
    extra = {"device_mac": mac, "device_model": model}
    async with aiohttp.ClientSession() as session:
        parsed = await _app_request(creds, session, _URL_PROPERTY_LIST, extra)

    props = ((parsed.get("data") or {}).get("property_list")) or []
    for prop in props:
        if str(prop.get("pid")) == _PID_POWER:
            value = str(prop.get("value"))
            if value == "1":
                return True
            if value == "0":
                return False
            return None
    # P3 not reported (device offline, or a model that reports power elsewhere).
    return None


async def discover(creds: dict) -> list:
    """Enumerate the account's plugs and return DiscoFlate device dicts.

    Returns [{"label": <nickname>, "vendor": "wyze", "mac": <mac>,
              "model": <product_model>}, ...].
    """
    async with aiohttp.ClientSession() as session:
        parsed = await _app_request(creds, session, _URL_OBJECT_LIST, {})

    device_list = ((parsed.get("data") or {}).get("device_list")) or []
    devices = []
    for dev in device_list:
        product_type = dev.get("product_type") or ""
        if product_type not in _PLUG_PRODUCT_TYPES:
            continue
        mac = dev.get("mac")
        if not mac:
            continue
        devices.append({
            # nickname is the user-facing name; fall back to the mac.
            "label": dev.get("nickname") or mac,
            "vendor": "wyze",
            "mac": mac,
            # product.model in wyze_sdk maps to product_model here.
            "model": dev.get("product_model") or product_type,
        })
    return devices
