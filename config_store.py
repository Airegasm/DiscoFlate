"""
config_store.py — persistent DiscoFlate settings (data/config.json).

Holds the Discord token, listener state, the active/registered devices, the
roll settings, and the capacity-range -> dice table. Written atomically with
0600 perms (the file contains a bot token).
"""

from __future__ import annotations

import base64
import copy
import json
import os
import uuid
import re
import shutil
import tempfile
import time

try:
    import pyaes   # pure-Python AES, already shipped for the Tapo KLAP driver
except Exception:  # noqa: BLE001 — optional: without it the token stays plaintext
    pyaes = None

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
CONFIG_VERSION = 17

# The scene we ship read-only. It is the baseline every install can fall back
# to and compare against, so nothing may write to it.
DEFAULT_SCENE_NAME = "DiscoFlate Default"

# The starter scene. It ships as a carbon copy of the read-only baseline and
# keeps tracking it — change what we ship and an untouched copy follows — right
# up until the operator makes it theirs by editing or renaming it. From that
# moment it is their scene and we never touch it again. `pristine` is the mark
# of "still ours"; the first edit clears it for good.
STARTER_SCENE_NAME = "Your Scene"

# The gameplay half of the config: everything that defines HOW THE SHOW PLAYS,
# as opposed to the infrastructure it runs on (token, devices, calibration,
# channels, scores). A SCENE owns this set outright — pick a scene and you have
# picked its look, its rules and its wording together. Nothing can point at a
# group that belongs to some other scene, because there is no longer a second
# axis to get out of step.
GAMEPLAY_KEYS = [
    # Commands tab
    "command_prefix", "command_names", "roll", "system_buffer_seconds",
    "capacity_message", "pumptimer_message", "pump_message",
    "cooldown_message",
    "capacity_embed", "capacity_title", "pumptimer_embed", "pumptimer_title",
    "cooldown_embed", "cooldown_title", "pump_embed", "pump_title",
    "commands", "broadcasts", "modes", "prizes", "owner_commands", "chat_buttons",
    # Game tab
    "cooldown_seconds", "auto_report",
    "listener_message_on", "listener_message_off",
    "listener_on_embed", "listener_on_title", "listener_off_embed", "listener_off_title",
    "pause_message", "resume_message", "paused_notice_message",
    "pause_embed", "pause_title",
    "output_headers", "rich_output",
    "capacity_ranges", "always_on_enabled", "always_on_commands",
    # Events tab
    "events", "capacity_events", "polls", "competitions", "bonus_rounds", "minigames",
    "event_in_process_message", "event_cooldown_message",
    # Templates tab
    "templates",
]

def scene_gameplay(cfg: dict, name: str | None = None) -> dict:
    """The gameplay block of one scene (the live one by default)."""
    want = str(name if name is not None else cfg.get("chat_scene") or "").strip().lower()
    for sc in (cfg.get("scenes") or []):
        if str(sc.get("name") or "").strip().lower() == want:
            gp = sc.get("gameplay")
            return gp if isinstance(gp, dict) else {}
    return {}


def live_gameplay(cfg: dict) -> dict:
    """The live scene's gameplay dict, MUTABLE and created in place if absent.

    For handlers that flip a flag and save the whole config. Falls back to the
    config itself when there is no live scene, so a scene-less install keeps
    working exactly as before.
    """
    want = str(cfg.get("chat_scene") or "").strip().lower()
    for sc in (cfg.get("scenes") or []):
        if str(sc.get("name") or "").strip().lower() == want:
            if not isinstance(sc.get("gameplay"), dict):
                sc["gameplay"] = {}
            return sc["gameplay"]
    return cfg


def resolved(cfg: dict) -> dict:
    """`cfg` with the live scene's gameplay layered over the top level.

    Every reader downstream keeps calling cfg.get("command_prefix") and simply
    gets the live scene's answer. The top-level values survive as the fallback
    for a scene that has no block of its own yet.
    """
    gp = scene_gameplay(cfg)
    if not gp:
        return cfg
    return {**cfg, **{k: v for k, v in gp.items() if k in GAMEPLAY_KEYS}}



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
                      "vote": "agvote"},
    # The !pumptimer built-in reply (always available). Placeholders: [timer]/[total_secs].
    "pumptimer_message": "⏱️ [timer] seconds left on the pump timer.",
    # Per-message embed toggles + titlebars for the built-in replies/broadcasts.
    "capacity_embed": False, "capacity_title": "",
    "pumptimer_embed": False, "pumptimer_title": "",
    "cooldown_embed": False, "cooldown_title": "",
    "pump_embed": False, "pump_title": "",

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
    # Post the ON/OFF message as an embed with this titlebar (blank = default).
    "listener_on_embed": False,
    "listener_on_title": "",
    "listener_off_embed": False,
    "listener_off_title": "",

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
    # ONE embed toggle + titlebar shared by all three pause/resume messages.
    "pause_embed": False,
    "pause_title": "",

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
    "auto_report": {"enabled": False, "seconds": 300, "message": "",
                    "embed": False, "title": ""},

    # Channel the bot posts auto-reports and milestone messages into.

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

    # Owner Commands (#name): message macros ONLY THE OWNER fires — posted
    # ATTRIBUTED TO THE OWNER ("**Owner:** …") instead of as the bot. Usable
    # from the Chat tab box, typed in Discord by the owner, from Custom
    # Buttons, and from any action block's `command` row as "#name".
    # Each: {name, message}
    "owner_commands": [],
    # Chat-tab Custom Buttons: one-click owner launchers.
    # Each: {label, kind ("command"|"poll"|"owner"), name}
    "chat_buttons": [],
    # Overlay actions (Chat tab, VIDEO channels): buttons that play a timed
    # image layer over the virtual camera. Each: {label, image, seconds, pos, scale}

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

    # Scenes (Scenes tab) — OBS-style overlay designs. Each:
    #   {name, preset, width, height, groups: [scene-group names the scene owns —
    #    so an EMPTY group persists], hidden_groups: [names muted on the tab],
    #    overlays: [{id, label, kind, media,
    #    x, y, w, z, group, mode, seconds, layer, visible}]}
    # kind: media | text | timer | capacity_gauge | device_timers | poll_viewer.
    # x/y/w are FRACTIONS of the scene frame (0..1); z is draw order; `group`
    # tags an overlay into a named set fired together (scoped to its scene).
    # visible=true → always on; false → callable. Scenes live OUTSIDE gameplay
    # presets (a scene LINKS a preset by name) but ride along in Device Sync
    # with their media files.
    "scenes": [],
    # Minigame PROFILES: a named game config a `minigame` action calls by name.
    # [{id,name,kind,config{},tiers[],luck,intro,start_events[]}] — two profiles
    # of the same kind can differ in limits, luck and score tiers.
    "minigames": [],
    "scene_globals": [],   # overlay items callable from every scene
    # Group metadata for that pool, mirroring a scene's own shape — so an
    # empty global group survives a reload the same way a scene's does.
    "scene_globals_meta": {"groups": [], "hidden_groups": [], "intro_groups": []},
    "chat_scene": "",      # the Scene linked to the Chat tab's video section
    # Which Gameplay Preset the live gameplay was last LOADED from. A readout
    # only: loading copies a preset into the live config, and editing the tabs
    # afterwards never writes back to it.
    "preset_loaded": "",
    # Two overlays the SESSION drives (picked in Dashboard -> Go Live Options):
    # notify_overlay = the ONE Text overlay every command's 📣 line writes to;
    # pause_overlay  = a scene GROUP played over everything while paused.
    "notify_overlay": "",
    "pause_overlay": "",

    # Go Live options (Dashboard). Switching LIVE on can open with an INTRO
    # instead of starting the game immediately: an optional scene group plays,
    # an optional announcement posts, and — crucially — commands stay held
    # until the intro ends, so nobody can pump during the pre-show. `commands`
    # lists command names that ARE allowed during the intro (e.g. a signup).
    # Placeholders: [intro_timer] counts down in messages and overlay text.
    "golive": {
        "intro_enabled": False,
        # what a viewer sees if they pick the virtual camera before you go
        # live — blank for a genuinely black screen
        "standby_text": "STARTING SOON",
        "seconds": 15,              # 0 = hold until you press Start now
        "scene_group": "",
        "announce": "",             # blank = say nothing
        "announce_image": "",       # posted WITH the first announcement only
        "announce_every": 0,        # repeat/edit interval, 0 = post once
        "after_group": "",          # scene group to play once the intro ends
        "blackout": True,           # black the camera until the intro ends
        "hold_commands": True,
        "commands": [],             # allowed anyway during the intro
    },

    # How the virtual camera was last started, so a `camera: start` action can
    # bring it up exactly the same way (device, size, fps, linked scene).
    "vcam_last": {},

    # Virtual camera: mirror the OUTGOING feed. Default off — viewers get the
    # true image and overlay text reads correctly to them. (Discord mirrors
    # your own self-view preview on its end; that's cosmetic and local.)
    # Applied to the raw frame BEFORE overlays composite either way.
    "vcam_mirror": False,
    # Your webcam and the size you send: hardware, not show design. Global on
    # purpose — switching scenes must never re-point your camera.
    "vcam_device": 0,
    "vcam_size": "1280x720",

    # Chat tab Isolate: broadcasts go ONLY to the chat tab's active channel
    # while enabled (other live channels still hear direct command replies).
    "chat_isolate": False,
    "chat_isolate_channel": "",

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


# ---- token at rest: AES-CTR with a per-install key ---------------------------
# config.json is the file people screenshot, share for help, and hand-edit —
# the bot token shouldn't sit in it as plaintext. AES-CTR (pyaes) with a random
# key in data/token.key keeps it opaque there; load() hands callers plaintext.
# Ceiling: someone with BOTH files in data/ can still recover it — that's
# inherent to a self-hosted bot that must present the real token to Discord.
_TOK_PREFIX = "enc1:"


def _token_key() -> bytes:
    path = os.path.join(DATA_DIR, "token.key")
    try:
        with open(path, "rb") as fh:
            k = fh.read()
        if len(k) == 32:
            return k
    except OSError:
        pass
    k = os.urandom(32)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(k)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return k


def _enc_token(tok: str) -> str:
    if not tok or pyaes is None or tok.startswith(_TOK_PREFIX):
        return tok
    nonce = os.urandom(16)
    ctr = pyaes.AESModeOfOperationCTR(
        _token_key(), counter=pyaes.Counter(initial_value=int.from_bytes(nonce, "big")))
    ct = ctr.encrypt(tok.encode("utf-8"))
    return _TOK_PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def _dec_token(tok: str) -> str:
    if not tok or not tok.startswith(_TOK_PREFIX):
        return tok
    if pyaes is None:
        return ""
    try:
        raw = base64.b64decode(tok[len(_TOK_PREFIX):])
        ctr = pyaes.AESModeOfOperationCTR(
            _token_key(), counter=pyaes.Counter(initial_value=int.from_bytes(raw[:16], "big")))
        return ctr.decrypt(raw[16:]).decode("utf-8")
    except Exception:  # noqa: BLE001 — corrupt blob / wrong key: treat as no token
        return ""


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
    cfg = _coerce_numbers(_deep_merge(DEFAULTS, _migrate(stored)))
    # an untouched starter picks up whatever we ship now, without waiting for
    # a save or a config-version bump
    _sync_starter(cfg, None, compare=False)
    cfg["discord_token"] = _dec_token(cfg.get("discord_token") or "")
    return cfg


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


def _games_to_profiles(block: dict) -> None:
    """Turn legacy `type: game-*` commands into minigame PROFILES + a minigame
    action row, in place. v13 did this for the whole config; a SCENE carries its
    own commands now, so each scene's block needs the same treatment — including
    the ones we ship, which v14 skips because they already have a block.
    """
    # only the fields that KIND actually reads — a command carries defaults
    # for every game type, and copying them all makes a noisy profile
    _BY_KIND = {
        "pushluck": ("pl_bust_start", "pl_bust_step", "pl_max_pumps", "pl_points"),
        "simon":    ("sm_symbols", "sm_max_rounds", "sm_reveal"),
        "balloon":  ("bl_cells", "bl_pops", "bl_points"),
        "rps":      ("rps_wins",),
        "slots":    ("sl_symbols",),
        "blackjack": (),
    }
    _CFG_FIELDS = tuple(f for fs in _BY_KIND.values() for f in fs)
    games = list(block.get("minigames") or [])
    taken = {str(g.get("name", "")).strip().lower() for g in games}
    for c in (block.get("commands") or []):
        if not isinstance(c, dict):
            continue
        t = str(c.get("type") or "").lower()
        if not t.startswith("game-"):
            continue
        kind = t[5:]
        base = (c.get("name") or kind or "game").strip() or kind
        nm, n = base, 2
        while nm.lower() in taken:
            nm = f"{base} {n}"
            n += 1
        taken.add(nm.lower())
        gid = "mg:" + uuid.uuid4().hex[:8]
        games.append({
            "id": gid, "name": nm, "kind": kind,
            "config": {k: c[k] for k in _BY_KIND.get(kind, ()) if k in c},
            "tiers": c.get("game_tiers") or [],
            "luck": c.get("game_luck"),
            "intro": c.get("game_intro") or "",
            "intro_embed": bool(c.get("game_intro_embed")),
            "intro_title": c.get("game_intro_title") or "",
            "start_events": list(c.get("start_events") or []),
        })
        # the command keeps its gates and becomes an ordinary action block
        # whose single row plays that profile
        c["type"] = "actions"
        c["actions"] = list(c.get("actions") or []) + [
            {"type": "minigame", "minigame": gid}]
        for k in (*_CFG_FIELDS, "game_tiers", "game_luck", "game_intro",
                  "game_intro_embed", "game_intro_title"):
            c.pop(k, None)
    if games:
        block["minigames"] = games


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
    if v < 12:
        # v12: the overlay designer's "Stage" is renamed SCENE — it matches
        # OBS (a Scene holds Sources; a Group holds some of them) and frees
        # "Stage" to mean only the phone's fullscreen camera page.
        for old_k, new_k in (("stages", "scenes"),
                             ("stage_globals", "scene_globals"),
                             ("chat_stage", "chat_scene")):
            if old_k in cfg and new_k not in cfg:
                cfg[new_k] = cfg.pop(old_k)
            else:
                cfg.pop(old_k, None)
    if v < 13:
        # v13: a minigame stops being a command TYPE and becomes a named
        # PROFILE that a `minigame` ACTION calls. That's what lets a command do
        # things BEFORE and AFTER a game, and lets two profiles of the same
        # kind differ in limits, luck and score tiers.
        # only the fields that KIND actually reads — a command carries defaults
        # for every game type, and copying them all makes a noisy profile
        _games_to_profiles(cfg)
        # A scene we SHIP only ever reaches brand-new installs, because the
        # factory config is a first-run seed. Anyone with an existing config
        # would never see it — so hand over any shipped scene they don't
        # already have, BY NAME. Their own scenes are never touched.
        have = {str(x.get("name") or "").strip().lower()
                for x in (cfg.get("scenes") or [])}
        added = [sc for sc in (_factory_seed().get("scenes") or [])
                 if str(sc.get("name") or "").strip().lower() not in have]
        if added:
            cfg["scenes"] = list(cfg.get("scenes") or []) + copy.deepcopy(added)
        # Go Live stages and the pause overlay name scene GROUPS, and a group
        # belongs to a scene — so one global block meant "Intro" resolved
        # against whichever scene happened to be linked. Each scene carries its
        # own show now. The scene that's currently live inherits what has been
        # running; the others start with the intro OFF rather than springing
        # one you never configured.
        live = str(cfg.get("chat_scene") or "").strip().lower()
        g = cfg.get("golive") or {}
        for sc in (cfg.get("scenes") or []):
            if isinstance(sc.get("golive"), dict):
                continue
            mine = str(sc.get("name") or "").strip().lower() == live
            sc["golive"] = copy.deepcopy(g) if mine else {**copy.deepcopy(g),
                                                          "intro_enabled": False}
            if mine:
                sc.setdefault("notify_overlay", cfg.get("notify_overlay") or "")
                sc.setdefault("pause_overlay", cfg.get("pause_overlay") or "")
    if v < 14:
        # v14: the SCENE absorbs gameplay. There used to be two independent
        # axes — the live scene, and the gameplay settings/preset loaded over
        # it — so a command could name a group that only existed in some other
        # scene. One axis now: a scene IS the show, rules and wording included.
        # Every existing scene inherits what has been running, verbatim, so an
        # upgrade changes nothing until the operator deliberately diverges one.
        mine = {k: copy.deepcopy(cfg[k]) for k in GAMEPLAY_KEYS if k in cfg}
        for sc in (cfg.get("scenes") or []):
            if not isinstance(sc, dict):
                continue
            if isinstance(sc.get("gameplay"), dict) and sc["gameplay"]:
                continue
            sc["gameplay"] = copy.deepcopy(mine)
        # A scene that ALREADY had a block (the ones we ship) never went through
        # the v13 pass, so its game-* commands are still sitting there with no
        # actions at all. Convert every block, not just the one we just copied.
        for sc in (cfg.get("scenes") or []):
            if isinstance(sc, dict) and isinstance(sc.get("gameplay"), dict):
                _games_to_profiles(sc["gameplay"])
        scenes = cfg.get("scenes") or []

        def _by(nm):
            return next((x for x in scenes
                         if str(x.get("name") or "").strip().lower() == nm), None)

        # "Basic Overlays" was both the shipped scene AND the one people edited.
        # Now that a read-only DiscoFlate Default exists, an edited copy under
        # the old name is confusing — give it a name of its own and keep every
        # bit of its content, including the gameplay just folded into it.
        old = _by("basic overlays")
        if old is not None and _by(DEFAULT_SCENE_NAME.lower()) is not None:
            taken = {str(x.get("name") or "").strip().lower() for x in scenes}
            want = str(cfg.get("operator_name") or "").strip() or "My Scene"
            nm, n = want, 2
            while nm.strip().lower() in taken:
                nm = f"{want} {n}"
                n += 1
            if str(cfg.get("chat_scene") or "").strip().lower() == "basic overlays":
                cfg["chat_scene"] = nm
            old["name"] = nm
            old.pop("builtin", None)

        # The shipped default is read-only and ships clean — no sample
        # broadcasts or capacity events to delete on every fresh install.
        dflt = _by(DEFAULT_SCENE_NAME.lower())
        if dflt is not None:
            dflt["builtin"] = True
            gp = dflt.setdefault("gameplay", {})
            gp["broadcasts"] = []
            gp["capacity_events"] = []
            gp["polls"] = []
        # The starter we just handed over is untouched by construction, so mark
        # it as ours — it keeps tracking the shipped scene until they edit it.
        start = _by(STARTER_SCENE_NAME.lower())
        if start is not None:
            start["pristine"] = True
        # never leave the panel pointed at a scene nobody can edit
        if (str(cfg.get("chat_scene") or "").strip().lower()
                == DEFAULT_SCENE_NAME.lower()):
            other = next((x for x in scenes
                          if not x.get("builtin")), None)
            if other is not None:
                cfg["chat_scene"] = other.get("name") or ""
    if v < 15:
        # v14 moved gameplay into the scene before minigame PROFILES were part
        # of that set, so a scene got the game commands but not the games they
        # play — and its empty list then shadowed the top-level one, leaving
        # every game command pointing at nothing. Pull back any profile a
        # scene's own commands actually reference.
        pool = {}
        for src in [cfg] + list(cfg.get("scenes") or []):
            block = src.get("gameplay") if src is not cfg else cfg
            for g in ((block or {}).get("minigames") or []):
                if isinstance(g, dict) and g.get("id"):
                    pool.setdefault(g["id"], g)
        for sc in (cfg.get("scenes") or []):
            gp = sc.get("gameplay")
            if not isinstance(gp, dict):
                continue
            have = {g.get("id") for g in (gp.get("minigames") or []) if isinstance(g, dict)}
            want = {a.get("minigame") for c in (gp.get("commands") or [])
                    if isinstance(c, dict)
                    for a in (c.get("actions") or [])
                    if isinstance(a, dict) and a.get("type") == "minigame"}
            missing = [pool[i] for i in want if i and i not in have and i in pool]
            if missing:
                gp["minigames"] = list(gp.get("minigames") or []) + copy.deepcopy(missing)
    if v < 16:
        # v16: the single "announce channel" becomes a per-channel flag. It was
        # the only way to have a channel that receives events without accepting
        # commands — now any listen target can be marked announce-only, which
        # removes the special case and lets you have more than one.
        # competitions are embeds with buttons — there is no typed entry
        # command any more, so stop carrying its name around. command_names is
        # scene-scoped, so every scene holds its own copy to clean as well.
        for _blk in [cfg] + [sc.get("gameplay") for sc in (cfg.get("scenes") or [])
                             if isinstance(sc, dict)]:
            if isinstance(_blk, dict) and isinstance(_blk.get("command_names"), dict):
                _blk["command_names"].pop("enter", None)
        ann = str(cfg.pop("announce_channel_id", "") or "").strip()
        if ann:
            targets = cfg.get("listen_targets")
            if not isinstance(targets, list):
                targets = []
            hit = next((t for t in targets if isinstance(t, dict)
                        and str(t.get("channel_id") or "").strip() == ann), None)
            if hit is None:
                targets.append({"guild_id": str(cfg.get("listen_guild_id") or ""),
                                "guild_name": "", "channel_id": ann,
                                "channel_name": "announce", "announce_only": True})
            else:
                # it was BOTH a listen target and the announce channel, so it
                # already accepted commands — leave it accepting them
                hit.setdefault("announce_only", False)
            cfg["listen_targets"] = targets
    if v < 17:
        # v17: a channel does two independent jobs — take COMMANDS and receive
        # BROADCASTS — so it gets a tickbox for each instead of one Active.
        # active=False meant silent; announce_only meant broadcasts without
        # commands; anything else did both.
        for t in (cfg.get("listen_targets") or []):
            if not isinstance(t, dict):
                continue
            on = bool(t.get("active", True))
            ann_only = bool(t.pop("announce_only", False))
            t["listen"] = bool(t.get("listen", on and not ann_only))
            t["announce"] = bool(t.get("announce", on))
            t.pop("active", None)
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


def _shipped(name: str) -> dict | None:
    want = name.strip().lower()
    return next((sc for sc in (_factory_seed().get("scenes") or [])
                 if str(sc.get("name") or "").strip().lower() == want), None)


def _named(sc: dict, like: dict) -> dict:
    """`sc` under the other scene's name, for a name-blind comparison."""
    out = dict(sc)
    out["name"] = like.get("name")
    return out


def _scene_body(sc: dict) -> str:
    """A scene's content, ignoring the pristine mark, for change detection."""
    return json.dumps({k: v for k, v in sc.items() if k != "pristine"},
                      sort_keys=True, default=str)


def _sync_starter(cfg: dict, previous: list | None, compare: bool) -> None:
    """Keep an untouched starter matching what we ship; release an edited one.

    Only a SAVE can tell an edit from an untouched copy, because only a save
    has both the incoming scenes and the stored ones. `compare=True` makes that
    judgement. A LOAD passes compare=False: the config it just read IS the
    stored copy, so comparing would always say "unchanged" — the stored
    `pristine` mark is already the record of whether they had touched it, and
    the refresh is what carries a newly shipped version across.
    """
    scenes = cfg.get("scenes")
    if not isinstance(scenes, list):
        return
    want = STARTER_SCENE_NAME.strip().lower()
    for sc in scenes:
        # renamed it → it's theirs now, whatever else they did
        if isinstance(sc, dict) and sc.get("pristine") \
                and str(sc.get("name") or "").strip().lower() != want:
            sc.pop("pristine", None)
    at = next((i for i, sc in enumerate(scenes)
               if isinstance(sc, dict) and sc.get("pristine")
               and str(sc.get("name") or "").strip().lower() == want), None)
    if at is None:
        return
    fresh = _shipped(STARTER_SCENE_NAME)
    if compare:
      # Untouched means matching EITHER what we ship (load() may have just
    # refreshed it) OR what was last written (we shipped a change they have not
    # picked up yet). Only when it matches neither did they edit it.
    # With no stored copy to compare against — the very first write — matching
    # what we ship is the only evidence of "untouched", and we must not refresh
    # on a guess or we would wipe content the caller deliberately put there.
      body = _scene_body(scenes[at])
      was = next((sc for sc in (previous or [])
                  if isinstance(sc, dict)
                  and str(sc.get("name") or "").strip().lower() == want), None)
      same_as_shipped = fresh is not None and _scene_body(_named(fresh, scenes[at])) == body
      same_as_disk = was is not None and _scene_body(was) == body
      if not (same_as_shipped or same_as_disk):
          scenes[at].pop("pristine", None)        # edited → hands off from now on
          return
    if fresh is not None:
        scenes[at] = copy.deepcopy(fresh)
        scenes[at]["pristine"] = True


def _disk_scenes() -> list | None:
    """The scene list exactly as stored, for before/after comparison."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d.get("scenes") if isinstance(d, dict) else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def _enforce_builtin(cfg: dict) -> None:
    """Restore the read-only shipped scene, whatever the caller sent.

    Enforced here rather than in the API handler so EVERY write path — panel,
    device sync, import, preset load — gets the same answer: the DiscoFlate
    Default is the one thing you can always compare against, so nothing may
    edit it, rename it or delete it.
    """
    want = DEFAULT_SCENE_NAME.strip().lower()
    shipped = next((sc for sc in (_factory_seed().get("scenes") or [])
                    if str(sc.get("name") or "").strip().lower() == want), None)
    if shipped is None:
        return
    scenes = cfg.get("scenes")
    if not isinstance(scenes, list):
        return
    at = next((i for i, sc in enumerate(scenes)
               if isinstance(sc, dict)
               and str(sc.get("name") or "").strip().lower() == want), None)
    if at is None:
        scenes.insert(0, copy.deepcopy(shipped))   # deleted → put it back
    else:
        scenes[at] = copy.deepcopy(shipped)        # edited → undo it


def save(cfg: dict) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    _sync_starter(cfg, _disk_scenes(), compare=True)
    _enforce_builtin(cfg)
    cfg["config_rev"] = int(cfg.get("config_rev") or 0) + 1
    _rotate_backups()
    # Only the ON-DISK copy carries the encrypted token; callers keep using
    # the returned dict with the plaintext one (the bot needs the real thing).
    disk = dict(cfg)
    disk["discord_token"] = _enc_token(disk.get("discord_token") or "")
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(disk, fh, indent=2)
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
