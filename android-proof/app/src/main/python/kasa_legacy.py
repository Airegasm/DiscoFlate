"""
kasa_legacy.py — TP-Link Kasa "legacy" local protocol (the UDP/TCP 9999 way).

No cloud, no credentials, no external Kasa library. This talks to Kasa devices
directly on your LAN using the original TP-Link smarthome protocol:

  * Discovery  -> UDP broadcast to 255.255.255.255:9999 (no length header)
  * Control    -> TCP connect to <device>:9999 (4-byte big-endian length header)
  * Encryption -> the "autokey" XOR stream cipher, seeded at 0xAB (171)

Works with legacy-firmware HS/KP/EP plugs & switches, HS300/KP303 power strips,
HS220 dimmers, and KL/LB bulbs. Devices whose firmware only speaks the newer
KLAP/AES transport (cloud-locked) will NOT answer on 9999 — that's expected.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from dataclasses import dataclass, field

DEFAULT_PORT = 9999
BROADCAST_ADDR = "255.255.255.255"
INITIAL_KEY = 0xAB  # 171 — the TP-Link autokey seed


# --------------------------------------------------------------------------- #
# Autokey XOR cipher
# --------------------------------------------------------------------------- #
def encrypt(plaintext: str) -> bytes:
    """XOR autokey encrypt. Each cipher byte becomes the key for the next."""
    key = INITIAL_KEY
    out = bytearray()
    for b in plaintext.encode("utf-8"):
        key = key ^ b
        out.append(key)
    return bytes(out)


def decrypt(ciphertext: bytes) -> str:
    """Inverse of encrypt()."""
    key = INITIAL_KEY
    out = bytearray()
    for b in ciphertext:
        out.append(key ^ b)
        key = b
    return out.decode("utf-8", errors="replace")


def _framed(plaintext: str) -> bytes:
    """TCP payload: 4-byte big-endian length header + encrypted body."""
    body = encrypt(plaintext)
    return struct.pack(">I", len(body)) + body


# --------------------------------------------------------------------------- #
# Low-level transport (blocking; call via asyncio.to_thread)
# --------------------------------------------------------------------------- #
def _tcp_query(host: str, payload: dict, port: int = DEFAULT_PORT, timeout: float = 5.0) -> dict:
    """Send one command over TCP 9999 and return the decoded JSON reply."""
    raw = json.dumps(payload, separators=(",", ":"))
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(_framed(raw))

        # Reply is also length-prefixed. Cap it: a real sysinfo is a few KB, so
        # a spoofed device declaring a huge length can't balloon our memory.
        header = _recv_exact(sock, 4)
        (length,) = struct.unpack(">I", header)
        if length > 1024 * 1024:
            raise ConnectionError(f"kasa reply claims {length} bytes — refusing")
        body = _recv_exact(sock, length)

    return json.loads(decrypt(body))


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed mid-message")
        buf.extend(chunk)
    return bytes(buf)


def _discover_raw(timeout: float = 3.0, broadcast: str = BROADCAST_ADDR) -> dict[str, dict]:
    """
    UDP-broadcast get_sysinfo on port 9999 and collect every reply.

    Returns {ip: sysinfo_dict}. Runs for the full `timeout` window so slow /
    distant devices still get counted.
    """
    query = encrypt(json.dumps({"system": {"get_sysinfo": {}}}, separators=(",", ":")))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 0))
    sock.settimeout(0.5)

    found: dict[str, dict] = {}
    try:
        sock.sendto(query, (broadcast, DEFAULT_PORT))

        loop = _monotonic()
        deadline = loop + timeout
        while _monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            ip = addr[0]
            if ip in found:
                continue
            try:
                reply = json.loads(decrypt(data))
                sysinfo = reply["system"]["get_sysinfo"]
            except (ValueError, KeyError):
                continue  # not a legacy-9999 device, ignore
            found[ip] = sysinfo
    finally:
        sock.close()
    return found


def _monotonic() -> float:
    # Wrapped so the module stays import-safe in odd sandboxes.
    import time
    return time.monotonic()


# --------------------------------------------------------------------------- #
# Device model / capability detection
# --------------------------------------------------------------------------- #
@dataclass
class Outlet:
    """A controllable target: a whole device, or one child of a power strip."""
    host: str
    alias: str
    model: str
    child_id: str | None = None          # None = the device itself
    is_bulb: bool = False
    is_dimmer: bool = False
    has_emeter: bool = False
    device_id: str = ""

    @property
    def key(self) -> str:
        return self.alias

    def _context(self, payload: dict) -> dict:
        """Wrap a command so it targets a specific strip child, if any."""
        if self.child_id:
            return {"context": {"child_ids": [self.child_id]}, **payload}
        return payload


def _is_bulb(sysinfo: dict) -> bool:
    if "light_state" in sysinfo:
        return True
    mic = (sysinfo.get("mic_type") or sysinfo.get("type") or "").upper()
    return "SMARTBULB" in mic or "BULB" in mic


def _is_dimmer(sysinfo: dict) -> bool:
    name = (sysinfo.get("dev_name") or sysinfo.get("model") or "").lower()
    if "dimmer" in name:
        return True
    # HS220-style dimmers expose a top-level brightness alongside a relay.
    return "brightness" in sysinfo and "relay_state" in sysinfo


def _has_emeter(sysinfo: dict) -> bool:
    return "ENE" in (sysinfo.get("feature") or "")


def outlets_from_sysinfo(host: str, sysinfo: dict) -> list[Outlet]:
    """Expand a device's sysinfo into one or more controllable Outlets."""
    model = sysinfo.get("model", "?")
    device_id = sysinfo.get("deviceId", "")
    children = sysinfo.get("children")

    if children:  # power strip: one Outlet per child
        parent_alias = sysinfo.get("alias", model)
        results: list[Outlet] = []
        for child in children:
            cid = child.get("id", "")
            # Legacy strips give a 2-char child id that must be prefixed
            # with the parent deviceId to form the full child_id.
            full = device_id + cid if len(cid) <= 2 else cid
            child_alias = child.get("alias") or f"{parent_alias} {cid}"
            results.append(
                Outlet(
                    host=host,
                    alias=child_alias,
                    model=model,
                    child_id=full,
                    has_emeter=_has_emeter(sysinfo),
                    device_id=device_id,
                )
            )
        return results

    return [
        Outlet(
            host=host,
            alias=sysinfo.get("alias", model),
            model=model,
            is_bulb=_is_bulb(sysinfo),
            is_dimmer=_is_dimmer(sysinfo),
            has_emeter=_has_emeter(sysinfo),
            device_id=device_id,
        )
    ]


# --------------------------------------------------------------------------- #
# Async-friendly public API
# --------------------------------------------------------------------------- #
async def discover(timeout: float = 3.0, broadcast: str = BROADCAST_ADDR) -> list[Outlet]:
    """Broadcast-discover the LAN and return a flat list of controllable Outlets."""
    raw = await asyncio.to_thread(_discover_raw, timeout, broadcast)
    outlets: list[Outlet] = []
    for host, sysinfo in raw.items():
        outlets.extend(outlets_from_sysinfo(host, sysinfo))
    outlets.sort(key=lambda o: o.alias.lower())
    return outlets


async def _query(outlet: Outlet, payload: dict) -> dict:
    return await asyncio.to_thread(_tcp_query, outlet.host, outlet._context(payload))


async def set_state(outlet: Outlet, on: bool) -> None:
    if outlet.is_bulb:
        await _query(outlet, {
            "smartlife.iot.smartbulb.lightingservice": {
                "transition_light_state": {"on_off": 1 if on else 0}
            }
        })
    else:
        await _query(outlet, {"system": {"set_relay_state": {"state": 1 if on else 0}}})


async def set_brightness(outlet: Outlet, level: int) -> None:
    level = max(0, min(100, int(level)))
    if outlet.is_bulb:
        await _query(outlet, {
            "smartlife.iot.smartbulb.lightingservice": {
                "transition_light_state": {"on_off": 1, "brightness": level}
            }
        })
    elif outlet.is_dimmer:
        # A dimmer must be relay-on for brightness to be visible.
        await set_state(outlet, True)
        await _query(outlet, {"smartlife.iot.dimmer": {"set_brightness": {"brightness": level}}})
    else:
        raise ValueError(f"{outlet.alias} does not support brightness")


async def get_status(outlet: Outlet) -> dict:
    """Return a normalized status dict: on, brightness, power_w (if metered)."""
    reply = await _query(outlet, {"system": {"get_sysinfo": {}}})
    sysinfo = reply["system"]["get_sysinfo"]

    # Resolve the specific child's state if this Outlet is a strip child.
    on = None
    if outlet.child_id and sysinfo.get("children"):
        suffix = outlet.child_id[-2:]
        for child in sysinfo["children"]:
            if child.get("id", "").endswith(suffix):
                on = bool(child.get("state"))
                break
    elif outlet.is_bulb:
        on = bool(sysinfo.get("light_state", {}).get("on_off"))
    else:
        on = bool(sysinfo.get("relay_state"))

    status = {"on": on, "alias": outlet.alias, "model": outlet.model}

    if outlet.is_bulb:
        ls = sysinfo.get("light_state", {})
        status["brightness"] = (ls.get("dft_on_state") or ls).get("brightness")
    elif outlet.is_dimmer:
        status["brightness"] = sysinfo.get("brightness")

    if outlet.has_emeter:
        try:
            em = await _query(outlet, {"emeter": {"get_realtime": {}}})
            rt = em["emeter"]["get_realtime"]
            # Older hw reports milli-units; newer reports plain units.
            status["power_w"] = rt.get("power", (rt.get("power_mw", 0) / 1000.0))
        except Exception:
            pass

    return status


# --------------------------------------------------------------------------- #
# Standalone self-test:  python3 kasa_legacy.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # 1) prove the cipher round-trips
    sample = json.dumps({"system": {"get_sysinfo": {}}})
    assert decrypt(encrypt(sample)) == sample, "cipher round-trip failed"
    print("cipher round-trip OK")

    # 2) live discovery
    async def _main():
        print(f"broadcasting get_sysinfo -> {BROADCAST_ADDR}:{DEFAULT_PORT} ...")
        outlets = await discover(timeout=3.0)
        if not outlets:
            print("no legacy devices answered on UDP 9999.")
            return
        for o in outlets:
            tag = "bulb" if o.is_bulb else "dimmer" if o.is_dimmer else "plug/switch"
            meter = " +emeter" if o.has_emeter else ""
            print(f"  {o.host:<15} {o.alias:<24} [{o.model} {tag}{meter}]")

    asyncio.run(_main())
