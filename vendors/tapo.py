"""TP-Link Tapo smart-plug driver for DiscoFlate (LOCAL KLAP protocol).

Unlike PumpDirect -- which shells out to the Rust ``tapo`` crate and therefore
never had to implement the handshake -- this driver speaks the Tapo **KLAP**
LAN protocol directly, on the device's local IP. No cloud round-trip is made:
the TP-Link cloud e-mail/password are used only to derive the LOCAL auth hash.

This is a FROM-SCRATCH KLAP port, translated by hand from the working Kotlin
recipe in the SwellDreams-mobile app:

  * app/src/main/kotlin/com/swelldreams/device/protocol/crypto/KlapCrypto.kt
  * app/src/main/kotlin/com/swelldreams/device/tapo/TapoSession.kt

The crypto below is a faithful, byte-for-byte translation of those two files.

KLAP recipe (all hashes over raw bytes; ``+`` is concatenation):
  * auth_hash   = SHA256( SHA1(email) + SHA1(password) )
  * handshake1  : POST /app/handshake1 with a random 16-byte local_seed.
                  Response = remote_seed(16) + server_hash(32).
                  Verify server_hash == SHA256(local_seed + remote_seed + auth).
                  Capture the TP_SESSIONID cookie from Set-Cookie.
  * handshake2  : POST /app/handshake2 with SHA256(remote_seed + local_seed +
                  auth), sending the captured cookie.
  * session key = SHA256("lsk" + local_seed + remote_seed + auth)[0:16]
  * iv_full     = SHA256("iv"  + local_seed + remote_seed + auth)   (32 bytes)
                  iv_seed = iv_full[0:12]; the last 4 bytes (iv_full[28:32])
                  are a big-endian SIGNED int -> the starting sequence number.
  * sig         = SHA256("ldk" + local_seed + remote_seed + auth)[0:28]
  * per request : seq += 1; iv = iv_seed + seq(4 BE);
                  ct  = AES-128-CBC(key, iv, PKCS7(plaintext));
                  signature = SHA256(sig + seq(4 BE) + ct);
                  wire body = signature(32) + ct;
                  POST /app/request?seq=<seq> with the cookie.
                  Response is decrypted with the SAME seq (strip 32-byte sig,
                  AES-CBC decrypt the rest, un-pad).

AES is done with the pure-Python ``pyaes`` package (NOT cryptography /
pycryptodome), and PKCS7 padding is implemented here, so the whole thing runs
under Chaquopy on Android. Everything else is stdlib (hashlib, os, struct,
json, time, asyncio) plus aiohttp for transport.

Tapo has no cloud/LAN discovery here (devices are added by manual IP), so
``discover()`` returns ``[]``.

WARNING: UNVERIFIED against real Tapo hardware. This KLAP implementation is a
hand port of the SwellDreams-mobile Kotlin code and has NOT been confirmed
on-device.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
import time

import aiohttp
import pyaes


class TapoError(Exception):
    """Raised on any handshake / verification / HTTP / crypto failure."""


class _SessionExpired(TapoError):
    """Retryable: the device rejected our session (HTTP 403) or the transport
    dropped -- we reset and re-handshake once."""


# ---------------------------------------------------------------------------
# Low-level crypto helpers (direct translation of KlapCrypto.kt)
# ---------------------------------------------------------------------------

def _sha1(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _compute_auth_hash(email: str, password: str) -> bytes:
    """auth_hash = SHA256( SHA1(email) + SHA1(password) )."""
    return _sha256(_sha1(email.encode("utf-8")) + _sha1(password.encode("utf-8")))


def _to_signed32(value: int) -> int:
    """Wrap ``value`` into a signed 32-bit int (mimics Kotlin ``Int`` overflow
    and matches python-kasa). The Kotlin source reads the initial sequence via
    ``ByteBuffer.int`` (signed) and writes it via ``putInt`` (two's complement),
    so the on-wire seq bytes are the two's-complement encoding of this value and
    the ``?seq=`` query value is its (possibly negative) signed decimal."""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _seq_bytes(seq: int) -> bytes:
    """Big-endian two's-complement 4-byte encoding of a signed seq (Kotlin
    ``ByteBuffer.putInt``)."""
    return struct.pack(">i", seq)


def _pkcs7_pad(data: bytes) -> bytes:
    """PKCS7 pad to the 16-byte AES block size. Like Java's PKCS5Padding, a full
    block (0x10 * 16) is appended when ``len(data)`` is already a multiple of
    16."""
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    """Strip and validate PKCS7 padding."""
    if not data or len(data) % 16 != 0:
        raise TapoError("decrypted payload is not a multiple of the AES block size")
    pad = data[-1]
    if pad < 1 or pad > 16 or data[-pad:] != bytes([pad]) * pad:
        raise TapoError("invalid PKCS7 padding on decrypted payload")
    return data[:-pad]


def _aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """AES-128-CBC encrypt with PKCS7 padding, one 16-byte block at a time
    (pyaes' CBC mode has no built-in padding and takes a block per call)."""
    aes = pyaes.AESModeOfOperationCBC(key, iv=iv)
    padded = _pkcs7_pad(plaintext)
    out = bytearray()
    for i in range(0, len(padded), 16):
        out += aes.encrypt(padded[i:i + 16])
    return bytes(out)


def _aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-128-CBC decrypt (block at a time) then strip PKCS7 padding."""
    if len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        raise TapoError("ciphertext is not a multiple of the AES block size")
    aes = pyaes.AESModeOfOperationCBC(key, iv=iv)
    out = bytearray()
    for i in range(0, len(ciphertext), 16):
        out += aes.decrypt(ciphertext[i:i + 16])
    return _pkcs7_unpad(bytes(out))


# ---------------------------------------------------------------------------
# Per-host KLAP session (translation of TapoSession.kt)
# ---------------------------------------------------------------------------

# Module-level cache of live sessions, keyed by host (LAN IP). Mirrors the
# single long-lived TapoSession the Kotlin app keeps per device.
_SESSIONS: dict[str, "_KlapSession"] = {}

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)
_OCTET_STREAM = {"Content-Type": "application/octet-stream"}


class _KlapSession:
    """Holds the derived KLAP key material, the TP_SESSIONID cookie and the
    running sequence number for one device, plus its aiohttp transport."""

    def __init__(self, host: str, auth_hash: bytes) -> None:
        self.host = host
        self.auth_hash = auth_hash

        self.cookie: str | None = None
        self.key: bytes | None = None
        self.iv_seed: bytes | None = None
        self.sig: bytes | None = None
        self.seq: int = 0
        self.authenticated: bool = False

        self._client: aiohttp.ClientSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    # -- transport ----------------------------------------------------------

    async def _client_session(self) -> aiohttp.ClientSession:
        """Return an aiohttp session bound to the current running loop.

        Recreated (and the KLAP session invalidated) if the previous client was
        closed or belongs to a different event loop -- so a cached
        ``_KlapSession`` survives being reused across separate ``asyncio.run``
        calls. A DummyCookieJar is used because the device's cookie is not
        RFC-compliant and we manage the Cookie header by hand.
        """
        loop = asyncio.get_running_loop()
        client = self._client
        if client is None or client.closed or self._loop is not loop:
            if client is not None and self._loop is loop and not client.closed:
                try:
                    await client.close()
                except Exception:
                    pass
            self._client = aiohttp.ClientSession(
                timeout=_HTTP_TIMEOUT,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
            self._loop = loop
            # A brand-new transport means no server-side session either.
            self.authenticated = False
        return self._client

    # -- state --------------------------------------------------------------

    def _reset(self) -> None:
        """Drop all KLAP session state (forces a fresh handshake). Matches
        TapoSession.reset()."""
        self.cookie = None
        self.key = None
        self.iv_seed = None
        self.sig = None
        self.seq = 0
        self.authenticated = False

    # -- handshake ----------------------------------------------------------

    async def _authenticate_locked(self) -> None:
        """Perform handshake1 + handshake2 and derive the session keys.

        Assumes ``self._lock`` is already held. Raises TapoError on any failure.
        """
        client = await self._client_session()
        base = f"http://{self.host}"

        # Step 1: fresh 16-byte local seed.
        local_seed = os.urandom(16)

        # Step 2: handshake1 -- send local_seed, receive remote_seed + hash.
        try:
            async with client.post(
                f"{base}/app/handshake1", data=local_seed, headers=_OCTET_STREAM
            ) as resp:
                if resp.status != 200:
                    raise TapoError(f"handshake1 failed with HTTP {resp.status}")
                body = await resp.read()
                set_cookie = resp.headers.get("Set-Cookie")
        except aiohttp.ClientError as exc:
            raise TapoError(f"handshake1 transport error: {exc}") from exc

        if len(body) != 48:
            raise TapoError(
                f"invalid handshake1 response: expected 48 bytes, got {len(body)}"
            )

        remote_seed = body[0:16]
        server_hash = body[16:48]

        # Capture the session cookie (e.g. "TP_SESSIONID=ABC123"), first field.
        if not set_cookie:
            raise TapoError("no session cookie in handshake1 response")
        self.cookie = set_cookie.split(";")[0].strip()

        # Step 3: verify the server proved knowledge of the credentials.
        expected = _sha256(local_seed + remote_seed + self.auth_hash)
        if server_hash != expected:
            raise TapoError(
                "server verification failed -- invalid credentials for this device"
            )

        # Step 4: handshake2 -- prove OUR knowledge of the credentials.
        handshake2_payload = _sha256(remote_seed + local_seed + self.auth_hash)
        try:
            async with client.post(
                f"{base}/app/handshake2",
                data=handshake2_payload,
                headers={**_OCTET_STREAM, "Cookie": self.cookie},
            ) as resp:
                if resp.status != 200:
                    raise TapoError(f"handshake2 failed with HTTP {resp.status}")
                await resp.read()
        except aiohttp.ClientError as exc:
            raise TapoError(f"handshake2 transport error: {exc}") from exc

        # Step 5: derive session material.
        self.key = _sha256(b"lsk" + local_seed + remote_seed + self.auth_hash)[0:16]
        iv_full = _sha256(b"iv" + local_seed + remote_seed + self.auth_hash)
        self.iv_seed = iv_full[0:12]
        self.sig = _sha256(b"ldk" + local_seed + remote_seed + self.auth_hash)[0:28]
        # Initial seq = last 4 bytes of iv_full read as a signed big-endian int.
        self.seq = _to_signed32(struct.unpack(">i", iv_full[28:32])[0])

        self.authenticated = True

    # -- encrypted request --------------------------------------------------

    async def _send_locked(self, payload: dict) -> dict:
        """Encrypt+send one JSON command, decrypt the reply. Lock held.

        Raises _SessionExpired (retryable) on HTTP 403 / transport drop, and
        TapoError on any other failure.
        """
        if not (self.authenticated and self.key and self.iv_seed and self.sig):
            raise TapoError("not authenticated")

        client = await self._client_session()

        # Increment BEFORE using -- the first request uses initial_seq + 1
        # (matches TapoSession.sendCommand).
        self.seq = _to_signed32(self.seq + 1)
        seq = self.seq
        seq_be = _seq_bytes(seq)

        plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        iv = self.iv_seed + seq_be
        ciphertext = _aes_cbc_encrypt(self.key, iv, plaintext)
        signature = _sha256(self.sig + seq_be + ciphertext)
        wire = signature + ciphertext

        url = f"http://{self.host}/app/request?seq={seq}"
        try:
            async with client.post(
                url, data=wire, headers={**_OCTET_STREAM, "Cookie": self.cookie}
            ) as resp:
                if resp.status == 403:
                    raise _SessionExpired("device rejected session (HTTP 403)")
                if resp.status != 200:
                    raise TapoError(f"request failed with HTTP {resp.status}")
                resp_body = await resp.read()
        except aiohttp.ClientError as exc:
            raise _SessionExpired(f"request transport error: {exc}") from exc

        if len(resp_body) < 32:
            raise TapoError(
                f"response too short: {len(resp_body)} bytes (need >= 32)"
            )

        # Response is signed+encrypted under the SAME sequence number.
        resp_ciphertext = resp_body[32:]
        decrypted = _aes_cbc_decrypt(self.key, iv, resp_ciphertext)
        try:
            decoded = json.loads(decrypted.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TapoError(f"could not decode decrypted response JSON: {exc}") from exc
        # KLAP delivers device-level failures as error_code over HTTP 200 —
        # treat them as failures or a rejected command counts as a success.
        code = decoded.get("error_code")
        if code not in (None, 0):
            raise TapoError(f"device returned error_code {code}")
        return decoded

    # -- public entry point -------------------------------------------------

    async def request(self, payload: dict) -> dict:
        """Send one command, (re)handshaking as needed. Retries once if the
        session has expired or the transport dropped."""
        async with self._lock:
            last_exc: TapoError | None = None
            for attempt in range(2):
                try:
                    if not self.authenticated:
                        await self._authenticate_locked()
                    return await self._send_locked(payload)
                except _SessionExpired as exc:
                    last_exc = exc
                    self._reset()  # force a fresh handshake on the retry
                    continue
            assert last_exc is not None
            raise last_exc


async def _get_session(host: str, creds: dict) -> _KlapSession:
    """Return the cached session for ``host``, creating one (or replacing it if
    the credentials changed) as needed."""
    if not host:
        raise TapoError("device is missing 'host' (LAN IP)")
    email = (creds or {}).get("email")
    password = (creds or {}).get("password")
    if not email or not password:
        raise TapoError("Tapo creds must include 'email' and 'password'")

    auth_hash = _compute_auth_hash(email, password)
    session = _SESSIONS.get(host)
    if session is None or session.auth_hash != auth_hash:
        session = _KlapSession(host, auth_hash)
        _SESSIONS[host] = session
    return session


def _now_ms() -> int:
    """Milliseconds since epoch (Kotlin uses requestTimeMils)."""
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Public driver contract
# ---------------------------------------------------------------------------

async def set_state(device: dict, on: bool, creds: dict) -> None:
    """Turn ``device`` ON (on=True) or OFF (on=False) over local KLAP.

    device uses ``device["host"]`` (the LAN IP); creds are the TP-Link cloud
    ``{"email", "password"}`` used for LOCAL authentication.
    """
    session = await _get_session(device["host"], creds)
    payload = {
        "method": "set_device_info",
        "params": {"device_on": bool(on)},
        "requestTimeMils": _now_ms(),
    }
    await session.request(payload)


async def get_state(device: dict, creds: dict):
    """Return the power state of ``device`` over local KLAP.

    True == on, False == off, None == unknown (device_on missing/not a bool).
    Handshake / HTTP / crypto failures raise TapoError.
    """
    session = await _get_session(device["host"], creds)
    payload = {
        "method": "get_device_info",
        "params": None,
        "requestTimeMils": _now_ms(),
    }
    response = await session.request(payload)

    result = response.get("result") or {}
    value = result.get("device_on")
    return value if isinstance(value, bool) else None


async def discover(creds: dict) -> list[dict]:
    """Tapo devices are added by manual IP here, so there is no discovery.

    Returns ``[]`` (Tapo = manual IP).
    """
    return []
