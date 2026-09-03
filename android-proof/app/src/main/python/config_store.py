"""
config_store.py — persistent DiscoFlate settings (data/config.json).

Holds the Discord token, listener state, the active/registered devices, the
roll settings, and the capacity-range -> dice table. Written atomically with
0600 perms (the file contains a bot token).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# DISCOFLATE_DATA_DIR lets tests use a throwaway directory so they can never
# touch the real data/config.json (which holds your saved token).
DATA_DIR = os.environ.get("DISCOFLATE_DATA_DIR") or os.path.join(HERE, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
KEEP_BACKUPS = 10   # rolling ring of pre-save snapshots (max one per minute)
KEEP_DAILY = 7      # plus one snapshot per day

# Set when load() found a corrupt config and moved it aside — the UI surfaces
# this so a boot-into-defaults never masquerades as a factory reset.
RECOVERED_FROM: str | None = None

# Default location of PumpDirect's device registry (device list + calibration).
DEFAULT_PUMPDIRECT_PATH = os.path.normpath(
    os.path.join(HERE, "..", "PumpDirect", "data", "devices.json")
)

# Schema version of the stored config. Bump it + add a _migrate step whenever a
# key is renamed/moved, so old configs upgrade instead of silently stranding data.
CONFIG_VERSION = 2

DEFAULTS = {
    "discord_token": "",
    "config_version": CONFIG_VERSION,
    # Bumped on every save; the UI sends the rev it last saw with each config
    # write so a stale tab's snapshot is rejected instead of clobbering.
    "config_rev": 0,
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
                      "help": "aghelp", "leaderboard": "toppumpers",
                      "leaderboard_life": "toppumpers-life", "pumptimer": "pumptimer",
                      "vote": "agvote"},
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

    # Session pause (the dashboard STOP/RESUME button). While paused every
    # device-on path is latched off, running fires/events/minigames are
    # cancelled, and commands get the paused notice. The latch persists so a
    # crash or restart comes back paused. Placeholders: [user] (who acted).
    "session_paused": False,
    "session_paused_by": "",
    "pause_message": ("⏸️ **Session paused** by [user] — pumps are off and "
                      "commands are disabled until the operator resumes."),
    "resume_message": "▶️ **Session resumed** by [user] — pump away!",
    # Reply to someone who runs a command while paused (per-user, buffered).
    "paused_notice_message": "⏸️ [mention], the session is paused — hang tight until the operator resumes.",

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

    # Prefix every command/minigame/event output with a **[label · user]** tag so
    # that, when several players' commands and multi-stage minigames interleave in
    # chat, each line is traceable to who/what it belongs to. The user portion of
    # the tag respects cross-server anonymity (shows the anon label where the body
    # would). Events tag with just the event name (no user).
    "output_headers": False,

    # Post the status/report outputs (capacity check, the two leaderboards, the
    # auto-report, and custom Broadcasts) as rich embed cards — a bordered, colored
    # container — instead of plain text, so those blocks stand apart from the rapid
    # command chatter. Only affects those status posts; per-command replies stay
    # plain text.
    "rich_output": False,

    # Reusable templates saved from the Commands / Events / Ranges editors, kept per
    # install (and included in config backup/restore). "Add to Config" ports one
    # back into the live config with clash-safe naming. Each list holds whole item
    # objects (a command / event / range).
    "templates": {"commands": [], "events": [], "ranges": []},

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

    # User-defined commands, created in the web UI on the fly. Each:
    #   {name, type, seconds, dice, sides, device_id, reply, enabled, owner_only,
    #    mention, hide_in_list, description, start_events, range_gate,
    #    react_only, react_emoji}
    # react_only → acknowledge the command with a reaction (react_emoji, default 💨)
    #   on the caller's message instead of posting a text reply (cuts chat spam for
    #   rapid-fire commands). The fire/roll/credit still happen; only the reply text
    #   is suppressed. Cross-server echo is skipped (a reaction is local to the
    #   origin channel). Falls back to a normal reply if the emoji can't be added.
    # type "fire"   → fire device_id for `seconds` (+ optional extra `fires` rows)
    # type "roll"   → roll dice/sides (or the range's) and fire for the total
    # type "say"    → text-only reply, no device
    # type "chance" → gamble: roll 1–100 vs `chance`% (± `luck`); win posts
    #                 `success_reply` + fires the `fires` rows, miss posts `failure_reply`
    #   fires: [{device_id, seconds}] — independent, concurrent device fires
    # type "game-*" (pushluck/simon/balloon/rps/slots/blackjack) → button/
    #   ephemeral minigames (minigames.py). Params: pl_* (pushluck), sm_* (simon),
    #   bl_cells/bl_pops/bl_points (balloon), rps_wins (rps), sl_symbols (slots).
    #   game_intro = message with the Play button. game_tiers = [{op, min, message,
    #   fires}] score→outcome; the highest matching value (per `op`: >= > = != < <=)
    #   the final score reaches broadcasts + fires (credited to the player). A luck
    #   modifier (a flat ± added to the final score before tier lookup) may be set
    #   per range (the range's cooldowns[cmd].luck) or on the Always-On entry; a
    #   range the game is a member of takes precedence over Always-On. [score] and
    #   [luck] are available in tier messages. Every result is labelled with the
    #   game and respects cross-server anonymity (real name only where a member).
    "commands": [],

    # Custom commands that work in EVERY capacity range (bypass per-range
    # membership) when always_on_enabled is on. Each entry is {name, cooldown,
    # max_uses, luck} (bare name strings are also accepted); cooldown/max_uses/luck
    # are optional. `luck` applies only to minigames (see game-* above).
    "always_on_enabled": False,
    "always_on_commands": [],

    # Modes group several commands under one switch. Toggling a mode enables/
    # disables its member commands and optionally posts a message.
    # Each: {name, commands: [names], enabled, message_on, message_off}
    "modes": [],

    # Timed events — like commands, but fired automatically on a timer.
    # Each: {name, enabled, mode ("loop"|"once"), every (secs), action, message,
    #        seconds, device_id, dice, sides, capacity_op ("add"|"set"), capacity_value,
    #        max_repeats, cooldown, fire_immediately, clean_previous,
    #        activation_message, end_message,
    #        chance, luck, fires, fail_fires, success_message, failure_message}
    #   mode "loop"       → fires every `every` seconds, repeatedly (up to
    #                       `max_repeats` times if set; 0/blank = unlimited).
    #                       clean_previous → each round deletes the previous round's
    #                       message (anti-spam); the end_message replaces the last.
    #   mode "once"       → fires a single time, `every` seconds after arming
    #   action "message"  → post `message` to all listen channels
    #   action "fire"     → fire device for `seconds` (+ optional message)
    #   action "roll"     → roll dice on device, scaled on-time (+ message)
    #   action "capacity" → add/set capacity by capacity_value (+ message)
    #   action "chance"   → roll vs `chance`% (± `luck`); win fires `fires` +
    #                       posts `success_message`, miss fires `fail_fires` +
    #                       posts `failure_message`
    # Activation via a command's start_events is intelligent: a running event
    # posts `event_in_process_message`, one on cooldown posts `event_cooldown_message`;
    # otherwise it activates (posting the event's `activation_message`) and, when it
    # finishes, posts `end_message` and starts its per-event `cooldown` timer.
    "events": [],
    "event_in_process_message": "⏳ [event] is already running.",
    "event_cooldown_message": "⏳ [event] is on cooldown — [cooldown]s left.",

    # Capacity Events — one-shot triggers at a capacity threshold (1-999%),
    # independent of ranges (their effects beat normal range behaviour; the
    # End / End Sequence overrules them). Each:
    #   {name, enabled, at (1-999),
    #    stop_devices,                                   # abort all fires once, at trigger
    #    disable_range_cmds, disable_range_cmds_scope,   # chat-only; scope: "event"|"session"
    #    disable_always_on, disable_always_on_scope,
    #    pause_events, pause_events_scope,
    #    enable_commands_on, enable_commands: [names],   # usable during the event regardless
    #    actions: [{type: message|fire|roll|capacity|wait, message, seconds,
    #               device_id, dice, sides, capacity_op, capacity_value}]}
    # The action block runs sequentially; the event is "running" (its effects
    # active) until the block finishes. One-shot per session (re-armed by
    # session reset / activation).
    "capacity_events": [],

    # Polls — named, referenced by a "poll" action (in timed events, capacity-
    # event blocks, poll winners) or a command of type "poll". Posted as a rich
    # embed ("Poll: <title>" + body + numbered options); people vote with the
    # vote system command (!agvote N). One poll runs at a time; a poll inside
    # an event's action block keeps that event 'running' until it completes.
    # Each: {name, title, body, duration (s), repeat_every (s, 0=off),
    #        options: [up to 4 of {label, fallback, actions: [action rows]}]}
    # The winning option's actions execute on completion; the fallback option
    # wins when nobody votes (else nothing happens).
    "polls": [],

    # Preset broadcast messages — pick one from the Dashboard → Controls dropdown
    # and hit "Broadcast Custom" to post it to the channel. Each: {name, message}.
    "broadcasts": [],

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

    # Optional user allowlist so the bot only reacts to specific people.
    # (guild/channel scoping is listen_targets' job — see migration v2.)
    "allow": {"user_ids": []},

    # Remote access (System tab): OFF = the server binds loopback only (the
    # default, nothing else on the network can even connect). ON = binds
    # 0.0.0.0, and ONLY clients whose IP is in allowed_ips may talk to it
    # (empty list = nobody remote — fail closed). Entries: exact IPs
    # ("192.168.1.23"), wildcards ("192.168.1.*"), or CIDR ("192.168.1.0/24").
    "remote_access": {"enabled": False, "allowed_ips": []},

    "pumpdirect_path": DEFAULT_PUMPDIRECT_PATH,
}


# Field names that are numeric wherever they appear in the config (commands,
# events, ranges, prizes, always-on entries, fire rows, game tiers, templates).
_NUM_FIELDS = {
    "seconds", "dice", "sides", "chance", "luck", "every", "max_repeats",
    "capacity_value", "cooldown", "goal", "uses", "min", "max", "max_uses",
    "pl_bust_start", "pl_bust_step", "pl_max_pumps", "pl_points",
    "sm_symbols", "sm_max_rounds", "sm_reveal",
    "bl_cells", "bl_pops", "bl_points", "rps_wins", "sl_symbols",
    "calibration_seconds_to_100", "factor", "min_seconds", "max_seconds",
    "cooldown_seconds", "system_buffer_seconds", "mock_calibration_seconds_to_100",
}


def _coerce_numbers(node):
    """Recursively force known-numeric fields to real numbers (junk → None).
    The UI interpolates these into HTML attributes assuming they're numbers,
    so a hand-edited or tampered backup can't smuggle markup through them."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, (dict, list)):
                _coerce_numbers(v)
            elif k in _NUM_FIELDS and v is not None and not isinstance(v, bool):
                if isinstance(v, (int, float)):
                    continue
                try:
                    f = float(v)
                    node[k] = int(f) if f == int(f) else f
                except (TypeError, ValueError):
                    node[k] = None
    elif isinstance(node, list):
        for item in node:
            _coerce_numbers(item)
    return node


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    global RECOVERED_FROM
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if not isinstance(stored, dict):
            raise ValueError(f"config root is a {type(stored).__name__}, expected an object")
    except FileNotFoundError:
        stored = {}
    except ValueError as e:
        # Corrupt config: NEVER run silently on defaults — the next save would
        # make the wipe permanent. Move the bad file aside for recovery and
        # flag it so the UI can warn.
        aside = CONFIG_PATH + f".corrupt-{int(time.time())}"
        try:
            os.replace(CONFIG_PATH, aside)
        except OSError:
            aside = "(couldn't move the corrupt file aside)"
        RECOVERED_FROM = aside
        print(f"!! config.json is corrupt ({e}) — moved to {aside}; check data/backups/ to restore")
        stored = {}
    # Migrate the RAW stored config (merging first would inherit DEFAULTS'
    # current config_version and skip every step).
    return _coerce_numbers(_deep_merge(DEFAULTS, _migrate(stored)))


def _migrate(cfg: dict) -> dict:
    """Ordered upgrades for configs written by older versions. Each step bumps
    config_version; unknown future keys always pass through untouched."""
    v = int(cfg.get("config_version") or 0)
    if v < 1:
        # v1: roll.cooldown_reset_message was a dead nested key (nothing ever
        # read it) — hoist it to the live top-level key if that one is blank.
        nested = (cfg.get("roll") or {}).pop("cooldown_reset_message", None)
        if nested and not cfg.get("cooldown_reset_message"):
            cfg["cooldown_reset_message"] = nested
    if v < 2:
        # v2: allow.guild_ids / allow.channel_ids were never read (listen_targets
        # is the real scoping) — drop them so nobody edits a dead knob.
        if isinstance(cfg.get("allow"), dict):
            cfg["allow"].pop("guild_ids", None)
            cfg["allow"].pop("channel_ids", None)
    cfg["config_version"] = CONFIG_VERSION
    return cfg


def _prune(paths: list[str], keep: int) -> None:
    for p in sorted(paths)[:-keep] if len(paths) > keep else []:
        try:
            os.remove(p)
        except OSError:
            pass


def _rotate_backups() -> None:
    """Copy the current config aside before it's overwritten: a rolling ring of
    the last KEEP_BACKUPS saves (throttled to one per minute so a burst of
    autosaves doesn't flush the whole ring) plus one snapshot per day."""
    if not os.path.exists(CONFIG_PATH):
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ring = [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
                if f.startswith("config.ring-")]
        newest = max((os.path.getmtime(p) for p in ring), default=0)
        if time.time() - newest >= 60:
            shutil.copy2(CONFIG_PATH, os.path.join(BACKUP_DIR, f"config.ring-{int(time.time())}.json"))
            _prune([os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
                    if f.startswith("config.ring-")], KEEP_BACKUPS)
        daily = os.path.join(BACKUP_DIR, f"config.daily-{time.strftime('%Y-%m-%d')}.json")
        if not os.path.exists(daily):
            shutil.copy2(CONFIG_PATH, daily)
            _prune([os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
                    if f.startswith("config.daily-")], KEEP_DAILY)
        # The all-time leaderboard is precious too — one snapshot per day.
        life = os.path.join(DATA_DIR, "pumpers_lifetime.json")
        life_daily = os.path.join(BACKUP_DIR, f"pumpers.daily-{time.strftime('%Y-%m-%d')}.json")
        if os.path.exists(life) and not os.path.exists(life_daily):
            shutil.copy2(life, life_daily)
            _prune([os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
                    if f.startswith("pumpers.daily-")], KEEP_DAILY)
    except OSError as e:
        print(f"!! config backup rotation failed: {e}")


def _fsync_dir(path: str) -> None:
    """fsync the directory so the rename itself survives power loss (no-op on
    platforms that can't fsync a directory, e.g. Windows)."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def save(cfg: dict) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    cfg["config_rev"] = int(cfg.get("config_rev") or 0) + 1
    _rotate_backups()
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())   # data on disk BEFORE the rename makes it live
        try:
            os.chmod(tmp, 0o600)   # protect the token on desktop; best-effort on
        except OSError:            # Android, where app-private storage is already isolated
            pass
        os.replace(tmp, CONFIG_PATH)
        _fsync_dir(DATA_DIR)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return cfg


def update(patch: dict) -> dict:
    """Deep-merge a patch into the stored config and persist it."""
    return save(_deep_merge(load(), patch))
