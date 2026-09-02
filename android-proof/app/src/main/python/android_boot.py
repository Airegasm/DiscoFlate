"""Android entry point.

Boots DiscoFlate's app.py (aiohttp server + engine + Discord bot) on a daemon
thread, pointed at the app's private writable storage, so the Kotlin/Java host
can load the existing web UI in a WebView at http://127.0.0.1:8765.
"""
import os
import socket
import threading
import time

_thread = None


def start(files_dir):
    """Called from Java with the app's filesDir path. Sets writable storage +
    web-dir env vars, then launches app.main() in a daemon thread. Returns at once.

    IMPORTANT: the env vars are set BEFORE `import app` (which imports
    config_store), because those modules compute their paths at import time.
    """
    global _thread
    if _thread is not None and _thread.is_alive():
        return "already-running"

    data_dir = os.path.join(files_dir, "data")
    web_dir = os.path.join(files_dir, "web")   # Java copies assets/web/* here
    os.makedirs(data_dir, exist_ok=True)
    os.environ["DISCOFLATE_DATA_DIR"] = data_dir
    os.environ["DISCOFLATE_WEB_DIR"] = web_dir

    def _run():
        import asyncio
        import app  # first import happens here, with the env vars above in place
        try:
            asyncio.run(app.main())
        except Exception as e:  # noqa: BLE001
            import traceback
            print("DiscoFlate server thread crashed:", repr(e))
            traceback.print_exc()

    _thread = threading.Thread(target=_run, name="discoflate-server", daemon=True)
    _thread.start()
    return "started"


def force_off():
    """Best-effort safety shutoff: hit the local /api/abort so the engine turns
    every device OFF. Called by the foreground service when the app is stopped or
    swiped away, so a pump can't get stuck on."""
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:8765/api/abort",
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
        return "ok"
    except Exception as e:  # noqa: BLE001
        return "err:" + repr(e)


def wait_until_up(host="127.0.0.1", port=8765, timeout=25.0):
    """Block until the local server accepts a TCP connection, or timeout.
    Called from a Java background thread so the WebView loads only when ready."""
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        try:
            with socket.create_connection((host, int(port)), 1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False
