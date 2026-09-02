"""
pumpdirect_import.py — read PumpDirect's device registry so DiscoFlate can
reuse the devices you already registered and calibrated over there.

PumpDirect stores devices at data/devices.json as {"devices": [ ... ]}, each:
  {id, label, vendor, ip, childId, isPrimary,
   calibration: {secondsTo100, calibrationTime, calibratedAt}, ...}

We only surface locally-controllable Kasa devices (that's what DiscoFlate's
driver speaks). Everything else is ignored with a note.
"""

from __future__ import annotations

import json


def load_kasa_devices(path: str) -> list[dict]:
    """
    Return DiscoFlate-shaped device dicts for every Kasa device in PumpDirect's
    registry:  {id, label, host, child_id, calibration_seconds_to_100, source}
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, ValueError):
        return []

    out: list[dict] = []
    for d in raw.get("devices", []):
        if (d.get("vendor") or "").lower() != "kasa":
            continue
        if not d.get("ip"):
            continue
        cal = (d.get("calibration") or {}).get("secondsTo100")
        out.append({
            "id": f"pd:{d.get('id')}",
            "label": d.get("label") or d.get("ip"),
            "host": d["ip"],
            "child_id": d.get("childId") or None,
            "calibration_seconds_to_100": cal,
            "source": "pumpdirect",
            "is_primary": bool(d.get("isPrimary")),
        })
    return out
