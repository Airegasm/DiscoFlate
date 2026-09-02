"""Day-one Chaquopy proof: confirm DiscoFlate's Python stack imports on Android.

Called from MainActivity. Returns a JSON report of every dependency + DiscoFlate
core module, with the version (or the exact failure) for each — so a green screen
on-device means the real backend loads under Chaquopy.
"""
import json
import sys


def report():
    out = {"python": sys.version.split()[0], "platform": sys.platform}

    # Third-party deps (the four C-extension ones are the make-or-break).
    for mod in ("aiohttp", "discord", "yarl", "multidict", "frozenlist",
                "aiosignal", "attrs", "idna"):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, "__version__", "imported")
        except Exception as e:  # noqa: BLE001
            out[mod] = "FAILED: " + repr(e)

    # DiscoFlate's own modules (engine pulls in kasa_legacy; app pulls everything).
    for mod in ("kasa_legacy", "config_store", "engine", "discord_bot", "app"):
        try:
            __import__(mod)
            out["discoflate." + mod] = "imported"
        except Exception as e:  # noqa: BLE001
            out["discoflate." + mod] = "FAILED: " + repr(e)

    ok = all(not str(v).startswith("FAILED") for k, v in out.items()
             if k not in ("python", "platform"))
    out["RESULT"] = "ALL GREEN ✅" if ok else "SOME IMPORTS FAILED ❌"
    return json.dumps(out, indent=2)
