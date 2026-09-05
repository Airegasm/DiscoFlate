"""
config_store.py — persistent DiscoFlate settings (data/config.json).

Holds the Discord token, listener state, the active/registered devices, the
roll settings, and the capacity-range -> dice table. Written atomically with
0600 perms (the file contains a bot token).
"""

from __future__ import annotations

import json
import os
import re
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
CONFIG_VERSION = 11

# The dead "dice recharged" default that a remediation migration accidentally
# promoted to the live cooldown-ready message. Migration v3 undoes that.
_DEAD_RECHARGE_MSG = "🎲 [mention], your dice are recharged — roll again with [roll_cmd]!"

DEFAULTS = {
    "discord_token": "",
    "config_version": CONFIG_VERSION,
    # Bumped on every save; the UI sends the rev it last saw with each config
    # write so a stale tab's snapshot is rejected instead of clobbering.
    "config_rev": 0,
    "command_prefix": "!",
    "listener_enabled": False,
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
    "command_names": {"capacity": "capacity",
                      "help": "aghelp", "leaderboard": "toppumpers",
                      "leaderboard_life": "toppumpers-life", "pumptimer": "pumptimer",
                      "vote": "agvote", "enter": "enter"},
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

    # Reusable templates saved from the Commands / Events / Ranges / Polls /
    # Competitions / Capacity-Events editors, kept per install (and included in
    # config backup/restore). "Add to Config" ports one back into the live config
    # with clash-safe naming. Each list holds whole item objects.
    "templates": {"commands": [], "events": [], "ranges": [],
                  "polls": [], "competitions": [], "capevents": []},

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

    # Bot auto-posts every N seconds to announce_channel. `message` is a custom
    # template ([capacity], [commands]); blank uses the built-in capacity+commands text.
    "auto_report": {"enabled": False, "seconds": 300, "message": ""},

    # Channel the bot posts auto-reports and milestone messages into.
    "announce_channel_id": "",

    # Roll total -> on-time (seconds). (Chat rolls are custom commands with a
    # `roll` action now; this block is the shared dice mechanics they all use.)
    "roll": {
        "mode": "value",        # "value": seconds = roll total
                                # "factor": seconds = roll total * factor
        "factor": 1.0,
        "min_seconds": 1,
        "max_seconds": 20,      # HARD CAP on any single fire
        "disable_at_100": False,
    },

    # Which N-dice-of-S-sides to roll, by current capacity range.
    # Evaluated top-down; first range that contains the capacity wins.
    "capacity_ranges": [
        {"min": 0,   "max": 33,  "dice": 1, "sides": 4},
        {"min": 33,  "max": 66,  "dice": 1, "sides": 6},
        {"min": 66,  "max": 100, "dice": 2, "sides": 6},
    ],

    # User-defined commands, created in the web UI on the fly. A command is its
    # GATES plus an ACTION BLOCK (v10: the old fire/roll/say/poll/chance types
    # collapsed into action rows; replies are message rows). Each:
    #   {name, type ("actions" | "game-*"), enabled, owner_only, hide_in_list,
    #    description, start_events, range_gate, react_only, react_emoji,
    #    actions: [action rows — see capacity_events below for the full row set]}
    # react_only → acknowledge the command with a reaction (react_emoji, default 💨)
    #   on the caller's message (cuts chat spam for rapid-fire commands). The
    #   block still runs; cross-server echo is skipped.
    # A `roll` action's dice: the RANGE's dice/luck (its range-dice row) win;
    #   the row's own dice/sides/luck are the fallback (for always-on rolls).
    # type "game-*" (pushluck/simon/balloon/rps/slots/blackjack) → button/
    #   ephemeral minigames (minigames.py). Params: pl_* (pushluck), sm_* (simon),
    #   bl_cells/bl_pops/bl_points (balloon), rps_wins (rps), sl_symbols (slots).
    #   game_intro = message with the Play button (game_intro_embed +
    #   game_intro_title post it as an embed). game_tiers = [{op, min,
    #   actions}] score→outcome (v11): the highest matching value (per `op`:
    #   >= > = != < <=) the final score reaches runs its ACTION BLOCK after the
    #   labeled score line posts (fires credited to the player). A luck modifier (a flat ± added to the
    #   final score before tier lookup) may be set per range (the range's
    #   cooldowns[cmd].luck) or on the Always-On entry; a range the game is a
    #   member of takes precedence over Always-On. [score] and [luck] are
    #   available in tier messages. Every result is labelled with the game and
    #   respects cross-server anonymity (real name only where a member).
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

    # Timed events — THREE ACTION BLOCKS on a timer (v10: the old per-type
    # fields and activation/end messages collapsed into blocks):
    #   activation_actions (once, when a command starts it, before round 1) →
    #   actions (each round) → post_actions (when the loop ends, detached).
    # Each: {name, enabled, mode ("loop"|"once"), every (secs), max_repeats,
    #        cooldown, fire_immediately, clean_previous,
    #        activation_actions: [...], actions: [...], post_actions: [...]}
    #   mode "loop" → runs the round block every `every` seconds, repeatedly (up
    #                 to `max_repeats` times if set; 0/blank = unlimited).
    #                 clean_previous → each round's message rows replace the
    #                 previous round's; the end block's first message replaces
    #                 the last round.
    #   mode "once" → one round, `every` seconds after arming.
    # Rounds run DETACHED (a wait/poll inside never stalls the session loop) and
    # never overlap: a new round waits for the previous round's block to finish.
    # Activation via a command's start_events is intelligent: a running event
    # posts `event_in_process_message`, one on cooldown posts
    # `event_cooldown_message`; otherwise it activates, runs activation_actions,
    # and when it finishes starts its per-event `cooldown` timer.
    "events": [],
    "event_in_process_message": "⏳ [event] is already running.",
    "event_cooldown_message": "⏳ [event] is on cooldown — [cooldown]s left.",

    # Capacity Events — one-shot triggers at a capacity threshold (1-999%),
    # independent of ranges (their effects beat normal range behaviour; the
    # End / End Sequence overrules them). Each:
    #   {name, enabled, at (1-999),
    #    # command gating (disable range/always-on cmds, pause events, allow
    #    # specific ones) is done with a command_gate action IN the block below —
    #    # the old disable_*/pause_events/enable_commands tickboxes migrated there
    #    # (v9), and the stop_devices tickbox became a stop_devices ACTION (v10).
    #    actions: [{type: message|broadcast|command|fire|roll|capacity|wait|poll|
    #               competition|bonus_round|award|command_gate|stop_devices|end_session,
    #               # message: style (plain|embed) + title + message; an OPTIONAL
    #               #   button via target (""=none / winner / runnerup / range_leader
    #               #   / session_leader / top_bonus_holder / everyone / allowlist)
    #               #   + allow:[…] + label + freeze + deadline + deadline_message +
    #               #   timeout_message + actions:[…] (run on press/deadline). Folds
    #               #   in the old message/embed/winner_button/session_leader_event.
    #               # award: award_type (command|secs|pct) + target; command form
    #               #   adds command + charges + stash + lock + deadline +
    #               #   deadline_message + timeout_message; secs/pct form adds amount
    #               #   (banked for a Bonus Round). Folds in award_prize/award_amount.
    #               command (run a named custom command),
    #               bonus_round (name),   # start a named Bonus Round
    #               seconds (number OR [placeholder]),
    #               device_id, dice, sides, capacity_op, capacity_value,
    #               poll, broadcast, competition,
    #               fire_mode (seconds|add|to) + fill_pct (fire: pump until N%
    #                 added / reached; [secs] = the computed time) + block_during +
    #                 post_actions:[…],
    #               stop_devices: no fields — aborts ALL fires now (+ optional message),
    #               modifiers:[{op,command?,event?}] (command_gate: block_all|unblock_all|
    #                 remove_block|allow|unallow|block|unblock|block_event|unblock_event|
    #                 disable_range_cmds|resume_range_cmds|disable_always_on|
    #                 resume_always_on|pause_events|resume_events|pause_capacity|
    #                 resume_capacity; command for the cmd ops, event (timed/capacity
    #                 event name) for the event ops)}]}
    # The action block runs sequentially; the event is "running" (its effects
    # active) until the block finishes. One-shot per session (re-armed by
    # session reset / activation).
    "capacity_events": [],

    # Competitions ("roll-offs" & friends) — named, started by a "competition"
    # action (timed events / capacity-event blocks). Players type the enter
    # command to join, then compete via the entry command during a window; the
    # winner (by type/metric) gets rewards. Each:
    # Players join via an "Enter Challenge" button on the embed and roll
    # privately (ephemeral), with an optional reroll budget (only the latest
    # roll; earlier rolls lock); results post per-player all at once. Each:
    #   {name, type ("rolloff"|"race"|"raffle"), command (entry command),
    #    duration, require_enter, required_entries, max_entries, metric
    #    ("total"|"highest"|"count"), allow_reroll, reroll_count, roll_specs,
    #    repeat_every, repeat_message, title, body, intro, entry_message,
    #    win_message, no_winner_message,
    #    add_all_totals (bool: fire EVERY finisher's total, each credited),
    #    win_actions: [action rows run on a win — fire [winner_score], an embed
    #      with a gated button, award_prize a command, command_gate others, …],
    #    no_winner_actions: [action rows run when nobody qualified]}
    #    Legacy award_pump/bonus_command/lock/deadline fields migrate to
    #    win_actions (config_version 4).
    "competitions": [],

    # Bonus Rounds ("teamwork" cash-ins) — named, started by a "bonus_round"
    # action. award_amount banks per-player bonus AMOUNTS (pump secs / cap %); a
    # Bonus Round posts an embed with a Confirm button for bonus holders. When the
    # needed holders confirm (all, or the top holder) before the timer, its action
    # block runs with the pooled [total_bonus_secs]/[total_bonus_pct], then the
    # banks are spent (cleared). Each: {name, type ("teamwork"), title, body,
    # duration (s), confirm ("all"|"leader"), actions: [action rows],
    # no_holders_message, expire_message}
    "bonus_rounds": [],

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
    # Perfect Prizes (v10) — achievement-triggered awards. Each:
    #   {name, enabled, counter, goal, range_gate, progress_message,
    #    actions: [unlock block — e.g. award (target "[user]") + message rows]}
    # A PERFECT roll (a roll action hitting its maximum total) bumps that
    # person's "roll" counter automatically; an `achievement` action row (e.g.
    # on a blackjack 21 tier) bumps any named counter. When a watched counter
    # reaches `goal` (inside the prize's range gate), the unlock block runs for
    # the earner and their progress resets — prizes are re-earnable. Progress
    # placeholders: [count] [goal] [remaining] [counter] [prize].
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

    # Named gameplay presets (System tab) — saved snapshots of the Game/Commands/
    # Events/Templates tabs you can switch between. Separate from the live config:
    # your ongoing edits autosave to the live config, NOT to a preset. Each:
    # {name, data: {<safe gameplay keys>}}. Managed only via /api/gameplay/preset.
    "gameplay_presets": [],

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
    "charges",
    "pl_bust_start", "pl_bust_step", "pl_max_pumps", "pl_points",
    "sm_symbols", "sm_max_rounds", "sm_reveal",
    "bl_cells", "bl_pops", "bl_points", "rps_wins", "sl_symbols",
    "calibration_seconds_to_100", "factor", "min_seconds", "max_seconds",
    "cooldown_seconds", "system_buffer_seconds", "mock_calibration_seconds_to_100",
}


# Fields that accept a NUMBER OR a bare [placeholder] token (the engine's
# _num_expr renders it at run time) — e.g. a fire row's seconds = [winner_score].
_PLACEHOLDER_NUM_FIELDS = {"seconds", "fill_pct", "amount", "deadline"}
_PLACEHOLDER_TOKEN = re.compile(r"^\s*\[[\w-]+\]\s*$")


def _coerce_numbers(node):
    """Recursively force known-numeric fields to real numbers (junk → None).
    The UI interpolates these into HTML attributes assuming they're numbers,
    so a hand-edited or tampered backup can't smuggle markup through them.
    Placeholder-capable fields keep a bare [token] (always esc()'d in the UI)."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, (dict, list)):
                _coerce_numbers(v)
            elif k in _NUM_FIELDS and v is not None and not isinstance(v, bool):
                if isinstance(v, (int, float)):
                    continue
                if (k in _PLACEHOLDER_NUM_FIELDS and isinstance(v, str)
                        and _PLACEHOLDER_TOKEN.match(v)):
                    node[k] = v.strip()
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


_FACTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default_config.json")


def _factory_seed() -> dict:
    """The shipped factory config (Basic Session preloaded) — used to pre-fill a
    brand-new install's fields. Empty dict if it isn't bundled."""
    try:
        with open(_FACTORY_PATH, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def load() -> dict:
    global RECOVERED_FROM
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if not isinstance(stored, dict):
            raise ValueError(f"config root is a {type(stored).__name__}, expected an object")
    except FileNotFoundError:
        stored = _factory_seed()   # first run → start pre-loaded with the factory config
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


_LEGACY_EMBED_TYPES = {"embed_message", "winner_button", "session_leader_event"}
_KNOWN_BUTTON_TARGETS = {"winner", "runnerup", "range_leader", "session_leader",
                         "top_bonus_holder", "everyone", "all", "anyone"}


def _walk_migrate_actions_v8(node) -> None:
    """Recursively fold award_prize/award_amount → `award` (award_type) and
    `embed` → `message` (style=embed), anywhere in the config."""
    if isinstance(node, dict):
        t = (node.get("type") or "").lower()
        if t == "award_prize":
            node["type"] = "award"; node["award_type"] = "command"
        elif t == "award_amount":
            unit = (node.get("unit") or "secs").lower()
            node["type"] = "award"
            node["award_type"] = "pct" if unit in ("pct", "%", "cap", "capacity") else "secs"
        elif t == "embed":
            node["type"] = "message"; node["style"] = "embed"
        for v in node.values():
            _walk_migrate_actions_v8(v)
    elif isinstance(node, list):
        for item in node:
            _walk_migrate_actions_v8(item)


def _walk_migrate_embeds(node) -> None:
    """Recursively fold legacy embed_message/winner_button/session_leader_event
    action rows (anywhere — nested blocks, templates) into the unified `embed`."""
    if isinstance(node, dict):
        t = (node.get("type") or "").lower()
        if t == "embed_message":
            node["type"] = "embed"; node.setdefault("target", ""); node.setdefault("actions", [])
        elif t == "session_leader_event":
            node["type"] = "embed"; node["target"] = "session_leader"; node.setdefault("actions", [])
        elif t == "winner_button":
            raw = node.get("target") or "winner"
            tok = str(raw).strip().strip("[]").lower()
            if tok in _KNOWN_BUTTON_TARGETS:
                node["target"] = tok
            else:
                node["target"] = "allowlist"; node["allow"] = [raw]
            node["type"] = "embed"; node.setdefault("actions", [])
        for v in node.values():
            _walk_migrate_embeds(v)
    elif isinstance(node, list):
        for item in node:
            _walk_migrate_embeds(item)


def _v10_msg_row(text, if_option=None):
    row = {"type": "message", "message": text}
    if if_option:
        row["if_option"] = if_option
    return row


def _v10_fire_rows(fires):
    """Legacy device-fire rows ({device_id, seconds}) → fire actions."""
    return [{"type": "fire", "fire_mode": "seconds",
             "seconds": (r.get("seconds") if r.get("seconds") is not None else 3),
             "device_id": r.get("device_id") or None}
            for r in (fires or []) if isinstance(r, dict)]


def _v10_actions(rows):
    """The bot speaks ONLY through message rows now: pull each legacy per-action
    announce text (fire/roll/award/command_gate/stop_devices after the row,
    end_session before it) out into a real message row. Recursive over nested
    blocks; idempotent (the extracted keys are removed)."""
    out = []
    for a in (rows or []):
        if not isinstance(a, dict):
            continue
        t = (a.get("type") or "message").lower()
        for sub in ("actions", "post_actions", "win_actions", "miss_actions"):
            if a.get(sub):
                a[sub] = _v10_actions(a[sub])
        if t in ("fire", "roll", "award", "command_gate", "stop_devices", "end_session"):
            msg = (a.pop("message", "") or "").strip()
            if msg:
                if t == "award":   # award messages addressed the TARGET
                    msg = msg.replace("[mention]", "[target_mention]")
                mrow = _v10_msg_row(msg, a.get("if_option"))
                if t == "end_session":   # nothing runs after end_session
                    out.append(mrow)
                    out.append(a)
                else:
                    out.append(a)
                    out.append(mrow)
                continue
        out.append(a)
    return out


def _v10_command(c: dict) -> None:
    """Collapse a legacy-typed command (fire/roll/say/poll/chance) into a pure
    'actions' command; its reply becomes a trailing message row ([secs]/[result]
    flow through the block context). Minigame types keep their mechanics."""
    t = (c.get("type") or "fire").lower()
    if t.startswith("game-"):
        for tier in (c.get("game_tiers") or []):
            if isinstance(tier, dict) and tier.get("actions"):
                tier["actions"] = _v10_actions(tier["actions"])
        c.pop("reply", None)
        return
    name = (c.get("name") or "").strip()
    reply = (c.get("reply") or "").strip()
    rows: list = []
    if t == "actions":
        rows = list(c.get("actions") or [])
        if not reply and not c.get("mention"):
            # nothing to fold in — just normalize the rows
            c["actions"] = _v10_actions(rows)
            for dead in ("reply", "mention", "seconds", "dice", "sides", "device_id",
                         "fire_until", "fires", "fail_fires", "chance", "luck",
                         "success_reply", "failure_reply", "win_actions", "miss_actions", "poll"):
                c.pop(dead, None)
            return
    elif t == "say":
        reply = reply or (f"{name}!" if name else "")
    elif t == "poll":
        rows = [{"type": "poll", "poll": c.get("poll") or None}]
    elif t == "roll":
        rows = [{"type": "roll", "dice": c.get("dice") or None,
                 "sides": c.get("sides") or None, "device_id": c.get("device_id") or None}]
        reply = reply or "🎲 **[user]** rolled [dice] = **[result]** → **[secs]s**"
    elif t == "chance":
        win = _v10_fire_rows(c.get("fires")) + list(c.get("win_actions") or [])
        miss = _v10_fire_rows(c.get("fail_fires")) + list(c.get("miss_actions") or [])
        if not c.get("react_only"):
            sr = (c.get("success_reply") or "").strip() or \
                "🎲 **[user]** rolled [roll] vs [chance]% — **win!**"
            fr = (c.get("failure_reply") or "").strip() or \
                "🎲 **[user]** rolled [roll] vs [chance]% — no luck."
            if c.get("mention"):
                sr = sr if "[mention]" in sr else "[mention] " + sr
                fr = fr if "[mention]" in fr else "[mention] " + fr
            win.append(_v10_msg_row(sr))
            miss.append(_v10_msg_row(fr))
        rows = [{"type": "chance",
                 "chance": (c.get("chance") if c.get("chance") is not None else 50),
                 "luck": c.get("luck"), "win_actions": win, "miss_actions": miss}]
        reply = ""   # the outcome blocks carry the talk
    else:   # fire (the old default)
        fu = c.get("fire_until")
        if fu not in (None, "", 0):
            main = {"type": "fire", "fire_mode": "to", "fill_pct": fu,
                    "device_id": c.get("device_id") or None}
        else:
            main = {"type": "fire", "fire_mode": "seconds",
                    "seconds": (c.get("seconds") if c.get("seconds") not in (None, "", 0) else 3),
                    "device_id": c.get("device_id") or None}
        rows = [main] + _v10_fire_rows(c.get("fires"))
        reply = reply or ("🔥 **[user]** ran **" + (name or "it") + "** → **[secs]s**")
    if reply and not c.get("react_only"):
        if c.get("mention") and "[mention]" not in reply:
            reply = "[mention] " + reply
        rows = rows + [_v10_msg_row(reply)]
    c["type"] = "actions"
    c["actions"] = _v10_actions(rows)
    for dead in ("reply", "mention", "seconds", "dice", "sides", "device_id", "fire_until",
                 "fires", "fail_fires", "chance", "luck", "success_reply", "failure_reply",
                 "win_actions", "miss_actions", "poll"):
        c.pop(dead, None)


def _v10_event(e: dict) -> None:
    """Collapse a legacy-typed timed event (message/broadcast/poll/competition/
    capacity/fire/roll/chance) into its pure action blocks. An event is now
    THREE blocks: activation_actions (once, when a command starts it) →
    actions (each round) → post_actions (when the loop ends)."""
    if "activation_message" in e:
        am = (e.pop("activation_message") or "").strip()
        if am:
            e["activation_actions"] = [_v10_msg_row(am)] + list(e.get("activation_actions") or [])
    if "end_message" in e:
        em = (e.pop("end_message") or "").strip()
        if em:
            e["post_actions"] = [_v10_msg_row(em)] + list(e.get("post_actions") or [])
    if e.get("activation_actions"):
        e["activation_actions"] = _v10_actions(e["activation_actions"])
    if "action" not in e and "message" not in e:
        if e.get("post_actions"):
            e["post_actions"] = _v10_actions(e["post_actions"])
        return   # already collapsed
    act = (e.pop("action", "") or "message").lower()
    msg = (e.pop("message", "") or "").strip()
    rows: list = []
    if act == "actions":
        rows = list(e.get("actions") or [])
    elif act == "broadcast":
        rows = [{"type": "broadcast", "broadcast": e.get("broadcast") or None}]
        if msg:
            rows.append(_v10_msg_row(msg))
    elif act == "poll":
        # message FIRST: the poll row holds the block until the poll finishes
        if msg:
            rows.append(_v10_msg_row(msg))
        rows.append({"type": "poll", "poll": e.get("poll") or None})
    elif act == "competition":
        rows = [{"type": "competition", "competition": e.get("competition") or None}]
        if msg:
            rows.append(_v10_msg_row(msg))
    elif act == "capacity":
        rows = [{"type": "capacity", "capacity_op": e.get("capacity_op") or "add",
                 "capacity_value": e.get("capacity_value") or 0}]
        if msg:
            rows.append(_v10_msg_row(msg))
    elif act in ("fire", "roll"):
        if act == "fire":
            rows = [{"type": "fire", "fire_mode": "seconds",
                     "seconds": (e.get("seconds") if e.get("seconds") not in (None, "", 0) else 3),
                     "device_id": e.get("device_id") or None}]
        else:
            rows = [{"type": "roll", "dice": e.get("dice") or None,
                     "sides": e.get("sides") or None, "device_id": e.get("device_id") or None}]
        if msg:
            rows.append(_v10_msg_row(msg))
    elif act == "chance":
        win = _v10_fire_rows(e.get("fires"))
        miss = _v10_fire_rows(e.get("fail_fires"))
        sm = (e.get("success_message") or "").strip() or msg
        fm = (e.get("failure_message") or "").strip() or msg
        if sm:
            win.append(_v10_msg_row(sm))
        if fm:
            miss.append(_v10_msg_row(fm))
        rows = [{"type": "chance",
                 "chance": (e.get("chance") if e.get("chance") is not None else 50),
                 "luck": e.get("luck"), "win_actions": win, "miss_actions": miss}]
    else:   # plain message event
        if msg:
            rows = [_v10_msg_row(msg)]
    e["actions"] = _v10_actions(rows)
    if e.get("post_actions"):
        e["post_actions"] = _v10_actions(e["post_actions"])
    for dead in ("seconds", "dice", "sides", "capacity_op", "capacity_value", "device_id",
                 "poll", "broadcast", "competition", "chance", "luck", "fires",
                 "fail_fires", "success_message", "failure_message"):
        e.pop(dead, None)


def _v10_competition(c: dict) -> None:
    """Competitions are fully button-driven: drop the entry command + chat-entry
    fields; win/no-winner messages become embed message rows in their blocks."""
    name = (c.get("name") or "").strip()
    if "win_message" in c:
        wm = (c.pop("win_message") or "").strip() or \
            "🏆 **[winner]** wins with **[winner_score]**!\n\n[results]"
        c["win_actions"] = [{"type": "message", "style": "embed",
                             "title": f"🏁 {name}".strip(), "message": wm}] + \
            list(c.get("win_actions") or [])
    if "no_winner_message" in c:
        nm = (c.pop("no_winner_message") or "").strip() or \
            (f"**{name}**: no qualifying winner." if name else "No qualifying winner.")
        c["no_winner_actions"] = [{"type": "message", "style": "embed",
                                   "title": f"🏁 {name}".strip(), "message": nm}] + \
            list(c.get("no_winner_actions") or [])
    if not (c.get("body") or "").strip() and (c.get("intro") or "").strip():
        c["body"] = c["intro"]
    for dead in ("command", "entry_message", "require_enter", "intro"):
        c.pop(dead, None)
    c["type"] = "rolloff"
    c["win_actions"] = _v10_actions(c.get("win_actions"))
    c["no_winner_actions"] = _v10_actions(c.get("no_winner_actions"))


def _v10_builtin_roll(c: dict) -> None:
    """The builtin chat dice-roll becomes a plain custom command: a `roll`
    action + a message row (the old reply). It joins every range the builtin
    was enabled in, inheriting that range's roll cooldown/scope; the range's
    dice/sides/luck stay on the range-dice row (range values override)."""
    names = c.get("command_names")
    rname = ""
    if isinstance(names, dict):
        rname = (names.pop("roll", "") or "").strip().lower()
    enabled = c.pop("roll_enabled", None)
    reply = ""
    if isinstance(c.get("roll"), dict):
        reply = (c["roll"].pop("reply", "") or "").strip()
    if enabled is None and not rname:
        return   # nothing to convert (already done, or a subset without it)
    rname = rname or "agroll"
    cmds = c.get("commands")
    if enabled is False or not isinstance(cmds, list):
        return   # the builtin was off (or no commands list to add to)
    if any((x.get("name") or "").strip().lower() == rname
           for x in cmds if isinstance(x, dict)):
        return   # a command already owns that name
    cmds.append({
        "name": rname, "type": "actions", "enabled": True,
        "description": "Roll the dice — the result becomes pump seconds",
        "actions": [
            {"type": "roll"},
            {"type": "message", "message": reply or
             "🎲 **[user]** rolled **[dice]** = **[result]** → **[secs]s** · capacity [capacity]%"}]})
    for r in (c.get("capacity_ranges") or []):
        cds = r.get("cooldowns") if isinstance(r, dict) else None
        if not isinstance(cds, dict):
            continue
        e = cds.get("roll")
        if not isinstance(e, dict) or e.get("enabled") is False:
            continue   # roll was disabled in this range → not a member there
        row = {}
        if e.get("seconds") not in (None, ""):
            row["seconds"] = e["seconds"]
        if e.get("scope"):
            row["scope"] = e["scope"]
        cds[rname] = row


def _v10_prizes(c: dict) -> None:
    """Max-Roll Prizes → Perfect Prizes: the old dice-only prize rows (and the
    single legacy max_roll_prize) become counter/goal rows whose unlock is an
    ACTION BLOCK — an award row grants the prize command (now a real, hidden
    custom command) to the earner. Perfect rolls bump the "roll" counter."""
    mrp = c.pop("max_roll_prize", None)
    prizes = c.get("prizes")
    legacy = []
    if isinstance(prizes, list):
        for p in list(prizes):
            if isinstance(p, dict) and "actions" not in p:   # legacy shape
                legacy.append(p)
                prizes.remove(p)
    if not legacy and isinstance(mrp, dict) and mrp.get("enabled") \
            and isinstance(prizes, list) and not prizes:
        legacy.append({**mrp, "range_gate": "all"})
    if not legacy:
        return
    cmds = c.get("commands") if isinstance(c.get("commands"), list) else None
    for p in legacy:
        pcmd = (p.get("command") or "").strip()
        # the prize command becomes a REAL custom command (hidden, range-free —
        # only the award grant makes it usable, and its range_gate limits where)
        if pcmd and cmds is not None and not any(
                (x.get("name") or "").strip().lower() == pcmd.lower()
                for x in cmds if isinstance(x, dict)):
            rows = []
            act = (p.get("action") or "fire").lower()
            if act == "roll":
                rows.append({"type": "roll", "dice": p.get("dice") or None,
                             "sides": p.get("sides") or None,
                             "device_id": p.get("device_id") or None})
            elif act != "say":
                rows.append({"type": "fire", "fire_mode": "seconds",
                             "seconds": (p.get("seconds") if p.get("seconds") not in (None, "", 0) else 5),
                             "device_id": p.get("device_id") or None})
            rep = (p.get("reply") or "").strip()
            if rep:
                rows.append(_v10_msg_row(rep))
            cmds.append({"name": pcmd, "type": "actions", "enabled": True,
                         "hide_in_list": True, "range_gate": p.get("range_gate") or "all",
                         "description": p.get("description") or "", "actions": rows})
        try:
            uses = max(1, int(p.get("uses") or 1))
        except (TypeError, ValueError):
            uses = 1
        acts = []
        if pcmd:
            acts.append({"type": "award", "award_type": "command", "target": "[user]",
                         "command": pcmd, "charges": uses, "stash": True})
        um = (p.get("unlock_message") or "").strip()
        if um:
            um = (um.replace("[prize_cmd]", "[bonus_cmd]").replace("[uses]", "[charges]")
                    .replace("[prize_desc]", p.get("description") or ""))
            acts.append(_v10_msg_row(um))
        prizes.append({"name": (p.get("description") or pcmd or "prize").strip(),
                       "enabled": bool(p.get("enabled", True)),
                       "counter": "roll", "goal": p.get("goal") or 3,
                       "range_gate": p.get("range_gate") or "all",
                       "progress_message": p.get("progress_message") or "",
                       "actions": acts})


def _collapse_v10(c: dict) -> dict:
    """v10 consolidation (shape-keyed & IDEMPOTENT — safe to run on presets,
    templates and gameplay imports of any age): commands & timed events become
    pure action blocks; per-action announce texts become message rows;
    competitions lose the entry-command/chat fields; the capacity-event
    stop_devices tickbox becomes a stop_devices action; the builtin chat
    dice-roll becomes a custom command; the cooldown-ready notice is gone."""
    _v10_builtin_roll(c)
    _v10_prizes(c)
    c.pop("cooldown_reset_message", None)
    # Always-On and range membership are mutually exclusive: a command living
    # in any range is stripped from Always-On (range values rule).
    members = set()
    for r in (c.get("capacity_ranges") or []):
        if isinstance(r, dict):
            members |= {str(k).lower() for k in (r.get("cooldowns") or {})
                        if str(k).lower() != "roll"}
    if members and isinstance(c.get("always_on_commands"), list):
        c["always_on_commands"] = [
            a for a in c["always_on_commands"]
            if (((a.get("name") if isinstance(a, dict) else str(a)) or "").strip().lower())
            not in members]
    for cmd in (c.get("commands") or []):
        if isinstance(cmd, dict):
            _v10_command(cmd)
    for e in (c.get("events") or []):
        if isinstance(e, dict):
            _v10_event(e)
    for e in (c.get("capacity_events") or []):
        if isinstance(e, dict):
            if e.pop("stop_devices", False):
                e["actions"] = [{"type": "stop_devices"}] + list(e.get("actions") or [])
            e["actions"] = _v10_actions(e.get("actions"))
            if e.get("post_actions"):
                e["post_actions"] = _v10_actions(e["post_actions"])
    for comp in (c.get("competitions") or []):
        if isinstance(comp, dict):
            _v10_competition(comp)
    for p in (c.get("polls") or []):
        for o in (p.get("options") or []) if isinstance(p, dict) else []:
            if isinstance(o, dict) and o.get("actions"):
                o["actions"] = _v10_actions(o["actions"])
    for b in (c.get("bonus_rounds") or []):
        if isinstance(b, dict) and b.get("actions"):
            b["actions"] = _v10_actions(b["actions"])
    for p in (c.get("prizes") or []):
        if isinstance(p, dict) and p.get("actions"):
            p["actions"] = _v10_actions(p["actions"])
    t = c.get("templates") or {}
    if t:
        _collapse_v10({"commands": t.get("commands"), "events": t.get("events"),
                       "capacity_events": t.get("capevents"),
                       "competitions": t.get("competitions"), "polls": t.get("polls")})
    return c


def _collapse_v11(c: dict) -> None:
    """v11: minigame tiers are pure `score op N` + ACTION BLOCK — the old
    per-tier device-fire rows and result-message template become fire rows +
    a message row at the front of the tier's block. Shape-keyed & idempotent
    (the popped keys are gone afterwards); walks templates too."""
    def _tier(t):
        fires = t.pop("fires", None)
        msg = (t.pop("message", "") or "").strip()
        if fires or msg:
            rows = _v10_fire_rows(fires) + ([_v10_msg_row(msg)] if msg else [])
            t["actions"] = rows + list(t.get("actions") or [])
    def _cmds(lst):
        for cmd in (lst or []):
            if isinstance(cmd, dict):
                for t in (cmd.get("game_tiers") or []):
                    if isinstance(t, dict):
                        _tier(t)
    _cmds(c.get("commands"))
    _cmds((c.get("templates") or {}).get("commands"))


def _migrate(cfg: dict) -> dict:
    """Ordered upgrades for configs written by older versions. Each step bumps
    config_version; unknown future keys always pass through untouched."""
    v = int(cfg.get("config_version") or 0)
    if v < 1:
        # v1: roll.cooldown_reset_message was always a DEAD nested key (nothing
        # ever read it). Just drop it — do NOT promote it to the live top-level
        # key; the cooldown-ready message stays silent by default.
        (cfg.get("roll") or {}).pop("cooldown_reset_message", None)
    if v < 2:
        # v2: allow.guild_ids / allow.channel_ids were never read (listen_targets
        # is the real scoping) — drop them so nobody edits a dead knob.
        if isinstance(cfg.get("allow"), dict):
            cfg["allow"].pop("guild_ids", None)
            cfg["allow"].pop("channel_ids", None)
    if v < 3:
        # v3: undo the earlier accidental promotion of the dead "dice recharged"
        # message. Drop the dead nested copy, and clear the top-level ONLY if it
        # still holds that exact promoted default (a custom message is kept).
        (cfg.get("roll") or {}).pop("cooldown_reset_message", None)
        if (cfg.get("cooldown_reset_message") or "").strip() == _DEAD_RECHARGE_MSG:
            cfg["cooldown_reset_message"] = ""
    if v < 4:
        # v4: competition endings became an ACTION BLOCK (win_actions). Convert
        # the old bespoke award fields (award_pump / bonus command / lock /
        # deadline) into an equivalent block, then drop the dead keys.
        for c in (cfg.get("competitions") or []):
            if not isinstance(c, dict) or c.get("win_actions"):
                continue
            typ = (c.get("type") or "rolloff").lower()
            acts = []
            if c.get("award_pump"):
                secs = "[winner_score]" if typ == "rolloff" else (c.get("award_seconds") or 0)
                acts.append({"type": "fire", "seconds": secs, "message": ""})
                if c.get("bonus_after_pump", True):
                    acts.append({"type": "wait", "seconds": secs})
            if c.get("bonus_command_on") and (c.get("bonus_command") or "").strip():
                stash = bool(c.get("bonus_stashable"))
                acts.append({"type": "award_prize", "target": "[winner]",
                             "command": (c.get("bonus_command") or "").strip(), "charges": 0,
                             "stash": stash, "lock": bool(c.get("lock_progression")),
                             "deadline": 0 if stash else (c.get("winner_deadline") or 0),
                             "message": c.get("bonus_message") or "",
                             "deadline_message": c.get("deadline_message") or "",
                             "timeout_message": c.get("timeout_message") or ""})
            c["win_actions"] = acts
            c.setdefault("no_winner_actions", [])
            c.setdefault("add_all_totals", False)
            for dead in ("award_pump", "award_seconds", "bonus_command_on", "bonus_command",
                         "bonus_stashable", "lock_progression", "bonus_after_pump",
                         "bonus_message", "winner_deadline", "deadline_message", "timeout_message"):
                c.pop(dead, None)
    if v < 5:
        # v5: race & raffle competition types were removed (pending a proper
        # redesign) — they forced pointless dice rolls. Any legacy competition of
        # those types becomes a roll-off (highest score wins).
        for c in (cfg.get("competitions") or []):
            if isinstance(c, dict) and (c.get("type") or "").lower() in ("race", "raffle"):
                c["type"] = "rolloff"
    if v < 6:
        # v6: embed_message + winner_button + session_leader_event collapsed into
        # one unified `embed` action (title/body + optional gated button + block).
        _walk_migrate_embeds(cfg)
    if v < 7:
        # v7: chance commands' device-only win/miss fire rows become win_actions /
        # miss_actions action blocks (so a gamble can do anything an event can).
        def _fires_to_actions(rows):
            return [{"type": "fire", "device_id": (r.get("device_id") or None),
                     "seconds": (r.get("seconds") if r.get("seconds") is not None else 3)}
                    for r in (rows or []) if isinstance(r, dict)]
        for c in (cfg.get("commands") or []):
            if isinstance(c, dict) and (c.get("type") or "").lower() == "chance":
                c["win_actions"] = _fires_to_actions(c.get("fires")) + list(c.get("win_actions") or [])
                c["miss_actions"] = _fires_to_actions(c.get("fail_fires")) + list(c.get("miss_actions") or [])
                c["fires"] = []; c["fail_fires"] = []
    if v < 8:
        # v8: award_prize + award_amount → one `award` (award_type command/pct/
        # secs); the `embed` action folds into `message` (style plain/embed).
        _walk_migrate_actions_v8(cfg)
    if v < 9:
        # v9: capacity-event gating TICKBOXES become command_gate rows inside the
        # event's action block. Event-scoped effects disable at the start of the
        # block and resume at the end (auto-lift); session-scoped ones just
        # disable (a later command/event can resume them). enable_commands →
        # allow rows (a universal bypass), unallowed at block end (event-scoped).
        for e in (cfg.get("capacity_events") or []):
            if not isinstance(e, dict):
                continue
            disables = []
            resumes = []
            for tick, dis_op, res_op in (
                    ("disable_range_cmds", "disable_range_cmds", "resume_range_cmds"),
                    ("disable_always_on", "disable_always_on", "resume_always_on"),
                    ("pause_events", "pause_events", "resume_events")):
                if e.get(tick):
                    disables.append({"op": dis_op})
                    if (e.get(tick + "_scope") or "event") != "session":
                        resumes.append({"op": res_op})
            allows = list(e.get("enable_commands") or []) if e.get("enable_commands_on") else []
            pre = [{"op": "allow", "command": c} for c in allows] + disables
            resumes += [{"op": "unallow", "command": c} for c in allows]   # enable is during-event
            if pre:
                acts = list(e.get("actions") or [])
                e["actions"] = ([{"type": "command_gate", "modifiers": pre}] + acts
                                + ([{"type": "command_gate", "modifiers": resumes}] if resumes else []))
            for dead in ("disable_range_cmds", "disable_range_cmds_scope",
                         "disable_always_on", "disable_always_on_scope",
                         "pause_events", "pause_events_scope",
                         "enable_commands_on", "enable_commands"):
                e.pop(dead, None)
    if v < 10:
        # v10: THE consolidation finale — see _collapse_v10. Saved gameplay
        # presets carry the same structures, so convert theirs too.
        _collapse_v10(cfg)
        for p in (cfg.get("gameplay_presets") or []):
            if isinstance(p, dict):
                _collapse_v10(p.get("data") or {})
    if v < 11:
        # v11: minigame tiers become pure action blocks (see _collapse_v11).
        _collapse_v11(cfg)
        for p in (cfg.get("gameplay_presets") or []):
            if isinstance(p, dict):
                _collapse_v11(p.get("data") or {})
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
