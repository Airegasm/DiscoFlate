"""
config_store.py — persistent DiscoFlate settings (data/config.json).

Holds the Discord token, listener state, the active/registered devices, the
roll settings, and the capacity-range -> dice table. Written atomically with
0600 perms (the file contains a bot token).
"""

from __future__ import annotations

import json
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# DISCOFLATE_DATA_DIR lets tests use a throwaway directory so they can never
# touch the real data/config.json (which holds your saved token).
DATA_DIR = os.environ.get("DISCOFLATE_DATA_DIR") or os.path.join(HERE, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

# Default location of PumpDirect's device registry (device list + calibration).
DEFAULT_PUMPDIRECT_PATH = os.path.normpath(
    os.path.join(HERE, "..", "PumpDirect", "data", "devices.json")
)

DEFAULTS = {
    "discord_token": "",
    "command_prefix": "!",
    "listener_enabled": False,
    # Master toggle for the built-in roll (dice) command. Off = dice disabled.
    "roll_enabled": True,
    # Short anti-spam buffer (seconds) between back-to-back uses of the SAME
    # command, across everyone. Subsequent calls inside the window are ignored
    # quietly (no reply). Separate from cooldowns; prevents channel flooding.
    "system_buffer_seconds": 8,
    # Mock mode: run everything (capacity, timers, messages, logs) but never
    # actually toggle a real device — a safe dry run.
    "mock_mode": False,
    # Calibration (seconds-to-100%) for the virtual Mock pump used in mock mode
    # when no real device is active. Editable in the Devices list.
    "mock_calibration_seconds_to_100": 60,

    # Hide the noisy device on/off (set_state) telemetry from the Activity log
    # (it still prints to the console). Discover/add/get always show.
    "silence_onoff_log": False,

    # Names of the three built-in commands (rename to avoid clashing with other
    # bots — e.g. another dice bot already using !roll).
    "command_names": {"roll": "agroll", "capacity": "capacity",
                      "help": "aghelp", "leaderboard": "leaderboard", "pumptimer": "pumptimer"},
    # The !pumptimer built-in reply (always available). Placeholders: [timer]/[total_secs].
    "pumptimer_message": "⏱️ [timer] seconds left on the pump timer.",

    # The Pump operator-control's channel message. Placeholders: [secs]
    # [secs2capacity] [capacity] [timer] [operator].
    "pump_message": ("**[secs]** seconds have been added to the pump timer, and will "
                     "increase [operator]'s volume by **+[secs2capacity]%**\n"
                     "Current Capacity: **[capacity]%** Remaining Pump Timer: **[timer]**s"),

    # The !capacity reply (fully templated; nothing auto-appended).
    # Placeholders: [capacity] [capacity_bar] [dice] [announce] [timer] …
    "capacity_message": "📊 Capacity **[capacity]%**\n[capacity_bar]\nRolling **[dice]** · [announce]",

    # Posted to the announce channel when the Listener is switched on / off.
    # Blank = say nothing. Supports [capacity] and the other placeholders.
    "listener_message_on": "",
    "listener_message_off": "",

    # The single server + channel the bot listens on (legacy / primary).
    # listen_channel_id "" = any channel in the selected server.
    # listen_guild_id "" = listen nowhere yet (pick one in the UI).
    "listen_guild_id": "",
    "listen_channel_id": "",

    # Multiple channels across servers, all sharing ONE dataset (capacity, pump
    # timer, cooldowns). The bot listens in every one and broadcasts events to
    # all of them. Each: {guild_id, guild_name, channel_id, channel_name}.
    "listen_targets": [],
    # When a trigger from one server is echoed to the others, the actor's name
    # and origin server are hidden behind this label.
    "anon_user_label": "Someone on another server",

    # Also accept commands via DM to the bot (opt-in). Anyone who shares a
    # server with the bot can DM it, so pair this with the user allowlist.
    "allow_dms": False,

    # Remembered channel picks per server: {guild_id: {listen, announce}}.
    # UI convenience so switching servers restores the last selection.
    "server_channels": {},

    # Default cooldown (seconds) — used by the roll command and by custom
    # commands that don't set their own. Cooldowns are per-user AND per-command.
    "cooldown_seconds": 30,
    # User IDs that bypass all cooldowns (e.g. you).
    "cooldown_exempt_user_ids": [],
    # Display names that bypass all cooldowns (case-insensitive) — easier than IDs.
    "cooldown_exempt_names": [],
    # The bot operator's name — exposed as [operator] in any message.
    "operator_name": "",
    # ONE generic cooldown message for all commands. Placeholders:
    # [mention] [user] [cooldown] [cmd] (the command they tried).
    "cooldown_message": "⏳ [mention], [cmd] is on cooldown — [cooldown]s left",
    # ONE generic "cooldown ready again" message (blank = say nothing).
    # Placeholders: [mention] [user] [cmd] (the command that reset).
    "cooldown_reset_message": "",

    # Bot auto-posts every N seconds to announce_channel. `message` is a custom
    # template ([capacity], [commands]); blank uses the built-in capacity+commands text.
    "auto_report": {"enabled": False, "seconds": 300, "message": ""},

    # Channel the bot posts auto-reports and milestone messages into.
    "announce_channel_id": "",

    # Roll total -> on-time (seconds).
    "roll": {
        "mode": "value",        # "value": seconds = roll total
                                # "factor": seconds = roll total * factor
        "factor": 1.0,
        "min_seconds": 1,
        "max_seconds": 20,      # HARD CAP on any single fire
        "disable_at_100": False,
        # Message the roll command posts. Placeholders: [user] [dice] [result]
        # [secs] [sides] [capacity]. Blank falls back to the built-in default.
        "reply": "🎲 **[user]** rolled **[dice]** = **[result]** → **[secs]s** · capacity [capacity]%",
    },

    # Which N-dice-of-S-sides to roll, by current capacity range.
    # Evaluated top-down; first range that contains the capacity wins.
    "capacity_ranges": [
        {"min": 0,   "max": 33,  "dice": 1, "sides": 4},
        {"min": 33,  "max": 66,  "dice": 1, "sides": 6},
        {"min": 66,  "max": 100, "dice": 2, "sides": 6},
    ],

    # User-defined commands, created in the web UI on the fly.
    # Each: {name, type: "fire"|"roll"|"say", seconds, dice, sides, reply, enabled}
    "commands": [],

    # Modes group several commands under one switch. Toggling a mode enables/
    # disables its member commands and optionally posts a message.
    # Each: {name, commands: [names], enabled, message_on, message_off}
    "modes": [],

    # Timed events — like commands, but fired automatically on a timer.
    # Each: {name, enabled, mode ("loop"|"once"), every (secs), action, message,
    #        seconds, device_id, dice, sides, capacity_op ("add"|"set"), capacity_value}
    #   mode "loop"       → fires every `every` seconds, repeatedly
    #   mode "once"       → fires a single time, `every` seconds after arming
    #   action "message"  → post `message` to all listen channels
    #   action "fire"     → fire device for `seconds` (+ optional message)
    #   action "roll"     → roll dice on device, scaled on-time (+ message)
    #   action "capacity" → add/set capacity by capacity_value (+ message)
    "events": [],

    # Max Roll Prize — hitting a "perfect" roll (max possible total) `goal`
    # times unlocks a limited-use bonus command for that person. Progress and
    # per-user unlock/uses are tracked in memory and reset on session reset /
    # app restart. Placeholders: [user] [mention] [count] [goal] [remaining]
    # (progress) and [prize_cmd] [prize_desc] [uses] [uses_left] (unlock/use).
    "max_roll_prize": {
        "enabled": False,
        "goal": 3,                 # perfect rolls needed to unlock
        "command": "special",      # the unlocked command's name
        "description": "a special reward",
        "uses": 1,                 # uses granted on unlock
        "action": "fire",          # fire | roll | say
        "seconds": 10,
        "dice": None, "sides": None,
        "device_id": None,
        "reply": "",               # message when the prize command is used
        "progress_message": "[mention] rolled a perfect score [[count]/[goal]] — [remaining] more will unlock a bonus command!",
        "unlock_message": "[mention] has unlocked: [prize_cmd] — [prize_desc]\\nYou can use this command a total of [uses] times.",
    },
    # Multiple prizes (new). Each has the same shape as max_roll_prize plus a
    # per-prize range_gate. An "all"-gated prize overrides range-gated ones.
    # If this list is empty, the single max_roll_prize above is used.
    "prizes": [],

    # Registered devices (imported from PumpDirect or discovered on the LAN).
    # type: "pump" (drives capacity) or "other" (just fires, no capacity).
    # vendor: "kasa" (default/local) | "tapo" | "tuya" | "govee" | "wyze" |
    #         "homeassistant". Each vendor reads different id fields:
    #   kasa/tapo -> host [+ child_id]; tuya -> device_id; govee -> device_id+sku;
    #   wyze -> mac+model; homeassistant -> entity_id.
    "devices": [],              # [{id,label,vendor,host,child_id,device_id,sku,mac,model,entity_id,calibration_seconds_to_100,source,type}]
    "active_device_id": None,   # the pump that drives capacity + is roll's target

    # Per-vendor cloud/account credentials (Kasa needs none — it's local-only).
    # Only the vendors a user actually owns need filling in.
    "vendors": {
        "tapo": {"email": "", "password": ""},
        "tuya": {"accessId": "", "accessSecret": "", "region": "us"},
        "govee": {"apiKey": ""},
        "wyze": {"email": "", "password": "", "keyId": "", "apiKey": "", "totpKey": ""},
        "homeassistant": {"baseUrl": "", "token": ""},
        "kauf": {"web_username": "", "web_password": ""},
    },

    # Optional scoping so the bot only reacts where you want it to.
    "allow": {"guild_ids": [], "channel_ids": [], "user_ids": []},

    "pumpdirect_path": DEFAULT_PUMPDIRECT_PATH,
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except (FileNotFoundError, ValueError):
        stored = {}
    return _deep_merge(DEFAULTS, stored)


def save(cfg: dict) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        try:
            os.chmod(tmp, 0o600)   # protect the token on desktop; best-effort on
        except OSError:            # Android, where app-private storage is already isolated
            pass
        os.replace(tmp, CONFIG_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return cfg


def update(patch: dict) -> dict:
    """Deep-merge a patch into the stored config and persist it."""
    return save(_deep_merge(load(), patch))
