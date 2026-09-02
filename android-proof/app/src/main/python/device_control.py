"""Vendor-agnostic device control.

Routes on/off/status/discovery by a device's ``vendor`` field. Kasa is the
built-in local driver (kasa_legacy); every other brand lives in ``vendors/<name>.py``
and is imported lazily, so a missing or broken vendor module never affects Kasa.

Device dicts carry a ``vendor`` (default "kasa") plus the id fields that vendor
needs (kasa: host[/child_id]; tapo: host; tuya: device_id; govee: device_id+sku;
wyze: mac+model; homeassistant: entity_id). ``creds`` is that vendor's saved
credentials (config["vendors"][vendor]).
"""

from __future__ import annotations

import importlib

import kasa_legacy as kasa

_ALIASES = {"ha": "homeassistant", "home_assistant": "homeassistant", "tplink": "kasa"}


_log_sink = None


def set_log_sink(fn) -> None:
    """Also route device debug into the app's Activity log (fn(msg))."""
    global _log_sink
    _log_sink = fn


def _dbg(msg: str) -> None:
    """Device debug line → console (stdout/logcat) AND the in-app Activity log."""
    print(f"[device] {msg}", flush=True)
    if _log_sink is not None:
        try:
            _log_sink(msg)
        except Exception:  # noqa: BLE001
            pass


def _ident(device: dict) -> str:
    for k in ("host", "device_id", "mac", "entity_id", "label", "id"):
        if device.get(k):
            return str(device[k])
    return "?"


def normalize_vendor(device: dict) -> str:
    v = (device.get("vendor") or "kasa").strip().lower()
    return _ALIASES.get(v, v)


def _driver(vendor: str):
    """Import vendors/<vendor>.py on demand."""
    return importlib.import_module(f"vendors.{vendor}")


def _kasa_outlet(device: dict) -> "kasa.Outlet":
    return kasa.Outlet(
        host=device["host"],
        alias=device.get("label") or device.get("host") or "",
        model=device.get("model") or "",
        child_id=device.get("child_id") or None,
    )


async def set_state(device: dict, on: bool, creds: dict) -> None:
    """Turn a device on/off, routed by vendor. Raises on failure."""
    vendor = normalize_vendor(device)
    _dbg(f"USE  set_state vendor={vendor} target={_ident(device)} on={on}")
    try:
        if vendor == "kasa":
            await kasa.set_state(_kasa_outlet(device), on)
        else:
            await _driver(vendor).set_state(device, on, creds or {})
        _dbg(f"USE  set_state OK  vendor={vendor} target={_ident(device)} on={on}")
    except Exception as e:  # noqa: BLE001
        _dbg(f"USE  set_state ERR vendor={vendor} target={_ident(device)}: {e!r}")
        raise


async def get_state(device: dict, creds: dict):
    """Return True (on) / False (off) / None (unknown), routed by vendor."""
    vendor = normalize_vendor(device)
    if vendor == "kasa":
        try:
            st = await kasa.get_status(_kasa_outlet(device))
        except Exception as e:  # noqa: BLE001
            _dbg(f"USE  get_state ERR vendor=kasa target={_ident(device)}: {e!r}")
            return None
        rs = st.get("relay_state") if isinstance(st, dict) else None
        result = None if rs is None else bool(rs)
    else:
        try:
            result = await _driver(vendor).get_state(device, creds or {})
        except Exception as e:  # noqa: BLE001
            _dbg(f"USE  get_state ERR vendor={vendor} target={_ident(device)}: {e!r}")
            return None
    _dbg(f"USE  get_state vendor={vendor} target={_ident(device)} -> {result}")
    return result


async def discover(vendor: str, creds: dict) -> list[dict]:
    """Enumerate devices for a vendor. Returns DiscoFlate device dicts."""
    v = _ALIASES.get((vendor or "kasa").strip().lower(), (vendor or "kasa").strip().lower())
    _dbg(f"SEARCH discover vendor={v} …")
    try:
        if v == "kasa":
            outlets = await kasa.discover()
            found = [{"label": o.alias or o.host, "vendor": "kasa", "host": o.host,
                      "child_id": o.child_id, "model": o.model} for o in outlets]
        else:
            found = await _driver(v).discover(creds or {})
    except Exception as e:  # noqa: BLE001
        _dbg(f"SEARCH discover ERR vendor={v}: {e!r}")
        raise
    _dbg(f"SEARCH discover vendor={v} found={len(found)}: "
         + ", ".join(_ident(d) for d in found[:20]))
    return found
