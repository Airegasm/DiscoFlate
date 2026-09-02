"""Per-vendor smart-outlet drivers for DiscoFlate.

Each module implements the async contract used by device_control:
    async def set_state(device: dict, on: bool, creds: dict) -> None
    async def get_state(device: dict, creds: dict) -> bool | None
    async def discover(creds: dict) -> list[dict]
"""
