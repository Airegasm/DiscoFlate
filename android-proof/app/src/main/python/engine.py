"""
engine.py — DiscoFlate's capacity + firing + dice engine.

Framework-agnostic (no Discord, no HTTP). The web server and the bot both talk
to a single Engine instance.

Device model:
  * Every registered device has a type: "pump" or "other".
  * There is ONE global capacity (0-100%). It climbs only while the active
    PUMP device is running, at a rate set by that pump's calibration
    (secondsTo100) — exactly like PumpDirect.
  * Any device can be fired independently, each with its OWN timer, so several
    devices (a pump + some "other" props) can run at once. Firing an "other"
    device does not move capacity.

Rolling:
  * A roll picks its dice from the capacity range it's currently in (N dice of
    S sides). The total scales the on-time (seconds = total, or × factor),
    clamped to [min_seconds, max_seconds]. max_seconds is a hard per-device cap.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections import deque

import kasa_legacy as kasa
import device_control
import config_store

# Friendly display names for minigame command types (shown in announcements +
# the [game] placeholder), independent of what the command itself is named.
GAME_DISPLAY_NAMES = {
    "game-pushluck": "Push Your Luck",
    "game-simon": "Simon",
    "game-balloon": "Don't Pop It",
    "game-rps": "Rock Paper Scissors",
    "game-slots": "Slots",
    "game-blackjack": "Blackjack",
}
SLOT_SYMBOLS = ["🍒", "🍋", "🔔", "⭐", "💎", "7️⃣", "🎰", "🍀"]


class Engine:
    def __init__(self) -> None:
        self.cfg: dict = {}

        self.capacity: float = 0.0
        self._cap_since: float | None = None  # monotonic marker while pump runs

        # Per-device fire state: device_id -> {deadline, task, abort, extend, alias}
        self._fires: dict[str, dict] = {}

        self._tick_task: asyncio.Task | None = None
        self.events: deque = deque(maxlen=100)

        self._users: dict[str, dict] = {}      # uid -> {name,count,last}
        # Per-(user, command) cooldowns: uid -> cmdkey -> {last, cd}. One user's
        # cooldown never affects another's; each command tracks its own timer.
        self._cooldowns: dict[str, dict] = {}
        self._milestones_fired: set = set()
        self._event_last: dict[str, float] = {}   # event name -> last-run monotonic
        self._events_done: set = set()            # "once" events (and repeat-capped loops) already finished
        self._event_fires: dict[str, int] = {}    # event name -> times fired (for loop max-repeats)
        self._event_cooldown_until: dict[str, float] = {}  # event name -> monotonic when its cooldown ends
        self._event_activator: dict[str, tuple] = {}   # event name -> (uid, who) that started it (for leaderboard credit)
        self._event_run_id: dict[str, int] = {}    # event key -> run counter, so each loop run replaces only its own round messages
        self._listener_was: bool = False          # for detecting off→on transitions
        # Events temporarily switched on by a command. Reverts on session reset
        # / app restart (it's in-memory only), never persisted to config.
        self._runtime_events_on: set = set()
        # Capacity Events: one-shot at a capacity threshold (1-999%). While one
        # runs (its action block), its effects gate commands/events; "session
        # remainder" effects persist after it until session reset. Normal
        # ranges yield to these; End / End Sequence overrules them.
        self._capev_done: set = set()                    # keys fired this session
        self._capev_tasks: dict[str, asyncio.Task] = {}  # running action blocks
        self._capev_fx: dict[str, set] = {}              # key -> effects while it runs
        self._capev_session_fx: set = set()              # effects locked for the session
        self._capev_enable: dict[str, set] = {}          # key -> commands enabled during it
        # Polls: at most ONE runs at a time. While active, the vote system
        # command works (!agvote N); a poll inside an event/capacity-event
        # action block BLOCKS that block, so the event counts as still running
        # for the poll's whole duration and resumes its remaining actions after.
        self._poll: dict | None = None                   # {def, opts, votes:{uid:idx}, voters:{uid:name}}
        self._poll_task: asyncio.Task | None = None      # command-started (background) polls
        # Winning option (0-based) of the most recent poll, or None. Lets a
        # post-event action be gated on which option won (if_option, 1-based).
        self._last_poll_winner: int | None = None
        # Competitions ("roll-offs"): one runs at a time. Players spam a
        # designated command during a window; scores tally by a metric; the
        # winner gets their total fired + optional winner-only range command.
        self._comp: dict | None = None          # {def, cmd, ends_at, metric, cap, scores:{uid:{name,score,entries}}}
        self._comp_task: asyncio.Task | None = None
        self._last_winner: str = ""             # [winner] placeholder (most recent competition winner)
        self._last_winner_score: float = 0.0
        self._last_runnerup: str = ""           # [runnerup] placeholder (2nd place, last competition)
        self._last_runnerup_score: float = 0.0
        self._last_total_score: float = 0.0     # [total_score] (sum of all finishers' scores)
        self._last_results: str = ""            # [results] placeholder (last competition's scoreboard)
        # Winner-only command grants: cmdkey -> uid. Only that user may run the
        # command, and only within the current range (cleared on range change /
        # session reset). The 'progression lock' freezes everyone else's
        # capacity gains until the winner uses their granted command.
        self._winner_grants: dict[str, str] = {}
        # award_prize charges: cmdkey -> remaining uses. A key present here caps a
        # grant to N uses (each use spends one; grant is revoked at 0). Absent =
        # unlimited (competition winner grants and award_prize charges<=0).
        self._grant_charges: dict[str, int] = {}
        self._progression_lock: dict | None = None   # {"uid":.., "cmd":..} or None
        self._inline_depth: int = 0                  # recursion guard for [!command] inline fires
        # Winner idle deadline: if the winner doesn't use their granted command
        # within the limit, it auto-fires so the game can't stall.
        self._bonus_pending: dict | None = None      # {"cmd":.., "uid":.., "who":.., "used":bool}
        self._deadline_task: asyncio.Task | None = None
        # Operator command_gate: when True, ALL custom commands AND the builtin
        # dice roll are frozen for everyone but the owner / bot-internal calls /
        # a granted command (award_prize) / a live Winner Button target. Set by a
        # command_gate action or a Winner Button's freeze; cleared by remove_block,
        # a Winner Button press/deadline, or session reset.
        self._cmd_gate_block: bool = False
        # command_gate refinements: commands whitelisted THROUGH a block_all, and
        # specific commands blocked. (disable_range / disable_always_on /
        # pause_events modifiers reuse the capacity-event session-effect set.)
        self._cmd_gate_allow: set = set()
        self._cmd_gate_blocked_cmds: set = set()
        # Winner Button: a one-press button handed to a competition winner (or any
        # target). Pressing it runs a mini action block; if the target never
        # presses it, a deadline auto-runs the block so the game can't stall.
        self._winner_button: dict | None = None      # {"uid","who","actions","freeze","name","used"}
        self._winner_button_task: asyncio.Task | None = None
        # Bonus bank: award_amount stacks per-player bonus AMOUNTS (pump seconds
        # and/or capacity %) that a Bonus Round later pools & cashes in. Session-
        # scoped (cleared on session reset; a round activation spends them).
        self._bonus_bank: dict[str, dict] = {}       # uid -> {"name","secs","pct"}
        # Bonus Round (teamwork): an embed with a confirm button for bonus holders;
        # once the needed holders confirm (all, or the top holder), its action
        # block runs with the pooled [total_bonus_*] totals.
        self._bonus_round: dict | None = None
        self._bonus_round_task: asyncio.Task | None = None
        # Post-event action blocks: run AFTER an event completes, detached — the
        # event has already cleared (effects lifted, marked done), so these are
        # NOT part of it. Session-scoped: cancelled on pause / reset / off.
        self._post_tasks: set = set()
        # Max Roll Prize tracking (in-memory; resets on session reset / restart).
        self._perfect: dict[str, int] = {}       # uid -> perfect-roll count
        self._prize_uses: dict[str, int] = {}    # uid -> remaining uses (present = unlocked)
        self._pump_time: dict[str, dict] = {}    # uid -> {name, seconds, capacity} (session leaderboard)
        # Per-range leaderboards: (min,max) -> {uid: {name, seconds, capacity}}.
        # Same shape as _pump_time but scoped to the range the pump landed in, for
        # [leaderboard_range]/[range_leader]. In-memory; cleared on session reset.
        self._pump_range: dict[tuple, dict[str, dict]] = {}
        # Lifetime (all-time) leaderboard — persisted to data/, survives session
        # resets AND app restarts. Separate from the per-session _pump_time above.
        self._pump_life: dict[str, dict] = self._load_lifetime()
        self._cmd_uses: dict[str, dict] = {}     # uid -> {cmdname: times used this session}
        self._last_fired: dict[str, float] = {}  # cmdkey -> last-fired monotonic (anti-spam buffer)
        self._current_range_key = None           # (min,max) of the range we're in, for entry detection
        self._end_triggered = False               # End Sequence final threshold already fired
        # Session pause: a global latch that blocks EVERY device-on path until the
        # operator resumes. Mirrors cfg["session_paused"] so it survives restarts.
        self._paused: bool = False
        self._paused_by: str = ""
        # Failsafe OFF bookkeeping: the last state we COMMANDED per device (True
        # only until a successful OFF), windows where ON outside _fires is fine
        # (calibration/test), and per-device watchdog retry pacing.
        self._last_commanded: dict[str, bool] = {}
        self._on_sanctioned: dict[str, float] = {}   # device_id -> monotonic ok-until
        self._watchdog_next: dict[str, float] = {}
        # Per-device command serialization: a dying fire's OFF and the next
        # fire's ON (or a test/calibration call) can't interleave on one plug.
        self._dev_locks: dict[str, asyncio.Lock] = {}
        # Session uptime: None until Activation turns on (uptime reads 0 — the
        # app being open is not a session). Stamped on activation / session
        # reset; frozen at its final value on deactivation so the OFF message's
        # [uptime] reports the full run. In-memory only; resets on app restart.
        self._session_start: float | None = None
        self._session_frozen: float = 0.0

        self.announce_cb = None                # async (text, image) -> None
        self.end_session_cb = None             # async () -> None : deactivate without the off-message
        self.cancel_games_cb = None            # async () -> None : cancel all live minigame views
        self.embed_cb = None                   # async (title, text) -> None : rich embed post (polls)
        self.broadcast_embed_cb = None         # async (text) -> None : broadcast-preset embed
        self.comp_embed_cb = None              # async (title, text, meta) -> None : competition embed + Enter button
        self.winner_button_cb = None           # async (title, text, meta) -> None : one-press Winner Button embed
        self.bonus_round_cb = None             # async (title, text, meta) -> None : Bonus Round embed + confirm button
        self.bot_connected: bool = False

        # Device add/search/use debug flows into the Activity log too (not just stdout).
        device_control.set_log_sink(self._device_log)

    # -- config / device lookup --------------------------------------------- #
    def set_config(self, cfg: dict) -> None:
        self.cfg = cfg
        # The pause latch persists (a crash while paused comes back paused). Only
        # pause()/resume() write these keys — set_config just mirrors them.
        self._paused = bool(cfg.get("session_paused"))
        self._paused_by = str(cfg.get("session_paused_by") or "")
        # Display names are spoofable (any member can set a matching nickname);
        # warn once if owner/exemption power rests on names alone.
        if ((cfg.get("cooldown_exempt_names") or cfg.get("operator_name"))
                and not cfg.get("cooldown_exempt_user_ids")
                and not getattr(self, "_warned_name_owner", False)):
            self._warned_name_owner = True
            self._log("bot", "⚠ owner/exemptions match by display NAME only — any member can "
                             "rename themselves to match. Set the Owner user ID (Game tab) too.")
        now_on = bool(cfg.get("listener_enabled"))
        if now_on and not self._listener_was:
            # Activation: re-arm all event timers so loops/once start fresh, and
            # re-trigger range entry for the current range. Arming also (re)starts
            # the session uptime clock; muting leaves it intact so the OFF message
            # can still report the full run.
            self._session_start = time.monotonic()
            self._session_frozen = 0.0
            self._capev_cancel_all(clear_session=True)
            self._cancel_poll_task()
            self._event_last.clear()
            self._events_done.clear()
            self._event_fires.clear()
            self._event_cooldown_until.clear()
            self._event_activator.clear()
            self._current_range_key = None
            self._end_triggered = False
        elif self._listener_was and not now_on:
            # Deactivation: freeze the session clock at its final value (the OFF
            # message renders [uptime] right after this), then clear cooldowns
            # and cancel timed events.
            if self._session_start is not None:
                self._session_frozen = max(0.0, time.monotonic() - self._session_start)
                self._session_start = None
            self._capev_cancel_all(clear_session=True)
            self._cancel_poll_task()
            self._cancel_competition()
            self._clear_winner_grants(all_incl_stash=True)
            self._cooldowns.clear()
            self._event_last.clear()
            self._events_done.clear()
            self._event_fires.clear()
            self._event_cooldown_until.clear()
            self._event_activator.clear()
            self._runtime_events_on.clear()
            self._log("bot", "deactivated — cooldowns cleared, events cancelled")
        self._listener_was = now_on

    def _device(self, device_id: str | None) -> dict | None:
        # Mock mode with no real device → a virtual pump so the whole game runs
        # (rolls, capacity, commands) as a dry run without any hardware.
        if device_id == "mock:virtual":
            return {"id": "mock:virtual", "label": "Mock pump", "vendor": "mock",
                    "host": "mock", "type": "pump",
                    "calibration_seconds_to_100": self.cfg.get("mock_calibration_seconds_to_100") or 60}
        for d in self.cfg.get("devices", []):
            if d.get("id") == device_id:
                return d
        return None

    def _active_id(self) -> str | None:
        aid = self.cfg.get("active_device_id")
        if self.cfg.get("mock_mode"):
            has_real = aid is not None and any(d.get("id") == aid for d in self.cfg.get("devices", []))
            if not has_real:
                return "mock:virtual"
        return aid

    def _active_device_dict(self) -> dict | None:
        return self._device(self._active_id())

    def _pump_id(self) -> str | None:
        """The device that drives capacity: the active device, if it's a pump."""
        d = self._active_device_dict()
        if d and (d.get("type") or "pump") == "pump":
            return d.get("id")
        return None

    def _outlet(self, dev: dict) -> kasa.Outlet:
        return kasa.Outlet(host=dev["host"], alias=dev.get("label") or dev["host"],
                           model="", child_id=dev.get("child_id") or None)

    def _vendor_creds(self, dev: dict) -> dict:
        """The saved credentials for a device's vendor (config['vendors'][vendor])."""
        vendor = (dev.get("vendor") or "kasa").strip().lower()
        return (self.cfg.get("vendors") or {}).get(vendor, {}) or {}

    async def _set_state(self, dev: dict | None, on: bool) -> None:
        """Mock-aware, vendor-agnostic device on/off. Routes by dev['vendor']
        (kasa is local; other brands via device_control) — so mock mode and the
        whole capacity engine behave identically regardless of vendor. In mock
        mode the real device is NOT touched."""
        if dev is None:
            return
        if self.cfg.get("mock_mode"):
            device_control._dbg(f"USE  set_state vendor={(dev.get('vendor') or 'kasa')} "
                                f"target={device_control._ident(dev)} on={on} (MOCK — not fired)")
            return
        did = dev.get("id")
        if on and did:
            # Record intent BEFORE the network call: if it half-succeeds (timeout
            # after the relay switched), the watchdog still knows to force it off.
            self._last_commanded[did] = True
        await device_control.set_state(dev, on, self._vendor_creds(dev))
        if not on and did:
            self._last_commanded[did] = False   # cleared only on a CONFIRMED off

    @property
    def paused(self) -> bool:
        return self._paused

    def _dev_lock(self, device_id) -> asyncio.Lock:
        return self._dev_locks.setdefault(str(device_id), asyncio.Lock())

    async def _force_off(self, dev: dict | None, attempts: int = 3, delay: float = 1.0) -> bool:
        """OFF with retries — the one command that must not silently fail. Returns
        True once the device confirms off; False if every attempt errored.
        Serialized per device so it can't interleave with a new fire's ON."""
        if dev is None:
            return False
        alias = dev.get("label") or dev.get("host") or dev.get("id") or "device"
        async with self._dev_lock(dev.get("id") or alias):
            for i in range(max(1, attempts)):
                try:
                    await self._set_state(dev, False)
                    return True
                except Exception as e:  # noqa: BLE001
                    self._log("error", f"turn-off failed on {alias} (try {i + 1}/{attempts}): {e}")
                    if i + 1 < attempts:
                        await asyncio.sleep(delay)
        return False

    async def _watchdog_sweep(self) -> None:
        """Failsafe: any device we commanded ON that no longer has a tracked fire
        (and isn't in a sanctioned calibration/test window) gets forced OFF."""
        now = time.monotonic()
        for did, on in list(self._last_commanded.items()):
            if not on or did in self._fires:
                continue
            if now <= self._on_sanctioned.get(did, 0.0):
                continue
            if now < self._watchdog_next.get(did, 0.0):
                continue
            self._watchdog_next[did] = now + 10.0   # pace retries per device
            dev = self._device(did)
            if dev is None:
                self._last_commanded.pop(did, None)   # device was removed from config
                continue
            self._log("device", f"watchdog: {dev.get('label') or did} believed ON with no fire — forcing OFF")
            await self._force_off(dev)

    # -- lifecycle ----------------------------------------------------------- #
    def start(self) -> None:
        if not self._tick_task:
            self._tick_task = asyncio.create_task(self._capacity_loop())

    async def stop(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
            self._tick_task = None
        await self.abort(reason="shutdown")
        # Wait for the fire tasks' own OFF handling to finish (bounded), then a
        # final forced-OFF sweep so shutdown can never strand a relay ON.
        tasks = [f["task"] for f in self._fires.values() if f.get("task")]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=8)
            except asyncio.TimeoutError:
                self._log("error", "shutdown: fire tasks didn't finish in time")
        for did, on in list(self._last_commanded.items()):
            if on:
                await self._force_off(self._device(did))

    async def _capacity_loop(self) -> None:
        watchdog_at = 0.0
        while True:
            try:
                await asyncio.sleep(0.2)
                pump_id = self._pump_id()
                running = pump_id is not None and pump_id in self._fires
                now = time.monotonic()
                if running:
                    sec = self._device(pump_id).get("calibration_seconds_to_100")
                    if sec and self._cap_since is not None:
                        self.capacity = min(self._capacity_cap(), self.capacity + (now - self._cap_since) / sec * 100.0)
                    self._cap_since = now
                else:
                    self._cap_since = None
                if now >= watchdog_at:
                    watchdog_at = now + 5.0
                    await self._watchdog_sweep()
                if self._paused:
                    continue   # paused: no milestones/events/end-sequence/reset notices
                await self._check_milestones()
                await self._check_range_entry()
                await self._check_end_sequence()
                await self._check_cooldown_resets()
                await self._check_events()
                await self._check_capacity_events()
            except asyncio.CancelledError:
                break
            except Exception as e:  # never let the loop die
                self._log("error", f"capacity loop: {e}")

    # -- message rendering --------------------------------------------------- #
    _IF_LINE = re.compile(r"^[ \t]*\[if(\s+not)?\s+([\w-]+)\][ \t]?", re.IGNORECASE)

    def _apply_line_conditionals(self, text: str, values: dict) -> str:
        """Per-LINE conditionals: a real line (\\n-separated, not soft-wrap) that
        starts with `[if token]` shows only when that placeholder resolves to a
        non-empty value; `[if not token]` shows only when it's empty/missing. The
        `[if …]` prefix is stripped from kept lines; dropped lines vanish."""
        if "[if" not in text.lower():
            return text
        kept = []
        for line in text.split("\n"):
            m = self._IF_LINE.match(line)
            if not m:
                kept.append(line)
                continue
            negate = bool(m.group(1))
            present = str(values.get(m.group(2).lower(), "")).strip() != ""
            if (not present) if negate else present:
                kept.append(line[m.end():])   # strip the [if …] prefix, keep the line
            # else: drop the whole line
        return "\n".join(kept)

    def _render(self, template: str, ctx: dict) -> str:
        # Single-pass substitution: a placeholder inside a substituted VALUE is
        # never expanded again (a user nicknamed "[toppump]" stays literal
        # instead of splicing the leaderboard into their name).
        out = template or ""
        if out:
            values = {str(k): ("" if v is None else str(v)) for k, v in ctx.items()}
            # line conditionals first (they test the raw placeholder values), then
            # normalise literal "\n" so multi-line [if] templates split correctly.
            out = self._apply_line_conditionals(out.replace("\\n", "\n"), values)

            def _sub(m):
                key = m.group(1) if m.group(1) is not None else m.group(2)
                return values.get(key, m.group(0))

            out = re.sub(r"\[([^\[\]{}]+)\]|\{([^\[\]{}]+)\}", _sub, out)
        return out.replace("\\n", "\n")

    def render(self, template: str, extra: dict | None = None) -> str:
        """[capacity], [timer]/[total_seconds] (active pump's remaining), and the
        command-name tokens are always available; `extra` overrides/adds more."""
        rem = f"{self._remaining(self._active_id()):.1f}"
        prefix = self.cfg.get("command_prefix", "!")
        bn = self.builtin_names()
        ctx = {
            "capacity": round(self.capacity, 1),
            "total_seconds": rem, "total_secs": rem, "timer": rem,
            "uptime": self._fmt_duration(self.session_uptime()),
            "uptime_seconds": int(self.session_uptime()),
            "capacity_bar": self._capacity_bar(),
            "prefix": prefix,
            "roll_cmd": f"{prefix}{bn['roll']}",
            "capacity_cmd": f"{prefix}{bn['capacity']}",
            "help_cmd": f"{prefix}{bn['help']}",
            "leaderboard_cmd": f"{prefix}{bn['leaderboard']}",
            "pumptimer_cmd": f"{prefix}{bn['pumptimer']}",
        }
        ctx["commands"] = self._commands_str(prefix)
        ctx["custom_commands"] = self.custom_commands_str(prefix)
        # [operator] = the bot operator's name (falls back to the first owner name).
        op = (self.cfg.get("operator_name") or "").strip()
        if not op:
            names = self.cfg.get("cooldown_exempt_names") or []
            op = str(names[0]).strip() if names else ""
        ctx["operator"] = op
        ctx["winner"] = self._last_winner           # most recent competition winner
        ctx["winner_score"] = f"{self._last_winner_score:g}" if self._last_winner else ""
        ctx["runnerup"] = self._last_runnerup       # 2nd place, most recent competition
        ctx["runnerup_score"] = f"{self._last_runnerup_score:g}" if self._last_runnerup else ""
        # summed score of every finisher (live during a competition, else last)
        ctx["total_score"] = f"{(self._comp_total_score() if self._comp is not None else self._last_total_score):g}"
        # live scoreboard during a competition, else the last competition's final results
        ctx["results"] = self._comp_results() if self._comp is not None else self._last_results
        # per-range top-pumper board for the current capacity band
        rl_uid, rl_name, rl_score = self.range_leader()
        ctx["leaderboard_range"] = self.range_board_text()
        ctx["range_leader"] = rl_name
        ctx["range_leader_score"] = f"{rl_score:.1f}" if rl_uid else ""
        # overall session top pumper (for an end-of-session special prize)
        sl_uid, sl_name, sl_score = self.session_leader()
        ctx["session_leader"] = sl_name
        ctx["session_leader_score"] = f"{sl_score:.1f}" if sl_uid else ""
        # who currently holds which bonus command(s) + charges (best in an embed)
        ctx["bonus_holders"] = self.bonus_holders_text()
        # pooled bonus AMOUNTS across everyone; [user_bonus_*] default blank
        # (filled in per-user contexts — action blocks, command replies)
        tb_secs, tb_pct = self.total_bonus()
        ctx["total_bonus_secs"] = f"{tb_secs:g}"
        ctx["total_bonus_pct"] = f"{tb_pct:g}"
        ctx.setdefault("user_bonus_secs", "")
        ctx.setdefault("user_bonus_pct", "")
        pz = self.active_prize() or {}
        ctx["max_roll_goal"] = pz.get("goal") if pz.get("goal") not in (None, "") else ""
        ctx["max_roll_command"] = f"{prefix}{(pz.get('command') or '').strip()}" if pz.get("command") else ""
        ctx["max_roll_desc"] = pz.get("description") or ""
        if extra:
            ctx.update(extra)
        # Embeddable blocks: [toppump] / [toppump-all] = the leaderboard command
        # output; [on_message] / [off_message] = the configured listener on/off text.
        # Each is rendered against the ctx built so far, so their own token stays
        # literal inside themselves (no recursion), same rule as [announce].
        ctx["toppump"] = self.leaderboard_text()
        ctx["toppump-all"] = self.leaderboard_life_text()
        ctx["on_message"] = self._render((self.cfg.get("listener_message_on") or ""), ctx)
        ctx["off_message"] = self._render((self.cfg.get("listener_message_off") or ""), ctx)
        # [pump_msg] = the operator Pump message, reusable in other messages
        # ([secs]/[timer]/[secs2capacity] are blank here unless passed in extra).
        ctx["pump_msg"] = self._render((self.cfg.get("pump_message") or ""), ctx)
        # [announce] = the current range's announce text (its own placeholders
        # resolved; [announce] inside it stays literal to avoid recursion).
        ann = (self.range_for(self.capacity).get("announce") or "").strip()
        ctx["announce"] = self._render(ann, ctx)
        return self._render(template, ctx)

    def session_uptime(self) -> float:
        """Seconds the current session has been running. 0 before the first
        activation; frozen at the final value while deactivated."""
        if self._session_start is None:
            return self._session_frozen
        return max(0.0, time.monotonic() - self._session_start)

    def _evt_hdr(self, name: str) -> str:
        """Output-header prefix for an event message (no user — events broadcast the
        same text everywhere). Empty unless output_headers is on."""
        return f"**[{name}]** " if (self.cfg.get("output_headers") and name) else ""

    @staticmethod
    def _fmt_duration(secs) -> str:
        """Compact human duration: '45s', '5m 30s', '2h 5m 30s', '3d 4h 12m'."""
        s = int(max(0, secs))
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        if d:
            return f"{d}d {h}h {m}m"
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def _anon_label(self) -> str:
        return self.cfg.get("anon_user_label") or "Someone on another server"

    def _mention(self, uid, who: str) -> str:
        """A Discord ping for the user (<@id>), or their plain name if no id."""
        return f"<@{uid}>" if uid else who

    def _secs_to_capacity(self, secs, device_id) -> str:
        """Capacity % that firing the (pump) device for `secs` seconds would add,
        capped at the remaining headroom. 0 for non-pump / uncalibrated devices."""
        dev = self._device(device_id)
        if not dev or (dev.get("type") or "pump") != "pump":
            return "0"
        cal = dev.get("calibration_seconds_to_100")
        try:
            if not cal or float(cal) <= 0:
                return "0"
            raw = float(secs) / float(cal) * 100.0
        except (TypeError, ValueError):
            return "0"
        # The projected add is purely the calibration rate (secs → % of a full
        # 0→100 fill). NOT clamped to remaining headroom: past 100% (end
        # sequences) headroom would be ≤0 and wrongly show 0.0%.
        return f"{max(0.0, raw):.1f}"

    def _capacity_bar(self, width: int = 17) -> str:
        """Emoji thermometer for the current capacity: green→yellow→orange→red."""
        filled = round(self.capacity / 100 * width)
        out = []
        for i in range(width):
            if i < filled:
                pos = (i + 0.5) / width * 100
                out.append("🟩" if pos < 33 else "🟨" if pos < 66 else "🟧" if pos < 90 else "🟥")
            else:
                out.append("⬛")
        return "".join(out)

    # -- ranges / rolling ---------------------------------------------------- #
    def range_for(self, capacity: float) -> dict:
        ranges = sorted(self.cfg.get("capacity_ranges", []),
                        key=lambda r: r.get("min", 0), reverse=True)  # high→low by min
        for r in ranges:
            if r.get("min", 0) <= capacity <= r.get("max", 100):
                return r
        # No exact match → we're in a gap (e.g. between the last normal range's
        # max and an End Sequence's Start). Extend the highest range we've passed
        # so the previous range keeps applying up to the sequence start.
        for r in ranges:
            if r.get("min", 0) <= capacity:
                return r
        return {"min": 0, "max": 100, "dice": 1, "sides": 6, "announce": "", "milestone": "", "image": ""}

    async def _check_milestones(self) -> None:
        if not self.cfg.get("listener_enabled"):
            return   # muted: a web test-fire shouldn't post milestones to Discord
        for r in sorted(self.cfg.get("capacity_ranges", []), key=lambda r: r.get("min", 0)):
            mn = r.get("min", 0)
            if mn <= 0:
                continue
            if self.capacity >= mn and mn not in self._milestones_fired:
                self._milestones_fired.add(mn)
                text = (r.get("milestone") or "").strip()
                image = (r.get("image") or "").strip()
                if text or image:
                    self._log("bot", f"milestone {mn}% → announcing")
                    await self._announce(self.render(text) or f"Reached {mn}% capacity", image or None)

    async def _check_range_entry(self) -> None:
        """When capacity moves into a new range, fire that range's start command
        (once) and apply its per-range event enable/disable actions."""
        if not self.cfg.get("listener_enabled"):
            return
        r = self.range_for(self.capacity)
        key = (r.get("min"), r.get("max"))
        if key == self._current_range_key:
            return
        # leaving a range clears any range-scoped winner-only grants (stashable
        # ones persist), so a winner's command doesn't linger into the next band
        if self._current_range_key is not None:
            self._clear_winner_grants(all_incl_stash=False)
        self._current_range_key = key

        # Enable/disable events on entry.
        for a in (r.get("event_actions") or []):
            name = (a.get("event") or "").strip()
            if not name:
                continue
            if (a.get("action") or "enable").lower() == "enable":
                self._runtime_events_on.add(name.lower())
                self._event_last.pop(name.lower(), None)
                self._events_done.discard(name.lower())
                self._log("bot", f"range {key[0]}–{key[1]}%: event '{name}' ON")
            else:
                self._runtime_events_on.discard(name.lower())
                self._log("bot", f"range {key[0]}–{key[1]}%: event '{name}' OFF")

        # Fire the range start command once (a custom command by name).
        if r.get("start_command_enabled"):
            cname = (r.get("start_command") or "").strip()
            cmd = self.find_command(cname) if cname else None
            if cmd:
                self._log("bot", f"range {key[0]}–{key[1]}%: start command '{cname}'")
                try:
                    res = await self.run_custom(cmd, self._anon_label(), uid=None)
                    if res.get("ok") and res.get("game"):
                        # A game launched here has no Play button (buttons only
                        # exist on the Discord path) — invite people to type it.
                        prefix = self.cfg.get("command_prefix", "!")
                        intro = (res.get("reply") or "").strip()
                        await self._announce(
                            (intro + "\n" if intro else "") +
                            f"🎮 Type **{prefix}{cname}** to play!", None)
                    elif res.get("ok") and res.get("reply"):
                        await self._announce(res["reply"], None)
                except Exception as e:  # noqa: BLE001
                    self._log("error", f"range start command failed: {e}")

    def _end_sequence(self) -> dict | None:
        for r in self.cfg.get("capacity_ranges", []):
            if r.get("is_end"):
                return r
        return None

    def _capacity_cap(self) -> float:
        """Max capacity. Normally 100, but an enabled End Sequence lets it tick
        up to the sequence's final threshold (its `max`)."""
        es = self._end_sequence()
        if es and es.get("enabled"):
            try:
                return max(100.0, float(es.get("max") or 100))
            except (TypeError, ValueError):
                return 100.0
        return 100.0

    async def _check_end_sequence(self) -> None:
        """When capacity reaches the End Sequence's final threshold, post its
        final message, reset the session, and end (deactivate) — quietly."""
        if not self.cfg.get("listener_enabled"):
            return
        es = self._end_sequence()
        if not (es and es.get("enabled")) or self._end_triggered:
            return
        try:
            final_at = float(es.get("max") or 100)
        except (TypeError, ValueError):
            return
        if self.capacity < final_at:
            return
        self._end_triggered = True
        msg = (es.get("final_message") or "").strip()
        # The final message has its OWN image (final_image), separate from the
        # milestone image (image) that fires when the sequence is first entered.
        fimg = (es.get("final_image") or "").strip()
        if msg or fimg:
            await self._announce(self.render(msg), fimg or None)
        # Stop every running fire (forces the pump OFF) before ending the session.
        await self.abort(reason="end sequence")
        self._log("bot", "END SEQUENCE — final message sent, session ending")
        self.session_reset()
        if self.end_session_cb:
            try:
                await self.end_session_cb()   # deactivate without the off-message
            except Exception as e:  # noqa: BLE001
                self._log("error", f"end session failed: {e}")

    async def _run_inline_commands(self, text: str) -> str:
        """[!command] tokens in an announced message FIRE that custom command
        (as the system, uid=None) and are stripped from the text — so a
        milestone/event/broadcast can trigger a real fire. One level deep only
        (a command fired this way can't inline-fire more, preventing loops)."""
        if not text or "[!" not in text or self._inline_depth:
            return text
        names = re.findall(r"\[!([\w-]+)\]", text)
        for name in names:
            cmd = self.find_command(name)
            if cmd is None:
                continue
            self._inline_depth += 1
            try:
                await self.run_custom(cmd, self._anon_label(), uid=None)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"inline [!{name}] failed: {e}")
            finally:
                self._inline_depth -= 1
        return re.sub(r"\s*\[!([\w-]+)\]\s*", " ", text).strip()

    async def render_inline(self, template: str, extra: dict | None = None) -> str:
        """Render a message AND fire any [!command] tokens in it (as the system),
        returning the text with those tokens stripped. For message paths that
        DON'T go through _announce — e.g. the activation ON message, which is
        posted straight to the channel by the web layer."""
        return await self._run_inline_commands(self.render(template, extra))

    async def _announce(self, text: str, image: str | None, replace_key: str | None = None) -> None:
        text = await self._run_inline_commands(text)
        if not text and image is None:
            return   # message was only [!command] token(s) — fired, nothing to post
        if self.announce_cb:
            try:
                await self.announce_cb(text, image, replace_key)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"announce failed: {e}")

    def _roll_total(self, dice: int, sides: int) -> tuple[int, list[int]]:
        dice = max(1, int(dice))
        sides = max(1, int(sides))
        rolls = [random.randint(1, sides) for _ in range(dice)]
        return sum(rolls), rolls

    def _duration_from_total(self, total: int) -> float:
        roll = self.cfg.get("roll", {})
        secs = total * float(roll.get("factor", 1.0)) if roll.get("mode") == "factor" else float(total)
        lo = float(roll.get("min_seconds", 1))
        hi = float(roll.get("max_seconds", 20))
        return round(max(lo, min(hi, secs)), 1)

    def _range_entry(self, r: dict, cmdkey: str) -> dict:
        """The per-command settings block inside a range (dice/sides/cooldown/enabled)."""
        return (r.get("cooldowns") or {}).get(cmdkey, {}) or {}

    def range_dice(self, r: dict) -> tuple[int, int]:
        """Dice/sides for a range's roll — from the roll sub-row, falling back to
        the range's own dice/sides (older configs), then 1d6."""
        e = self._range_entry(r, "roll")
        dice = e.get("dice") if e.get("dice") not in (None, "", 0) else r.get("dice")
        sides = e.get("sides") if e.get("sides") not in (None, "", 0) else r.get("sides")
        return int(dice or 1), int(sides or 6)

    def _always_on_map(self) -> dict:
        """{name_lower: entry} for always-on commands (when enabled). Each entry is
        either a bare name string or {name, cooldown, max_uses}."""
        if not self.cfg.get("always_on_enabled"):
            return {}
        out = {}
        for item in (self.cfg.get("always_on_commands") or []):
            if isinstance(item, dict):
                nm = str(item.get("name") or "").strip().lower()
                if nm:
                    out[nm] = item
            elif item:
                out[str(item).strip().lower()] = {}
        return out

    def _is_always_on(self, cmdkey: str) -> bool:
        """True if this custom command is in the global Always-On set — active in
        every range, bypassing per-range membership (when always_on_enabled)."""
        return cmdkey in self._always_on_map()

    def cmd_enabled_in_range(self, cmdkey: str) -> bool:
        """Roll: active in every range unless toggled off. Custom command: active
        in a range if it's a member (has an entry in that range's cooldowns) OR
        it's in the global Always-On set — presence = active, absence = inactive."""
        cds = (self.range_for(self.capacity).get("cooldowns") or {})
        if cmdkey == "roll":
            return (cds.get("roll") or {}).get("enabled", True) is not False
        if self._is_always_on(cmdkey):
            return True
        e = cds.get(cmdkey)
        return e is not None and e.get("enabled", True) is not False

    def cmd_max_uses(self, cmdkey: str) -> int | None:
        """Per-person session use budget for a command. An always-on command can
        set its own; otherwise it's defined once (the first range that sets it,
        low→high) and carries over ranges (never replenishes)."""
        ao = self._always_on_map().get(cmdkey)
        if ao and ao.get("max_uses") not in (None, "", 0):
            try:
                return int(ao["max_uses"])
            except (TypeError, ValueError):
                pass
        for r in sorted(self.cfg.get("capacity_ranges", []), key=lambda r: r.get("min", 0)):
            mu = self._range_entry(r, cmdkey).get("max_uses")
            if mu not in (None, "", 0):
                try:
                    return int(mu)
                except (TypeError, ValueError):
                    return None
        return None

    def cmd_uses_left(self, uid, cmdkey: str) -> int | None:
        """Remaining uses for a person, or None if the command is unlimited."""
        mx = self.cmd_max_uses(cmdkey)
        if mx is None:
            return None
        used = self._cmd_uses.get(str(uid), {}).get(cmdkey, 0)
        return max(0, mx - used)

    def preview_roll(self, dice: int | None = None, sides: int | None = None) -> dict:
        r = self.range_for(self.capacity)
        rd, rs = self.range_dice(r)
        # Optional override (operator Controls set an explicit NdN); else range dice.
        dice = int(dice) if dice else rd
        sides = int(sides) if sides else rs
        total, rolls = self._roll_total(dice, sides)
        # Luck modifier (this range's roll sub-row): positive % = chance to force
        # a perfect (max) roll; negative % = chance to force the minimum (all 1s).
        try:
            luck = float(self._range_entry(r, "roll").get("luck") or 0)
        except (TypeError, ValueError):
            luck = 0.0
        if luck > 0 and random.random() * 100 < luck:
            rolls = [sides] * dice
            total = sides * dice
        elif luck < 0 and random.random() * 100 < min(100.0, -luck):
            rolls = [1] * dice
            total = dice
        return {"range": r, "dice": dice, "sides": sides, "rolls": rolls,
                "total": total, "announce": (r.get("announce") or "").strip(),
                "duration": self._duration_from_total(total), "capacity": round(self.capacity, 1)}

    # -- cooldown / users ---------------------------------------------------- #
    def _cooldown(self) -> float:
        try:
            return max(0.0, float(self.cfg.get("cooldown_seconds", 0)))
        except (TypeError, ValueError):
            return 0.0

    def _exempt_ids(self) -> set:
        return {str(x).strip() for x in self.cfg.get("cooldown_exempt_user_ids", []) if str(x).strip()}

    def _exempt_names(self) -> set:
        names = list(self.cfg.get("cooldown_exempt_names") or [])
        op = (self.cfg.get("operator_name") or "").strip()
        if op:
            names.append(op)  # the operator is always exempt + treated as owner
        return {str(x).strip().lower() for x in names if str(x).strip()}

    def _is_exempt(self, uid, name: str = "") -> bool:
        return str(uid) in self._exempt_ids() or (name or "").strip().lower() in self._exempt_names()

    def is_owner(self, uid, name: str = "") -> bool:
        """Owner = anyone in the exempt names/IDs list. Owner-only commands
        can be used only by these people."""
        return self._is_exempt(uid, name)

    def _range_cd_scope(self, r: dict, cmdkey: str) -> tuple[float, str]:
        """Cooldown seconds + scope for a command IN A GIVEN RANGE.
        Range drives everything: each range can set a different cooldown/scope
        per command ("roll" for the dice, or the custom command's name). Falls
        back to the global cooldown_seconds (per-user) if the range says nothing.
        Scope is "user" (each person their own timer) or "command" (shared)."""
        e = (r.get("cooldowns") or {}).get(cmdkey)
        if e:
            secs = e.get("seconds")
            if secs is not None and str(secs).strip() != "":
                try:
                    cd = max(0.0, float(secs))
                except (TypeError, ValueError):
                    cd = self._cooldown()
            else:
                cd = self._cooldown()
            return cd, (e.get("scope") or "user").lower()
        # No range entry — an always-on command may set its own cooldown.
        ao = self._always_on_map().get(cmdkey)
        if ao and ao.get("cooldown") not in (None, ""):
            try:
                return max(0.0, float(ao["cooldown"])), "user"
            except (TypeError, ValueError):
                pass
        return self._cooldown(), "user"

    def cooldown_remaining(self, key_uid: str, cmdkey: str, cd: float | None = None) -> float:
        """Remaining cooldown for a (key, command). key_uid is the real user id
        for per-user scope, or "*" for a global (shared) command cooldown."""
        rec = self._cooldowns.get(str(key_uid), {}).get(cmdkey)
        if not rec:
            return 0.0
        use_cd = rec["cd"] if cd is None else cd
        if not use_cd:
            return 0.0
        return max(0.0, use_cd - (time.monotonic() - rec["last"]))

    def _track_user(self, uid: str, name: str) -> None:
        rec = self._users.setdefault(str(uid), {"name": name, "count": 0, "last": 0.0})
        rec["name"] = name
        rec["count"] += 1
        rec["last"] = time.monotonic()

    def _touch_cooldown(self, key_uid: str, cmdkey: str, cd: float) -> None:
        self._cooldowns.setdefault(str(key_uid), {})[cmdkey] = {
            "last": time.monotonic(), "cd": cd, "notified": cd <= 0}

    def _cmd_display(self, cmdkey: str) -> str:
        """A command's user-facing name with prefix, e.g. '!agroll'."""
        prefix = self.cfg.get("command_prefix", "!")
        if cmdkey == "roll":
            cmdkey = self.builtin_names()["roll"]
        return f"{prefix}{cmdkey}"

    async def _check_cooldown_resets(self) -> None:
        """When a cooldown expires, post the one generic reset message (if set)."""
        tmpl = (self.cfg.get("cooldown_reset_message") or "").strip()
        if not tmpl:
            return
        now = time.monotonic()
        # iterate over copies — the awaits below yield, and a command landing
        # mid-broadcast mutates these dicts (RuntimeError otherwise)
        for key_uid, cmds in list(self._cooldowns.items()):
            for cmdkey, rec in list(cmds.items()):
                if rec.get("notified"):
                    continue
                if rec["cd"] and now >= rec["last"] + rec["cd"]:
                    rec["notified"] = True
                    uid = None if key_uid == "*" else key_uid
                    name = "" if key_uid == "*" else self._users.get(key_uid, {}).get("name", "")
                    await self._announce(self.render(tmpl, {
                        "user": name, "mention": self._mention(uid, name), "cmd": self._cmd_display(cmdkey),
                    }), None)

    def reset_users(self) -> None:
        self._users.clear()
        self._cooldowns.clear()
        self._log("bot", "user tracking reset")

    def reset_current_leaderboard(self) -> None:
        """Wipe the CURRENT-session Top Pumpers (session + per-range pump boards)
        and the tracked-users list. Lifetime stats are untouched."""
        self._pump_time.clear()
        self._pump_range.clear()
        self._users.clear()
        self._log("bot", "current-session leaderboard reset")

    # -- capacity events ------------------------------------------------------ #
    def _capev_effect(self, fx: str) -> bool:
        """True if any running capacity event (or a session-remainder lock)
        currently imposes this effect: disable_range | disable_ao | pause_events."""
        return fx in self._capev_session_fx or any(fx in s for s in self._capev_fx.values())

    def _capev_enabled_cmds(self) -> set:
        out = set()
        for s in self._capev_enable.values():
            out |= s
        return out

    def _capev_cmd_blocked(self, cmdkey: str) -> bool:
        """Chat-side gate: is this custom command disabled by a capacity event?
        Its own enable-list always wins; otherwise always-on and range-member
        commands answer to their respective disable effects."""
        if cmdkey in self._capev_enabled_cmds():
            return False
        if self._is_always_on(cmdkey):
            return self._capev_effect("disable_ao")
        return self._capev_effect("disable_range")

    def _spawn_post_actions(self, actions, label: str, hdr: str | None = None) -> None:
        """Queue a post-event action block as a DETACHED task — the event is
        already complete and its effects lifted, so this runs independently
        (e.g. 'wait 5 min, then do X'). Cancelled on pause / reset / off."""
        if not actions:
            return

        async def _run():
            try:
                await self._run_action_block(actions, label, hdr=hdr)
                self._log("bot", f"{label} complete")
            except asyncio.CancelledError:
                pass
        t = asyncio.create_task(_run())
        self._post_tasks.add(t)
        t.add_done_callback(self._post_tasks.discard)

    def _capev_cancel_all(self, clear_session: bool) -> None:
        for t in self._capev_tasks.values():
            t.cancel()
        self._capev_tasks.clear()
        self._capev_fx.clear()
        self._capev_enable.clear()
        for t in list(self._post_tasks):
            t.cancel()
        self._post_tasks.clear()
        if clear_session:
            self._capev_session_fx.clear()
            self._capev_done.clear()

    async def _check_capacity_events(self) -> None:
        """One-shot triggers at a capacity threshold (1-999%). Independent of
        ranges — their effects take precedence over normal range behaviour —
        but the End / End Sequence overrules them (nothing fires once the end
        has triggered, and the session end cancels them)."""
        if not self.cfg.get("listener_enabled") or self._end_triggered:
            return
        for ev in self.cfg.get("capacity_events", []):
            if not ev.get("enabled"):
                continue
            try:
                at = float(ev.get("at") or 0)
            except (TypeError, ValueError):
                continue
            if not (1 <= at <= 999):
                continue
            name = (ev.get("name") or "").strip() or f"capacity-{at:g}"
            key = name.lower()
            if key in self._capev_done or self.capacity < at:
                continue
            self._capev_done.add(key)
            self._log("bot", f"CAPACITY EVENT '{name}' triggered at {at:g}%")
            # Effects: event-scoped ones lift when the action block finishes;
            # "session remainder" ones stick until session reset / deactivation.
            fx = set()
            for flag, effect in (("disable_range_cmds", "disable_range"),
                                 ("disable_always_on", "disable_ao"),
                                 ("pause_events", "pause_events")):
                if ev.get(flag):
                    if (ev.get(flag + "_scope") or "event") == "session":
                        self._capev_session_fx.add(effect)
                    else:
                        fx.add(effect)
            if fx:
                self._capev_fx[key] = fx
            if ev.get("enable_commands_on"):
                en = {str(n).strip().lower() for n in (ev.get("enable_commands") or []) if str(n).strip()}
                if en:
                    self._capev_enable[key] = en
            if ev.get("stop_devices"):
                await self.abort(reason=f"capacity event {name}")
            self._capev_tasks[key] = asyncio.create_task(self._run_capev(ev, key, name))

    def _num_expr(self, val, xc: dict | None = None, default: float = 0.0) -> float:
        """A number, OR a placeholder expression that renders to one (so a fire/
        wait action can use 'seconds' = [winner_score], [range_leader_score], …).
        Returns `default` if it can't be parsed."""
        if val is None or val == "":
            return default
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val)
        if "[" in s:
            s = self.render(s, xc or {})
        try:
            return float(str(s).strip())
        except (TypeError, ValueError):
            return default

    async def _run_action_block(self, actions, name: str, hdr: str | None = None,
                                uid=None, who: str | None = None,
                                extra_ctx: dict | None = None) -> None:
        """Execute an ordered action block: message | broadcast | fire | roll |
        capacity | wait | poll | competition | award_prize | command_gate |
        winner_button | end_session. Shared by capacity events, poll outcomes,
        'actions'-type custom commands, and competition win blocks. A poll action
        BLOCKS the caller until it finishes. `name` labels logs; `hdr` (default =
        name) is the output-header tag; `uid`/`who` (a command's runner, or a
        competition winner) credit that person's leaderboard for the block's
        fires; `extra_ctx` (e.g. {'mention': ..}) is merged into every render so
        winner/mention placeholders resolve inside the block."""
        if hdr is None:
            hdr = name
        xc = dict(extra_ctx or {})
        if uid is not None and "user_bonus_secs" not in xc:
            ubs, ubp = self.user_bonus(uid)   # [user_bonus_*] for the block's runner
            xc["user_bonus_secs"] = f"{ubs:g}"
            xc["user_bonus_pct"] = f"{ubp:g}"

        def R(template, more=None):
            return self.render(template, {**xc, **(more or {})})

        for a in (actions or []):
            if self._paused or self._end_triggered:
                break
            # Optional poll-winner gate: an action tied to option N (if_option,
            # 1-based) runs only if the most recent poll's winner was option N.
            cond = (a or {}).get("if_option")
            if cond not in (None, 0, ""):
                try:
                    want = int(cond)
                except (TypeError, ValueError):
                    want = None
                have = None if self._last_poll_winner is None else self._last_poll_winner + 1
                if want != have:
                    continue
            typ = ((a or {}).get("type") or "message").lower()
            try:
                if typ == "wait":
                    await asyncio.sleep(max(0.0, self._num_expr(a.get("seconds"), xc)))
                    continue
                if typ in ("message", "embed", "embed_message", "winner_button", "session_leader_event"):
                    # Unified post action: a `message` posted plain OR as an embed
                    # (style), OPTIONALLY with a button gated to a target (winner/
                    # runnerup/range_leader/session_leader/top_bonus_holder/everyone/
                    # allowlist) that runs a nested action block. Legacy embed /
                    # embed_message / winner_button / session_leader_event map on.
                    if typ == "message":
                        style = (a.get("style") or "plain").lower()
                        tgt = (a.get("target") or "").strip().strip("[]").lower()
                        acts = a.get("actions") or []
                    elif typ == "embed_message":
                        style, tgt, acts = "embed", "", []
                    elif typ == "session_leader_event":
                        style, tgt, acts = "embed", "session_leader", (a.get("actions") or [])
                    elif typ == "winner_button":
                        style = "embed"; tgt = (a.get("target") or "[winner]").strip().strip("[]").lower()
                        acts = a.get("actions") or []
                    else:  # embed
                        style = "embed"; tgt = (a.get("target") or "").strip().strip("[]").lower()
                        acts = a.get("actions") or []
                    has_button = bool(tgt) and bool(acts)
                    allowed = None; primary_uid = None; primary_who = ""
                    if has_button:
                        if tgt in ("everyone", "all", "anyone"):
                            allowed = None   # anyone may press (single-use)
                        elif tgt == "allowlist":
                            allowed = set()
                            for row in (a.get("allow") or []):
                                u, w = self._resolve_award_target(row)
                                if u:
                                    allowed.add(str(u))
                                    if primary_uid is None:
                                        primary_uid, primary_who = u, w
                            if not allowed:
                                has_button = False   # nobody resolvable → plain embed
                        else:
                            u, w = self._resolve_award_target(f"[{tgt}]")
                            if u:
                                allowed = {str(u)}; primary_uid, primary_who = u, w
                            else:
                                has_button = False
                    # [target]/[mention] = the button's target; [winner] is left to
                    # the global last-winner so a plain embed's [winner] still works.
                    bctx = {"target": primary_who,
                            "mention": self._mention(primary_uid, primary_who) if primary_uid else ""}
                    title = R((a.get("title") or "").strip(), bctx)
                    body = R((a.get("message") or "").strip(), bctx)
                    if not has_button:
                        if style == "embed":
                            if self.embed_cb:
                                try:
                                    await self.embed_cb(title or "​", body)
                                except Exception as e:  # noqa: BLE001
                                    self._log("error", f"embed failed: {e}")
                                    await self._announce((f"**{title}**\n" if title else "") + body, None)
                            else:
                                await self._announce((f"**{title}**\n" if title else "") + body, None)
                        else:   # plain message
                            if body:
                                await self._announce(self._evt_hdr(hdr) + body, None)
                        continue
                    label = (a.get("label") or "").strip() or "🎁 Claim"
                    freeze = bool(a.get("freeze"))
                    self._cancel_winner_button()   # only one live at a time
                    self._winner_button = {"allowed": allowed, "actions": acts, "freeze": freeze,
                                           "name": name, "used": False, "label": label,
                                           "primary_uid": primary_uid, "primary_who": primary_who,
                                           "mention": bctx["mention"]}
                    if freeze:
                        self._cmd_gate_block = True
                    text = body or "🎁 Press the button!"
                    if self.winner_button_cb:
                        try:
                            await self.winner_button_cb(title or ("🏆 " + name), text, {"label": label, "name": name})
                        except Exception as e:  # noqa: BLE001
                            self._log("error", f"embed button post failed: {e}")
                            await self._announce(text, None)
                    else:
                        await self._announce(text, None)
                    deadline = self._num_expr(a.get("deadline"), xc)
                    if deadline > 0:
                        dmsg = (a.get("deadline_message") or "").strip()
                        if dmsg:
                            await self._announce(R(dmsg, {**bctx, "seconds": f"{deadline:g}"}), None)
                        self._winner_button_task = asyncio.create_task(
                            self._winner_button_deadline(deadline, a.get("timeout_message")))
                    continue
                if typ == "command":
                    # run another custom command by name (credited to the block's
                    # runner). Depth-guarded so a command that runs itself can't loop.
                    cmdname = (a.get("command") or "").strip()
                    cmd2 = self.find_command(cmdname) if cmdname else None
                    if cmd2 is None:
                        self._log("error", f"{name}: command action — no command '{cmdname}'")
                        continue
                    if self._inline_depth >= 3:
                        self._log("error", f"{name}: command action '{cmdname}' — nesting too deep, skipped")
                        continue
                    self._inline_depth += 1
                    try:
                        res = await self.run_custom(cmd2, who or self._anon_label(), uid=uid)
                        # fire/roll/say return reply text the caller posts; actions-
                        # type commands self-announce and return no reply.
                        if res.get("reply"):
                            await self._announce(res["reply"], None)
                    except Exception as e:  # noqa: BLE001
                        self._log("error", f"{name}: command action '{cmdname}' failed: {e}")
                    finally:
                        self._inline_depth -= 1
                    continue
                if typ == "capacity":
                    op = (a.get("capacity_op") or "add").lower()
                    val = float(a.get("capacity_value") or 0)
                    self.set_capacity(val if op == "set" else self.capacity + val)
                    continue
                if typ == "broadcast":
                    # posts one of the saved Broadcast presets (Commands tab)
                    bname = str(a.get("broadcast") or "").strip().lower()
                    row = next((b for b in self.cfg.get("broadcasts", [])
                                if (b.get("name") or "").strip().lower() == bname), None)
                    if row is None:
                        self._log("error", f"{name}: no broadcast named '{a.get('broadcast')}'")
                    elif (row.get("message") or "").strip():
                        await self._post_broadcast(R(row["message"].strip()))
                    continue
                if typ == "poll":
                    pd = self.find_poll(a.get("poll"))
                    if pd is None:
                        self._log("error", f"{name}: no poll named '{a.get('poll')}'")
                    else:
                        await self._run_poll(pd, source=name)
                    continue
                if typ == "competition":
                    # non-blocking: start it in the background, block continues
                    res = self.start_competition_bg(a.get("competition"), source=name)
                    if not res.get("ok"):
                        self._log("error", f"{name}: {res.get('error')}")
                    continue
                if typ == "bonus_round":
                    res = self.start_bonus_round_bg(a.get("bonus_round"), source=name)
                    if not res.get("ok"):
                        self._log("error", f"{name}: {res.get('error')}")
                    continue
                if typ == "end_session":
                    # End the session like the OFF switch: post an optional
                    # extra line, then the normal off-message + deactivate.
                    # Ends the block — nothing after end_session runs.
                    extra = (a.get("message") or "").strip()
                    if extra:
                        await self._announce(self._evt_hdr(hdr) + R(extra), None)
                    await self.end_session(source=name)
                    break
                if typ in ("award", "award_prize", "award_amount"):
                    # Unified award: award_type=command grants a bonus command;
                    # award_type=pct/secs banks a bonus amount for a Bonus Round.
                    # Legacy award_prize/award_amount map onto it.
                    if typ == "award_prize":
                        award_type = "command"
                    elif typ == "award_amount":
                        award_type = "pct" if (a.get("unit") or "secs").lower() in ("pct", "%", "cap", "capacity") else "secs"
                    else:
                        award_type = (a.get("award_type") or "command").lower()
                    if award_type != "command":
                        # bank a bonus AMOUNT (pump seconds or capacity %)
                        uid_t, who_t = self._resolve_award_target(a.get("target") or "[winner]")
                        if uid_t is None:
                            self._log("bot", f"{name}: award — no target for '{a.get('target')}', skipped")
                            continue
                        amt = round(self._num_expr(a.get("amount"), xc), 1)
                        rec = self._bonus_bank.setdefault(str(uid_t), {"name": who_t, "secs": 0.0, "pct": 0.0})
                        rec["name"] = who_t
                        if award_type == "pct":
                            rec["pct"] += amt
                        else:
                            rec["secs"] += amt
                        self._log("bot", f"{name}: banked {amt:g}{'%' if award_type=='pct' else 's'} bonus for {who_t}")
                        msg = (a.get("message") or "").strip()
                        if msg:
                            await self._announce(self._evt_hdr(hdr) + R(msg, {
                                "target": who_t, "winner": who_t,
                                "mention": self._mention(uid_t, who_t), "amount": f"{amt:g}",
                                "user_bonus_secs": f"{rec['secs']:g}", "user_bonus_pct": f"{rec['pct']:g}"}), None)
                        continue
                    # award_type == "command": grant a bonus command (charges/lock/
                    # deadline/stash) — bypasses cooldown/lock/range for the target.
                    bkey = (a.get("command") or "").strip().lower()
                    cmd = self.find_command(bkey) if bkey else None
                    if cmd is None:
                        self._log("error", f"{name}: award_prize — no command '{a.get('command')}'")
                        continue
                    uid_t, who_t = self._resolve_award_target(a.get("target"))
                    if uid_t is None:
                        self._log("bot", f"{name}: award_prize — no target for '{a.get('target')}', skipped")
                        continue
                    try:
                        charges = int(a.get("charges") or 0)
                    except (TypeError, ValueError):
                        charges = 0
                    stash = a.get("stash", True)
                    self._winner_grants[bkey] = str(uid_t)
                    if stash:
                        self._winner_grants[bkey + "\x00stash"] = "1"   # survive range changes
                    else:
                        self._winner_grants.pop(bkey + "\x00stash", None)
                    if charges > 0:
                        self._grant_charges[bkey] = charges
                    else:
                        self._grant_charges.pop(bkey, None)   # 0/blank → unlimited until session reset
                    prefix = self.cfg.get("command_prefix", "!")
                    actx = {"winner": who_t, "target": who_t,
                            "mention": self._mention(uid_t, who_t),
                            "bonus_cmd": f"{prefix}{bkey}",
                            "charges": ("∞" if charges <= 0 else str(charges))}
                    if a.get("lock"):
                        self._progression_lock = {"uid": str(uid_t), "cmd": bkey}
                        self._log("bot", f"progression LOCKED until {who_t} uses !{bkey}")
                    self._log("bot", f"{name}: awarded !{bkey} to {who_t} "
                                     f"({'∞' if charges <= 0 else charges} charges)")
                    msg = (a.get("message") or "").strip()
                    if msg:
                        await self._announce(self._evt_hdr(hdr) + R(msg, actx), None)
                    deadline = self._num_expr(a.get("deadline"), xc)
                    if deadline > 0:
                        self._bonus_pending = {"cmd": bkey, "uid": str(uid_t), "who": who_t, "used": False}
                        dmsg = (a.get("deadline_message") or "").strip() or \
                            "⏳ **[target]**, use **[bonus_cmd]** within **[seconds]s** or it fires automatically!"
                        await self._announce(R(dmsg, {**actx, "seconds": f"{deadline:g}"}), None)
                        self._deadline_task = asyncio.create_task(
                            self._winner_deadline(deadline, bkey,
                                                  {"timeout_message": a.get("timeout_message")}))
                    continue
                if typ == "command_gate":
                    # Stackable gate modifiers (one action, many rows). Persist until
                    # a remove_block / resume / session reset. Exemptions: owner,
                    # bot-internal, granted commands, a live Winner Button target.
                    mods = a.get("modifiers")
                    if not mods:   # legacy single gate_mode
                        mods = [{"op": (a.get("gate_mode") or "block_all").lower()}]
                    for m in mods:
                        op = (m.get("op") or "").lower()
                        ck = (m.get("command") or "").strip().lower()
                        if op == "block_all":
                            self._cmd_gate_block = True
                        elif op in ("remove_block", "remove", "off", "none"):
                            self._cmd_gate_block = False
                            self._cmd_gate_allow.clear()
                            self._cmd_gate_blocked_cmds.clear()
                            self._capev_session_fx.discard("disable_range")
                            self._capev_session_fx.discard("disable_ao")
                            self._capev_session_fx.discard("pause_events")
                        elif op == "allow" and ck:
                            self._cmd_gate_allow.add(ck)
                        elif op == "block" and ck:
                            self._cmd_gate_blocked_cmds.add(ck)
                        elif op == "unblock" and ck:
                            self._cmd_gate_blocked_cmds.discard(ck)
                        elif op == "disable_range_cmds":
                            self._capev_session_fx.add("disable_range")
                        elif op == "resume_range_cmds":
                            self._capev_session_fx.discard("disable_range")
                        elif op == "disable_always_on":
                            self._capev_session_fx.add("disable_ao")
                        elif op == "resume_always_on":
                            self._capev_session_fx.discard("disable_ao")
                        elif op == "pause_events":
                            self._capev_session_fx.add("pause_events")
                        elif op == "resume_events":
                            self._capev_session_fx.discard("pause_events")
                    self._log("bot", f"{name}: command_gate applied {len(mods)} modifier(s)")
                    msg = (a.get("message") or "").strip()
                    if msg:
                        await self._announce(self._evt_hdr(hdr) + R(msg), None)
                    continue
                if typ in ("fire", "roll"):
                    target = a.get("device_id") or self._active_id()
                    if self._device(target) is None:
                        self._log("error", f"{name}: no target device")
                        continue
                    until = None
                    if typ == "roll":
                        rd, rs = self.range_dice(self.range_for(self.capacity))
                        dice = int(a.get("dice") or rd)
                        sides = int(a.get("sides") or rs)
                        total, _ = self._roll_total(dice, sides)
                        dur = self._duration_from_total(total)
                    else:
                        # fire amount: fixed seconds, or pump until N% is ADDED
                        # ('add') / until capacity REACHES N% ('to'). The %-modes
                        # use until_capacity (exempt from the single-fire cap).
                        mode = (a.get("fire_mode") or "seconds").lower()
                        if mode in ("add", "fill", "add_pct"):
                            until = self.capacity + self._num_expr(a.get("fill_pct"), xc)
                        elif mode in ("to", "to_pct", "until"):
                            until = self._num_expr(a.get("fill_pct"), xc)
                        dur = round(max(0.1, min(self._hard_cap(),
                                                 self._num_expr(a.get("seconds"), xc, 3.0))), 1)
                    if until is not None:
                        fr = self._begin_or_extend(target, 0.0, name, until_capacity=until)
                    else:
                        fr = self._begin_or_extend(target, dur, name)
                    dur = round(fr.get("added", dur), 1)   # seconds delivered → [secs], credit
                    wait_secs = round(fr.get("remaining", dur), 1)   # time until the pump ends
                    if uid is not None and fr.get("status") in ("started", "extended"):
                        self.credit_pump(uid, who, dur, target)
                    msg = (a.get("message") or "").strip()
                    if msg:
                        await self._announce(self._evt_hdr(hdr) + R(
                            msg, {"secs": f"{dur:.1f}", "seconds": f"{dur:.1f}"}), None)
                    # "block others while pumping" + Post-Pump Actions: when either
                    # is set the fire WAITS OUT its run (freezing all other custom
                    # commands + roll if asked), then runs the post block.
                    block = bool(a.get("block_during"))
                    post = a.get("post_actions")
                    if (block or post) and wait_secs > 0:
                        set_gate = block and not self._cmd_gate_block
                        if set_gate:
                            self._cmd_gate_block = True
                        try:
                            await asyncio.sleep(wait_secs)
                        finally:
                            if set_gate:
                                self._cmd_gate_block = False
                        if post and not self._paused and not self._end_triggered:
                            await self._run_action_block(
                                post, f"{name} (post-pump)", hdr, uid, who,
                                extra_ctx={**xc, "secs": f"{dur:.1f}", "seconds": f"{dur:.1f}"})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — one bad action shouldn't kill the block
                self._log("error", f"{name} action failed: {e}")

    async def _run_capev(self, ev: dict, key: str, name: str) -> None:
        """Execute the event's action block sequentially. The event counts as
        'running' (its effects active) until the block finishes; then any
        Post Event Commands are queued detached (not part of the event)."""
        completed = False
        try:
            await self._run_action_block(ev.get("actions"), f"capacity event {name}", hdr="CAPACITY EVENT")
            completed = True
        except asyncio.CancelledError:
            pass
        finally:
            self._capev_fx.pop(key, None)
            self._capev_enable.pop(key, None)
            self._capev_tasks.pop(key, None)
            self._log("bot", f"CAPACITY EVENT '{name}' complete")
        # Effects are lifted and the event is cleared — NOW queue post-event
        # commands (unless we were cancelled or the session is ending).
        if completed and not self._paused and not self._end_triggered:
            self._spawn_post_actions(ev.get("post_actions"), f"capacity event {name} (post)",
                                     hdr="CAPACITY EVENT")

    async def end_session(self, source: str = "end_session") -> None:
        """End the session like the OFF switch (posts the off-message), but
        stop all devices first — it's an explicit end, not just a mute."""
        await self.abort(reason=source)
        self._log("bot", f"SESSION ENDED ({source})")
        if self.end_session_cb:
            try:
                await self.end_session_cb(post_off_message=True)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"end session failed: {e}")

    # -- polls ---------------------------------------------------------------- #
    def find_poll(self, name) -> dict | None:
        key = str(name or "").strip().lower()
        if not key:
            return None
        for p in self.cfg.get("polls", []):
            if (p.get("name") or "").strip().lower() == key:
                return p
        return None

    def poll_active(self) -> bool:
        return self._poll is not None

    async def _post_broadcast(self, text: str) -> None:
        """Post a Broadcast-preset action as a rich embed (falls back to text)."""
        if not (text or "").strip():
            return
        if self.broadcast_embed_cb:
            try:
                await self.broadcast_embed_cb(text)
                return
            except Exception as e:  # noqa: BLE001
                self._log("error", f"broadcast embed failed: {e}")
        await self._announce(text, None)

    async def _announce_poll(self, title: str, text: str, options: list | None = None) -> None:
        """Post the poll as a rich embed (falls back to plain text). `options`
        (the option labels) rides along on live poll posts so the transport can
        attach vote buttons; results posts pass None."""
        if self.embed_cb:
            try:
                await self.embed_cb(title, text, options=options)
                return
            except Exception as e:  # noqa: BLE001
                self._log("error", f"poll embed failed: {e}")
        await self._announce(f"**{title}**\n{text}", None)

    def _poll_counts(self, opts: list) -> list[int]:
        counts = [0] * len(opts)
        for idx in (self._poll or {}).get("votes", {}).values():
            if 0 <= idx < len(opts):
                counts[idx] += 1
        return counts

    def _poll_text(self, pd: dict, opts: list, counts: list | None = None,
                   remaining: float | None = None) -> str:
        prefix = self.cfg.get("command_prefix", "!")
        vote = self.builtin_names()["vote"]
        body = self.render((pd.get("body") or "").strip())
        lines = [f"**{i + 1}.** {o.get('label', '')}"
                 + (f" — {counts[i]} vote{'s' if counts[i] != 1 else ''}" if counts else "")
                 for i, o in enumerate(opts)]
        out = ((body + "\n\n") if body else "") + "\n".join(lines) + \
            f"\n\nVote with `{prefix}{vote} 1`–`{prefix}{vote} {len(opts)}`"
        if remaining is not None:
            out += f"\n⏳ **{self._fmt_duration(remaining)}** remaining"
        return out

    async def _run_poll(self, pd: dict, source: str = "poll") -> None:
        """Run one poll start-to-finish: post the embed, collect votes for the
        duration (reposting every repeat_every seconds), tally, announce the
        result, and execute the winning option's action block. BLOCKING — a
        caller inside an event's action block stays 'running' throughout."""
        name = (pd.get("name") or "poll").strip()
        if self._poll is not None:
            self._log("error", f"{source}: poll '{name}' skipped — another poll is already running")
            return
        opts = [o for o in (pd.get("options") or [])[:4] if (o.get("label") or "").strip()]
        if not opts:
            self._log("error", f"{source}: poll '{name}' has no options")
            return
        title = "Poll: " + self.render((pd.get("title") or name).strip())
        try:
            duration = max(5.0, min(3600.0, float(pd.get("duration") or 60)))
        except (TypeError, ValueError):
            duration = 60.0
        try:
            repeat = max(0.0, float(pd.get("repeat_every") or 0))
        except (TypeError, ValueError):
            repeat = 0.0
        if repeat and repeat < 5:
            repeat = 5.0
        self._poll = {"def": pd, "opts": opts, "votes": {}, "voters": {}}
        self._log("bot", f"POLL '{name}' started ({duration:g}s, {len(opts)} options) [{source}]")
        winner_idx = None
        labels = [o.get("label", "") for o in opts]
        try:
            end = time.monotonic() + duration
            await self._announce_poll(title, self._poll_text(pd, opts, remaining=duration),
                                      options=labels)
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, repeat) if repeat else remaining)
                if repeat and time.monotonic() < end - 1:
                    # reposts carry the live tally + time left
                    await self._announce_poll(
                        title, self._poll_text(pd, opts, self._poll_counts(opts),
                                               remaining=max(0.0, end - time.monotonic())),
                        options=labels)
            # tally
            counts = self._poll_counts(opts)
            total = sum(counts)
            if total == 0:
                winner_idx = next((i for i, o in enumerate(opts) if o.get("fallback")), None)
                note = ("No votes — the fallback option wins." if winner_idx is not None
                        else "No votes — no outcome.")
            else:
                top = max(counts)
                winner_idx = random.choice([i for i, c in enumerate(counts) if c == top])
                tied = counts.count(top) > 1
                note = f"{total} vote{'s' if total != 1 else ''} in." + (" Tie broken at random!" if tied else "")
            lines = [f"{'🏆 ' if i == winner_idx else ''}**{i + 1}.** {o.get('label', '')} — "
                     f"{counts[i]} vote{'s' if counts[i] != 1 else ''}"
                     for i, o in enumerate(opts)]
            await self._announce_poll(title + " — RESULTS", "\n".join(lines) + f"\n\n{note}")
            self._log("bot", f"POLL '{name}' finished — " +
                      (f"winner: option {winner_idx + 1}" if winner_idx is not None else "no outcome"))
        except asyncio.CancelledError:
            self._log("bot", f"POLL '{name}' cancelled")
            raise
        finally:
            self._poll = None
        # Remember the winner so post-event commands can gate on it.
        self._last_poll_winner = winner_idx
        if winner_idx is not None:
            await self._run_action_block(opts[winner_idx].get("actions"), f"poll {name}")

    def cast_vote(self, uid, who: str, arg: str) -> dict | None:
        """Handle the vote system command. None = no poll running (stay silent
        — the command only exists during a poll). Otherwise a dict: `reply`
        (to the voter, for usage errors) or `broadcast` (a QUIET vote notice —
        the ballot itself stays secret; the channel only learns THAT they
        voted, never what for)."""
        if self._poll is None:
            return None
        opts = self._poll["opts"]
        prefix = self.cfg.get("command_prefix", "!")
        vote = self.builtin_names()["vote"]
        try:
            n = int(str(arg).strip())
        except (TypeError, ValueError):
            return {"reply": f"🗳 Vote with `{prefix}{vote} 1`–`{prefix}{vote} {len(opts)}`"}
        if not (1 <= n <= len(opts)):
            return {"reply": f"🗳 That poll has options 1–{len(opts)}."}
        if str(uid) in self._poll["votes"]:
            return {"reply": f"🗳 {who}, you already voted in this poll — one vote per person!"}
        self._poll["votes"][str(uid)] = n - 1
        self._poll["voters"][str(uid)] = who
        # `label` is for the voter's PRIVATE confirmation (ephemeral button/
        # slash replies) — the channel broadcast never reveals the choice.
        return {"broadcast": f"🗳 **[Poll]** {who} has voted in the poll!",
                "label": opts[n - 1].get("label", "")}

    def start_poll_bg(self, name, source: str = "manual") -> dict:
        """Start a named poll in the BACKGROUND — used by poll-type commands and
        the dashboard Controls. Never blocks anything: other commands keep
        working while it runs (only a poll inside an event's action block
        blocks, and then only that block)."""
        pd = self.find_poll(name)
        if pd is None:
            return {"ok": False, "error": f"no poll named '{name}' — create it in Events → Polls"}
        if self._poll is not None:
            return {"ok": False, "error": "🗳 A poll is already running — wait for it to finish."}
        if self._paused:
            return {"ok": False, "error": "session is paused"}
        self._poll_task = asyncio.create_task(self._run_poll(pd, source=source))
        return {"ok": True}

    def _cancel_poll_task(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        self._poll = None

    # -- competitions ("roll-offs" and friends) ------------------------------ #
    def find_competition(self, name) -> dict | None:
        key = str(name or "").strip().lower()
        if not key:
            return None
        for c in self.cfg.get("competitions", []):
            if (c.get("name") or "").strip().lower() == key:
                return c
        return None

    def competition_active(self) -> bool:
        return self._comp is not None

    def _comp_player(self, uid, who: str) -> dict:
        p = self._comp["players"].setdefault(str(uid), {"name": who, "score": 0.0,
                                                         "entries": 0, "entered": False,
                                                         "done_at": None, "rolls": []})
        p["name"] = who
        return p

    # -- ephemeral-roller API (driven by the Discord "Enter Challenge" flow) --
    def competition_window_open(self) -> bool:
        return self._comp is not None

    def _comp_rolls(self, cd: dict) -> int:
        specs = cd.get("roll_specs") or []
        if specs:
            return len(specs)
        return int(cd.get("max_entries") or 0) or int(cd.get("required_entries") or 0) or 1

    def _roll_spec(self, spec: dict) -> float:
        """Roll one roll-off row: its own NdN dice with a per-row luck modifier
        (positive % = chance to force the max, negative % = force the min)."""
        dice = max(1, int(spec.get("dice") or 1))
        sides = max(1, int(spec.get("sides") or 6))
        total, _ = self._roll_total(dice, sides)
        try:
            luck = float(spec.get("luck") or 0)
        except (TypeError, ValueError):
            luck = 0.0
        if luck > 0 and random.random() * 100 < min(100.0, luck):
            total = dice * sides
        elif luck < 0 and random.random() * 100 < min(100.0, -luck):
            total = dice
        return float(total)

    def competition_join(self, uid, who: str) -> dict:
        """A player pressed 'Enter Challenge'. Registers them and returns the
        roller config (how many rolls, how many rerolls). Refuses if they've
        already submitted."""
        if self._comp is None:
            return {"ok": False, "error": "This challenge has ended."}
        cd = self._comp["def"]
        p = self._comp_player(uid, who)
        if p.get("done_at") is not None:
            return {"ok": False, "error": "You've already locked in your rolls!"}
        p["entered"] = True
        rerolls = int(cd.get("reroll_count") or 0) if cd.get("allow_reroll") else 0
        return {"ok": True, "rolls": self._comp_rolls(cd), "rerolls": max(0, rerolls),
                "metric": self._comp.get("metric", "total")}

    def competition_roll_value(self, slot=None) -> float:
        """Produce one roll value for the given 0-based slot. With per-roll specs
        (roll-off rows) each slot uses its own NdN + luck; otherwise falls back
        to the entry command's dice."""
        if self._comp is None:
            return 0.0
        specs = self._comp["def"].get("roll_specs") or []
        if specs:
            i = 0 if slot is None else max(0, min(len(specs) - 1, int(slot)))
            return self._roll_spec(specs[i])
        entry = self.find_command(self._comp["cmd"])
        return self._comp_value(entry) if entry else float(self._roll_total(1, 6)[0])

    def competition_submit(self, uid, who: str, rolls: list) -> dict:
        """Record a player's finished rolls, score them by the metric, mark them
        done. Returns the all-at-once summary for the channel announcement."""
        if self._comp is None:
            return {"ok": False, "error": "This challenge has ended."}
        p = self._comp_player(uid, who)
        if p.get("done_at") is not None:
            return {"ok": False, "error": "already submitted"}
        vals = []
        for r in (rolls or []):
            try:
                vals.append(float(r))
            except (TypeError, ValueError):
                pass
        p["rolls"] = vals
        p["entries"] = len(vals)
        metric = self._comp.get("metric", "total")
        if metric == "highest":
            p["score"] = max(vals) if vals else 0.0
        elif metric == "count":
            p["score"] = float(len(vals))
        else:
            p["score"] = float(sum(vals))
        p["entered"] = True
        p["done_at"] = time.monotonic()
        shown = ", ".join(f"{v:g}" for v in vals)
        summary = f"🎲 **{who}** rolled {shown} → **{p['score']:g}**!" if vals else f"🎲 **{who}** entered."
        return {"ok": True, "summary": summary, "total": p["score"]}

    def _comp_eligible(self) -> list:
        """Players who qualify to win: entered (if required) and met the
        required-entries threshold (else eliminated)."""
        c = self._comp
        req_enter = c["def"].get("require_enter", True)
        # roll-off: must complete ALL rolls (submitted) to qualify.
        need = self._comp_rolls(c["def"])
        out = []
        for uid, p in c["players"].items():
            if req_enter and not p["entered"]:
                continue
            if p["entries"] < need:
                continue
            out.append((uid, p))
        return out

    def enter_competition(self, uid, who: str) -> str | None:
        """The !enter system command — register for the running competition.
        None if nothing's running (stay silent). Returns a reply otherwise."""
        if self._comp is None:
            return None
        p = self._comp_player(uid, who)
        if p["entered"]:
            return f"✅ {who}, you're already in — go compete!"
        p["entered"] = True
        return f"🙋 **{who}** entered the {self._comp['def'].get('type', 'competition')}!"

    def _comp_value(self, cmd: dict) -> float:
        """The score contribution of one entry-command use, by the command type:
        a roll's total, a fire's seconds, else 1 (a plain 'use')."""
        typ = (cmd.get("type") or "fire").lower()
        if typ == "roll":
            rd, rs = self.range_dice(self.range_for(self.capacity))
            dice = int(cmd.get("dice") or rd)
            sides = int(cmd.get("sides") or rs)
            total, _ = self._roll_total(dice, sides)
            return float(total)
        if typ == "fire":
            try:
                return float(cmd.get("seconds") or 1)
            except (TypeError, ValueError):
                return 1.0
        return 1.0

    async def competition_entry(self, cmd: dict, who: str, uid) -> dict:
        """A use of the entry command while a competition is live. Bypasses the
        command's normal cooldown/fire — it scores into the contest instead
        (only the winner's total fires, at the end)."""
        c = self._comp
        cd = c["def"]
        p = self._comp_player(uid, who)
        if cd.get("require_enter", True) and not p["entered"]:
            prefix = self.cfg.get("command_prefix", "!")
            return {"ok": False, "error": f"🙋 {who}, type `{prefix}{self.builtin_names()['enter']}` to join first!"}
        cap = int(c.get("cap") or 0)
        if cap and p["entries"] >= cap:
            return {"ok": False, "error": f"🚫 {who}, you're out of entries ({cap} max)."}
        val = self._comp_value(cmd)
        p["entries"] += 1
        metric = c.get("metric", "total")
        if metric == "highest":
            p["score"] = max(p["score"], val)
        elif metric == "count":
            p["score"] = p["entries"]
        else:  # total
            p["score"] += val
        need = int(c.get("required_entries") or 0)
        if p["done_at"] is None and p["entries"] >= max(1, need):
            p["done_at"] = time.monotonic()   # for 'race' (first to finish)
        anon = self._anon_label()
        base = {"value": f"{val:g}", "score": f"{p['score']:g}", "entries": p["entries"],
                "max_entries": cap or "∞"}
        tmpl = cd.get("entry_message") or "🎲 [user] entered **[value]** — total **[score]** ([entries] in)"
        return {"ok": True, "device": False, "started": False, "competition": True,
                "reply": self.render(tmpl, {"user": who, "mention": self._mention(uid, who), **base}),
                "reply_anon": self.render(tmpl, {"user": anon, "mention": anon, **base})}

    def start_competition_bg(self, name, source: str = "manual") -> dict:
        cd = self.find_competition(name)
        if cd is None:
            return {"ok": False, "error": f"no competition named '{name}'"}
        if self._comp is not None:
            return {"ok": False, "error": "a competition is already running"}
        if self._paused:
            return {"ok": False, "error": "session is paused"}
        self._comp_task = asyncio.create_task(self._run_competition(cd, source))
        return {"ok": True}

    async def _run_competition(self, cd: dict, source: str = "comp") -> None:
        name = (cd.get("name") or "competition").strip()
        typ = (cd.get("type") or "rolloff").lower()
        entry = self.find_command(cd.get("command") or "")
        entry_key = (cd.get("command") or "").strip().lower()
        try:
            duration = max(5.0, min(3600.0, float(cd.get("duration") or 60)))
        except (TypeError, ValueError):
            duration = 60.0
        try:
            repeat = max(0.0, float(cd.get("repeat_every") or 0))
        except (TypeError, ValueError):
            repeat = 0.0
        if repeat and repeat < 5:
            repeat = 5.0
        self._comp = {"def": cd, "cmd": entry_key, "metric": cd.get("metric", "total"),
                      "cap": int(cd.get("max_entries") or 0), "players": {}, "type": typ,
                      "required_entries": int(cd.get("required_entries") or 0)}
        rolls = self._comp_rolls(cd)
        rerolls = int(cd.get("reroll_count") or 0) if cd.get("allow_reroll") else 0
        title = "🏁 " + self.render((cd.get("title") or name).strip())
        body = self.render((cd.get("body") or cd.get("intro") or "").strip()) or self._comp_intro(cd, entry_key, duration)
        meta = {"name": name, "rolls": rolls, "rerolls": max(0, rerolls), "type": typ}
        self._log("bot", f"COMPETITION '{name}' ({typ}) started [{source}]")
        winner_uid = winner = None

        async def _post(text):
            if self.comp_embed_cb:
                try:
                    await self.comp_embed_cb(title, text, meta)
                    return
                except Exception as e:  # noqa: BLE001
                    self._log("error", f"competition embed failed: {e}")
            await self._announce_poll(title, text)   # fallback: plain embed, !enter flow

        def _repost_text(rem):
            # custom repeat message ([remaining]/[remaining_seconds]/[standings])
            # or the default body + standings + countdown.
            rmsg = (cd.get("repeat_message") or "").strip()
            ctx = {"remaining": self._fmt_duration(rem), "remaining_seconds": int(rem),
                   "standings": self._comp_standings()}
            if rmsg:
                return self.render(rmsg, ctx)
            return f"{body}\n\n{self._comp_standings()}\n⏳ **{self._fmt_duration(rem)}** left"

        try:
            end = time.monotonic() + duration
            await _post(f"{body}\n\n⏳ **{self._fmt_duration(duration)}** to enter & roll!")
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, repeat) if repeat else min(remaining, 1.0))
                if repeat and time.monotonic() < end - 1:
                    await _post(_repost_text(max(0, end - time.monotonic())))
            # decide winner among eligible players (roll-off: highest score wins;
            # ties broken at random). race & raffle were removed for a redesign —
            # any legacy value is treated as a roll-off.
            elig = self._comp_eligible()
            if elig:
                top = max(p["score"] for _, p in elig)
                winners = [(u, p) for u, p in elig if p["score"] == top]
                winner_uid, winner = random.choice(winners)
        except asyncio.CancelledError:
            self._log("bot", f"COMPETITION '{name}' cancelled")
            self._comp = None
            raise
        # "Add ALL Player Totals to Timer": every finisher's total is fired to
        # the pump and credited to THAT player (not just the winner's) — fairer
        # for a running session contest where everyone's rolls should count.
        if cd.get("add_all_totals") and winner is not None:
            for u, p in self._comp["players"].items():
                if p.get("done_at") is None:
                    continue
                secs = round(max(0.0, float(p.get("score") or 0)), 1)
                if secs <= 0:
                    continue
                fr = self._begin_or_extend(self._active_id(), secs,
                                           f"competition {name} totals", bypass_lock=True)
                if fr.get("status") in ("started", "extended"):
                    self.credit_pump(u, p["name"], fr.get("added", secs), self._active_id())
            self._log("bot", f"COMPETITION '{name}' — all player totals added to timer")
        results_text = self._comp_results()   # capture the scoreboard before clearing
        ru_name, ru_score = self._comp_runnerup(winner_uid)   # 2nd place, before clearing
        total_score = self._comp_total_score()   # combined total, before clearing
        self._comp = None
        self._last_results = results_text
        self._last_runnerup, self._last_runnerup_score = ru_name, ru_score
        self._last_total_score = total_score
        await self._finish_competition(cd, name, entry, winner_uid, winner, results_text)

    def _comp_intro(self, cd: dict, entry_key: str, duration: float) -> str:
        prefix = self.cfg.get("command_prefix", "!")
        en = f"{prefix}{self.builtin_names()['enter']}"
        need = int(cd.get("required_entries") or 0)
        parts = [f"Type `{en}` to join"]
        if entry_key:
            parts.append(f"then use `{prefix}{entry_key}`" + (f" ×{need}" if need else ""))
        return "**HIGHEST SCORE WINS** — " + ", ".join(parts) + f". {duration:g}s!"

    def _comp_standings(self) -> str:
        rows = sorted(self._comp["players"].values(), key=lambda p: p["score"], reverse=True)
        rows = [p for p in rows if p["entered"] or not self._comp["def"].get("require_enter", True)]
        if not rows:
            return "No entrants yet — press Enter Challenge to join!"
        lines = [f"**{p['name']}** — {p['score']:g} ({p['entries']} in)" for p in rows[:10]]
        return "\n".join(lines)

    def _comp_total_score(self) -> float:
        """Sum of every finisher's score in the current competition ([total_score]
        — the combined dice total across all players)."""
        if self._comp is None:
            return 0.0
        return sum(float(p.get("score") or 0) for p in self._comp["players"].values()
                   if p.get("done_at") is not None)

    def _comp_runnerup(self, winner_uid):
        """(name, score) of the 2nd-place finisher for [runnerup], or ("", 0.0).
        Highest-scoring done player who isn't the winner."""
        if self._comp is None:
            return ("", 0.0)
        others = [(u, p) for u, p in self._comp["players"].items()
                  if p.get("done_at") is not None and str(u) != str(winner_uid)]
        if not others:
            return ("", 0.0)
        _, p = max(others, key=lambda kv: kv[1].get("score", 0.0))
        return (p.get("name") or "?", float(p.get("score") or 0.0))

    def _comp_results(self) -> str:
        """Final scoreboard of everyone who entered (the [results] placeholder).
        Finished players first (by score), then any who entered but didn't
        finish, marked eliminated."""
        if self._comp is None:
            return ""
        players = list(self._comp["players"].values())
        done = sorted([p for p in players if p["done_at"] is not None],
                      key=lambda p: p["score"], reverse=True)
        dnf = [p for p in players if p["entered"] and p["done_at"] is None]
        if not done and not dnf:
            return "Nobody entered."
        lines = [f"**{i}.** {p['name']} — **{p['score']:g}**" for i, p in enumerate(done, 1)]
        lines += [f"• {p['name']} — did not finish" for p in dnf]
        return "\n".join(lines)

    async def _finish_competition(self, cd: dict, name: str, entry, winner_uid, winner,
                                  results: str = "") -> None:
        async def _result_embed(title, text):
            if self.embed_cb:
                try:
                    await self.embed_cb(f"🏁 {title}", text)
                    return
                except Exception as e:  # noqa: BLE001
                    self._log("error", f"result embed failed: {e}")
            await self._announce(text, None)
        if winner is None:
            self._last_winner, self._last_winner_score = "", 0.0
            msg = (cd.get("no_winner_message") or "").strip() or f"**{name}**: no qualifying winner."
            await _result_embed(name, self.render(msg, {"results": results}))
            await self._run_action_block(cd.get("no_winner_actions"),
                                         f"competition {name} no-winner", hdr="COMPETITION")
            self._log("bot", f"COMPETITION '{name}' — no winner")
            return
        self._last_winner = winner["name"]
        self._last_winner_score = winner["score"]
        mention = self._mention(winner_uid, winner["name"])
        # announce the win as a results embed
        msg = (cd.get("win_message") or "").strip() or "🏆 **[winner]** wins with **[winner_score]**!\n\n[results]"
        await _result_embed(name, self.render(msg, {"mention": mention}))
        self._log("bot", f"COMPETITION '{name}' — winner {winner['name']} ({winner['score']:g})")
        # Prizes are an ACTION BLOCK now: fire [winner_score] to the pump, hand
        # the winner a Winner Button, award_prize a bonus command, gate everyone
        # else — whatever the operator built. Fires credit the winner; [winner]/
        # [winner_score]/[runnerup]/[results]/[mention] all resolve inside it.
        await self._run_action_block(cd.get("win_actions"), f"competition {name} win",
                                     hdr="COMPETITION", uid=str(winner_uid),
                                     who=winner["name"], extra_ctx={"mention": mention})

    async def _winner_deadline(self, seconds: float, bkey: str, cd: dict) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        bp = self._bonus_pending
        if not bp or bp.get("used") or bp.get("cmd") != bkey:
            return
        cmd = self.find_command(bkey)
        if cmd is None:
            return
        tmsg = (cd.get("timeout_message") or "").strip() or "⌛ Time's up — **[bonus_cmd]** fires automatically!"
        prefix = self.cfg.get("command_prefix", "!")
        await self._announce(self.render(tmsg, {"winner": bp["who"], "bonus_cmd": f"{prefix}{bkey}"}), None)
        try:
            await self.run_custom(cmd, bp["who"], uid=bp["uid"])   # run it AS the winner → lifts lock, fires
        except Exception as e:  # noqa: BLE001
            self._log("error", f"winner deadline auto-fire failed: {e}")

    def _cancel_deadline(self) -> None:
        if self._deadline_task is not None:
            self._deadline_task.cancel()
            self._deadline_task = None
        self._bonus_pending = None

    # -- Winner Button (one-press prize) ------------------------------------- #
    def winner_button_can_press(self, uid) -> bool:
        """An embed button is pressable by: anyone (allowed is None = 'everyone'),
        or a uid in its allow-set."""
        wb = self._winner_button
        if not wb or wb.get("used"):
            return False
        allowed = wb.get("allowed")
        return allowed is None or str(uid) in allowed

    def winner_button_label(self) -> str:
        wb = self._winner_button
        return (wb or {}).get("label", "") if wb else ""

    async def _run_winner_button(self, wb: dict, uid, who: str, tag: str) -> None:
        """Run a claimed/expired embed button's action block, credited to the
        person who triggered it, exposing [winner]/[target]/[mention]."""
        if wb.get("freeze"):
            self._cmd_gate_block = False
        mention = self._mention(uid, who) if uid else (wb.get("mention") or who or "")
        await self._run_action_block(wb.get("actions"), f"embed button ({wb['name']}) {tag}",
                                     hdr="EMBED", uid=(str(uid) if uid else None), who=who or wb.get("primary_who") or "",
                                     extra_ctx={"target": who or "", "mention": mention})

    async def press_winner_button(self, uid, who: str) -> dict:
        """An eligible person pressed the button → run its block once (crediting
        the presser, lifting any freeze). Idempotent."""
        wb = self._winner_button
        if not wb or wb.get("used"):
            return {"ok": False, "error": "no button"}
        if not self.winner_button_can_press(uid):
            return {"ok": False, "error": "not for you"}
        wb["used"] = True
        self._cancel_winner_button_deadline()
        self._winner_button = None
        await self._run_winner_button(wb, str(uid), who, "pressed")
        return {"ok": True}

    async def _winner_button_deadline(self, seconds: float, timeout_message) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        wb = self._winner_button
        if not wb or wb.get("used"):
            return
        wb["used"] = True
        who = wb.get("primary_who") or ""
        tmsg = (timeout_message or "").strip() or \
            "⌛ Time's up — the prize triggers automatically!"
        await self._announce(self.render(tmsg, {"winner": who, "target": who,
                                                "mention": wb.get("mention") or who}), None)
        self._winner_button = None
        await self._run_winner_button(wb, wb.get("primary_uid"), who, "timeout")

    def _cancel_winner_button_deadline(self) -> None:
        if self._winner_button_task is not None:
            self._winner_button_task.cancel()
            self._winner_button_task = None

    def _cancel_winner_button(self) -> None:
        """Drop any live Winner Button (new one replacing it, or session reset)
        and lift a freeze it set. Does NOT run its block."""
        self._cancel_winner_button_deadline()
        wb = self._winner_button
        if wb and wb.get("freeze"):
            self._cmd_gate_block = False
        self._winner_button = None

    def _cancel_competition(self) -> None:
        if self._comp_task is not None:
            self._comp_task.cancel()
            self._comp_task = None
        self._comp = None

    # -- Bonus Round (teamwork cash-in) -------------------------------------- #
    def find_bonus_round(self, name) -> dict | None:
        key = (name or "").strip().lower()
        for b in self.cfg.get("bonus_rounds", []):
            if (b.get("name") or "").strip().lower() == key:
                return b
        return None

    def start_bonus_round_bg(self, name, source: str = "manual") -> dict:
        brd = self.find_bonus_round(name)
        if brd is None:
            return {"ok": False, "error": f"no bonus round named '{name}'"}
        if self._bonus_round is not None:
            return {"ok": False, "error": "a bonus round is already running"}
        if self._paused:
            return {"ok": False, "error": "session is paused"}
        self._bonus_round_task = asyncio.create_task(self._run_bonus_round(brd, source))
        return {"ok": True}

    async def _run_bonus_round(self, brd: dict, source: str = "bonus") -> None:
        name = (brd.get("name") or "bonus round").strip()
        holders = self._bonus_holder_uids()
        if not holders:
            self._log("bot", f"BONUS ROUND '{name}' — nobody holds a bonus, skipped")
            miss = (brd.get("no_holders_message") or "").strip()
            if miss:
                await self._announce(self.render(miss), None)
            self._bonus_round = None
            return
        confirm = (brd.get("confirm") or "all").lower()
        if confirm == "leader":
            top = self._top_bonus_holder()
            needed = {top} if top else set()
        else:
            needed = set(holders)
        try:
            duration = max(5.0, min(3600.0, float(brd.get("duration") or 60)))
        except (TypeError, ValueError):
            duration = 60.0
        self._bonus_round = {"def": brd, "name": name, "holders": set(holders),
                             "needed": set(needed), "pressed": set(), "done": False}
        title = "🤝 " + self.render((brd.get("title") or name).strip())
        body = self.render((brd.get("body") or "").strip()) or self._bonus_round_intro(confirm, needed)
        meta = {"name": name, "holders": [self._name_for(u) or "?" for u in holders]}
        self._log("bot", f"BONUS ROUND '{name}' started [{source}] — need {len(needed)}/{len(holders)}")
        if self.bonus_round_cb:
            try:
                await self.bonus_round_cb(title, f"{body}\n\n{self._bonus_round_status()}", meta)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"bonus round embed failed: {e}")
                await self._announce(f"**{title}**\n{body}", None)
        else:
            await self._announce(f"**{title}**\n{body}", None)
        try:
            end = time.monotonic() + duration
            while self._bonus_round is not None and not self._bonus_round["done"]:
                if self._bonus_round["needed"] <= self._bonus_round["pressed"]:
                    await self._activate_bonus_round()
                    return
                if time.monotonic() >= end:
                    break
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            self._bonus_round = None
            raise
        # expired without full confirmation
        if self._bonus_round is not None and not self._bonus_round["done"]:
            self._bonus_round = None
            fmsg = (brd.get("expire_message") or "").strip()
            if fmsg:
                await self._announce(self.render(fmsg), None)
            self._log("bot", f"BONUS ROUND '{name}' expired without confirmation")

    def _bonus_round_intro(self, confirm: str, needed: set) -> str:
        if confirm == "leader":
            return "The top bonus holder can press to cash in everyone's bonus!"
        return f"All {len(needed)} bonus holders must press before time runs out to cash in!"

    def _bonus_round_status(self) -> str:
        br = self._bonus_round
        if not br:
            return ""
        done = [self._name_for(u) or "?" for u in br["pressed"] if u in br["needed"]]
        line = f"✅ Confirmed **{len(br['pressed'] & br['needed'])}/{len(br['needed'])}**"
        if done:
            line += " — " + ", ".join(done)
        return line

    def bonus_round_can_press(self, uid) -> bool:
        br = self._bonus_round
        return bool(br and not br["done"] and str(uid) in br["holders"])

    async def bonus_round_press(self, uid, who: str) -> dict:
        """A bonus holder confirms. Returns status; activates when the needed set
        is satisfied. Non-holders are rejected (they've nothing to contribute)."""
        br = self._bonus_round
        if not br or br["done"]:
            return {"ok": False, "error": "no round"}
        if str(uid) not in br["holders"]:
            return {"ok": False, "error": "not a holder"}
        br["pressed"].add(str(uid))
        status = self._bonus_round_status()
        if br["needed"] <= br["pressed"]:
            await self._activate_bonus_round()
            return {"ok": True, "activated": True, "status": status}
        return {"ok": True, "activated": False, "status": status,
                "have": len(br["pressed"] & br["needed"]), "need": len(br["needed"])}

    async def _activate_bonus_round(self) -> None:
        br = self._bonus_round
        if not br or br["done"]:
            return
        br["done"] = True
        brd = br["def"]
        name = br["name"]
        secs, pct = self.total_bonus()   # pooled totals BEFORE clearing
        self._bonus_round = None
        self._log("bot", f"BONUS ROUND '{name}' ACTIVATED — pooled {secs:g}s / {pct:g}%")
        xc = {"total_bonus_secs": f"{secs:g}", "total_bonus_pct": f"{pct:g}"}
        # spent & cleared: the block sees the pooled totals, then banks reset
        self._bonus_bank.clear()
        await self._run_action_block(brd.get("actions"), f"bonus round {name}",
                                     hdr="BONUS ROUND", extra_ctx=xc)

    def _cancel_bonus_round(self) -> None:
        if self._bonus_round_task is not None:
            self._bonus_round_task.cancel()
            self._bonus_round_task = None
        self._bonus_round = None

    def _clear_winner_grants(self, all_incl_stash: bool = False) -> None:
        """Range change clears range-locked grants; session reset clears all."""
        if all_incl_stash:
            self._winner_grants.clear()
            self._grant_charges.clear()
            self._progression_lock = None
            self._cancel_deadline()
            return
        for k in [k for k in self._winner_grants if not k.endswith("\x00stash")]:
            if (k + "\x00stash") not in self._winner_grants:   # not stashable → range-scoped
                self._winner_grants.pop(k, None)
                self._grant_charges.pop(k, None)
        # a cleared progression-lock command means the lock lifts too
        if self._progression_lock and self._progression_lock["cmd"] not in self._winner_grants:
            self._progression_lock = None
        # a cleared grant cancels its pending idle deadline
        if self._bonus_pending and self._bonus_pending["cmd"] not in self._winner_grants:
            self._cancel_deadline()

    # -- timed events -------------------------------------------------------- #
    async def _check_events(self) -> None:
        """Fire each enabled event when its interval elapses. Events only run
        while the listener is enabled (so they don't fire on a paused/off bot)."""
        if not self.cfg.get("listener_enabled"):
            return
        if self._capev_effect("pause_events"):
            return   # a capacity event has paused all timed events
        now = time.monotonic()
        for ev in self.cfg.get("events", []):
            name = (ev.get("name") or "").strip()
            key = name.lower()
            # Effective on = saved tick OR temporarily started by a command.
            if not (ev.get("enabled") or key in self._runtime_events_on):
                continue
            try:
                every = float(ev.get("every") or 0)
            except (TypeError, ValueError):
                every = 0
            if not name or every <= 0:
                continue
            if key in self._events_done:
                continue  # a "once" event / repeat-capped loop that already finished
            last = self._event_last.get(key)
            if last is not None and now < last + every:
                continue
            self._event_last[key] = now
            if last is None and not ev.get("fire_immediately"):
                continue  # first tick just arms the timer (delay before first fire)
            await self._fire_event_once(ev, key)

    async def _emit(self, text: str, sink: list | None = None, replace_key: str | None = None) -> None:
        """Append to `sink` (in-order, for the immediate first round) or broadcast.
        `replace_key` (loops with clean_previous) tags the post so the transport can
        delete the prior round's message before showing the new one."""
        if sink is not None:
            sink.append({"text": text, "replace_key": replace_key} if replace_key else text)
        else:
            await self._announce(text, None, replace_key=replace_key)

    async def _fire_event_once(self, ev: dict, key: str, sink: list | None = None) -> None:
        """Run one iteration of an event: bump the loop count, fire it, and on the
        final round drop the activator, start its cooldown and post the end message.
        With `sink` set, messages go into that list (used for the immediate first
        round so it follows the command reply) instead of broadcasting now."""
        if self._capev_effect("pause_events"):
            return   # covers the fire_immediately path too
        name = (ev.get("name") or "").strip()
        one_shot = (ev.get("mode") or "loop").lower() == "once"
        try:
            every = float(ev.get("every") or 0)
        except (TypeError, ValueError):
            every = 0
        try:
            cap = int(ev.get("max_repeats") or 0)   # loop cap; 0/blank = unlimited
        except (TypeError, ValueError):
            cap = 0
        count = self._event_fires.get(key, 0) + 1
        self._event_fires[key] = count
        ending = one_shot or (cap > 0 and count >= cap)
        if ending:
            self._events_done.add(key)   # "once", or a loop that hit its repeat cap → stop
        # clean_previous (loops only): each round replaces the last round's message.
        # A per-run id keeps a fresh run from deleting the previous run's end message.
        replace_key = None
        if not one_shot and ev.get("clean_previous"):
            if count == 1:
                self._event_run_id[key] = self._event_run_id.get(key, 0) + 1
            replace_key = f"evloop:{key}:{self._event_run_id.get(key, 1)}"
        # [next_round] is a smart line: "Next Round in N seconds" except the last round.
        next_round = "" if ending else f"Next Round in {every:g} seconds"
        loop_ctx = {"current_loop": count, "total_loops": ("∞" if cap <= 0 else str(cap)),
                    "loop_timer": f"{every:g}", "event": name, "next_round": next_round}
        try:
            await self._run_event(ev, loop_ctx, sink=sink, replace_key=replace_key)
        except Exception as e:  # noqa: BLE001
            self._log("error", f"event {name} failed: {e}")
        if ending:
            # Loop finished: leave runtime-on, drop the activator, start its
            # cooldown, post the end message.
            self._runtime_events_on.discard(key)
            self._event_activator.pop(key, None)
            try:
                cdn = float(ev.get("cooldown") or 0)
            except (TypeError, ValueError):
                cdn = 0.0
            if cdn > 0:
                self._event_cooldown_until[key] = time.monotonic() + cdn
            em = (ev.get("end_message") or "").strip()
            if em:
                try:
                    # Same replace_key: the end message deletes the final round and
                    # stays put (the next run uses a new run id, so it's never wiped).
                    await self._emit(self.render(em, loop_ctx), sink, replace_key=replace_key)
                except Exception as e:  # noqa: BLE001
                    self._log("error", f"event {name} end msg failed: {e}")
            # The event has ended and cleared — queue its post-event commands
            # detached (they run on their own timeline, not part of the event).
            if not self._paused and not self._end_triggered:
                self._spawn_post_actions(ev.get("post_actions"), f"event {name} (post)", hdr="")

    async def _run_event(self, ev: dict, extra: dict | None = None, sink: list | None = None,
                         replace_key: str | None = None) -> None:
        name = ev.get("name", "event")
        action = (ev.get("action") or "message").lower()
        msg = (ev.get("message") or "").strip()
        # If a command started this event, credit that person's leaderboard for its pumps.
        act_uid, act_who = self._event_activator.get(name.strip().lower(), (None, ""))

        if action == "actions":
            # Full action block per round (message/embed/fire/award/competition/…),
            # exactly like a command's 'actions' type. Loop context ([current_loop],
            # [event], [next_round]) is available inside it. (clean_previous message
            # replacement stays a feature of the simple single-action types.)
            await self._run_action_block(ev.get("actions"), f"event {name}", hdr="",
                                         uid=act_uid, who=act_who, extra_ctx=extra)
            return

        if action == "chance":
            # Per-loop gamble: win fires `fires` + posts success_message; miss fires
            # `fail_fires` + posts failure_message (each falls back to `message`).
            try:
                chance = max(0.0, min(100.0, float(ev.get("chance") if ev.get("chance") is not None else 50)))
            except (TypeError, ValueError):
                chance = 50.0
            try:
                luck = float(ev.get("luck") or 0)
            except (TypeError, ValueError):
                luck = 0.0
            roll = random.randint(1, 100)
            win = roll <= chance
            if luck > 0 and random.random() * 100 < min(100.0, luck):
                win = True
            elif luck < 0 and random.random() * 100 < min(100.0, -luck):
                win = False
            fired = await self._run_fires(ev.get("fires") if win else ev.get("fail_fires"),
                                          act_who or f"event {name}", act_uid)
            total_secs = round(sum(f["duration"] for f in fired), 1)
            self._log("bot", f"event '{name}': chance rolled {roll} vs {chance:.0f}% → {'WIN' if win else 'miss'}")
            base = {"roll": roll, "chance": f"{chance:.0f}", "luck": f"{luck:.0f}", "won": win,
                    "secs": f"{total_secs:.1f}", "seconds": f"{total_secs:.1f}",
                    "secs2capacity": (self._secs_to_capacity(total_secs, self._active_id()) if fired else "0"),
                    **(extra or {})}
            tmpl = (ev.get("success_message") if win else ev.get("failure_message")) or msg
            if tmpl:
                await self._emit(self.render(tmpl, base), sink, replace_key=replace_key)
            return

        if action == "broadcast":
            bname = str(ev.get("broadcast") or "").strip().lower()
            row = next((b for b in self.cfg.get("broadcasts", [])
                        if (b.get("name") or "").strip().lower() == bname), None)
            if row is None:
                self._log("error", f"event '{name}': no broadcast named '{ev.get('broadcast')}'")
            elif (row.get("message") or "").strip():
                # broadcast actions post as a rich embed (not sink text)
                await self._post_broadcast(self.render(row["message"].strip(), extra))
            if msg:
                await self._emit(self.render(msg, extra), sink, replace_key=replace_key)
            return

        if action == "poll":
            # NON-BLOCKING: a timed event is a single action with nothing after
            # it, so start the poll in the background — awaiting it here would
            # freeze the capacity loop (accrual/milestones/other events) for the
            # poll's whole duration. (A poll inside a capacity-event ACTION
            # BLOCK still runs inline there, keeping that block's later actions
            # ordered after it — that block is itself a detached task, so the
            # loop keeps ticking.)
            res = self.start_poll_bg(ev.get("poll"), source=f"event {name}")
            if not res.get("ok"):
                self._log("error", f"event '{name}': {res.get('error')}")
            if msg:
                await self._emit(self.render(msg, extra), sink, replace_key=replace_key)
            return

        if action == "competition":
            res = self.start_competition_bg(ev.get("competition"), source=f"event {name}")
            if not res.get("ok"):
                self._log("error", f"event '{name}': {res.get('error')}")
            if msg:
                await self._emit(self.render(msg, extra), sink, replace_key=replace_key)
            return

        if action == "capacity":
            op = (ev.get("capacity_op") or "add").lower()
            try:
                val = float(ev.get("capacity_value") or 0)
            except (TypeError, ValueError):
                val = 0
            self.set_capacity(val if op == "set" else self.capacity + val)
            self._log("bot", f"event '{name}': capacity {op} {val} → {self.capacity:.1f}%")
        elif action in ("fire", "roll"):
            target = ev.get("device_id") or self._active_id()
            if self._device(target) is None:
                self._log("error", f"event '{name}': no target device")
            elif action == "roll":
                rd, rs = self.range_dice(self.range_for(self.capacity))
                dice = int(ev.get("dice") or rd)
                sides = int(ev.get("sides") or rs)
                total, _ = self._roll_total(dice, sides)
                dur = self._duration_from_total(total)
                fr = self._begin_or_extend(target, dur, f"event {name}")
                if fr.get("status") in ("started", "extended"):
                    self.credit_pump(act_uid, act_who, fr.get("added", dur), target)
                self._log("bot", f"event '{name}': rolled {dice}d{sides}={total}")
            else:
                secs = round(max(0.1, min(self._hard_cap(), float(ev.get("seconds") or 3))), 1)
                fr = self._begin_or_extend(target, secs, f"event {name}")
                if fr.get("status") in ("started", "extended"):
                    self.credit_pump(act_uid, act_who, fr.get("added", secs), target)
                self._log("bot", f"event '{name}': fired {secs}s")
        else:
            self._log("bot", f"event '{name}': message")

        if msg:
            await self._emit(self.render(msg, extra), sink, replace_key=replace_key)

    def _cooldown_reply(self, who: str, remaining: float, uid=None, cmdkey: str = "") -> str:
        tmpl = self.cfg.get("cooldown_message") or "⏳ [mention], [cmd] is on cooldown — [cooldown]s left"
        return self.render(tmpl, {"user": who, "mention": self._mention(uid, who),
                                  "cooldown": f"{remaining:.0f}", "cmd": self._cmd_display(cmdkey)})

    # -- custom commands ----------------------------------------------------- #
    def builtin_names(self) -> dict:
        n = self.cfg.get("command_names", {})
        return {"roll": (n.get("roll") or "agroll").strip().lower(),
                "capacity": (n.get("capacity") or "capacity").strip().lower(),
                "help": (n.get("help") or "aghelp").strip().lower(),
                "leaderboard": (n.get("leaderboard") or "leaderboard").strip().lower(),
                "leaderboard_life": (n.get("leaderboard_life") or "toppumpers-life").strip().lower(),
                "pumptimer": (n.get("pumptimer") or "pumptimer").strip().lower(),
                "vote": (n.get("vote") or "agvote").strip().lower(),
                "enter": (n.get("enter") or "enter").strip().lower()}

    # -- pump-time leaderboard (per session) --------------------------------- #
    def buffer_ok(self, cmdkey: str) -> bool:
        """Anti-spam gate: True (and records now) if this command hasn't fired
        within system_buffer_seconds; False (silently) if it's too soon."""
        try:
            b = float(self.cfg.get("system_buffer_seconds", 8))
        except (TypeError, ValueError):
            b = 0.0
        if b <= 0:
            return True
        now = time.monotonic()
        last = self._last_fired.get(cmdkey)
        if last is not None and now < last + b:
            return False
        self._last_fired[cmdkey] = now
        if len(self._last_fired) > 500:   # bound the per-user keys over long sessions
            cutoff = now - 3600
            self._last_fired = {k: t for k, t in self._last_fired.items() if t > cutoff}
        return True

    def credit_pump(self, uid, who: str, seconds: float, device_id) -> None:
        """Record a person's pump contribution (seconds + the % it adds)."""
        if uid is None:
            return
        dev = self._device(device_id)
        if not dev or (dev.get("type") or "pump") != "pump":
            return
        cal = dev.get("calibration_seconds_to_100")
        try:
            pct = seconds / float(cal) * 100.0 if cal else 0.0
        except (TypeError, ValueError):
            pct = 0.0
        r = self.range_for(self.capacity)
        rkey = (r.get("min"), r.get("max"))
        range_board = self._pump_range.setdefault(rkey, {})
        for board in (self._pump_time, self._pump_life, range_board):
            rec = board.setdefault(str(uid), {"name": who, "seconds": 0.0, "capacity": 0.0})
            rec["name"] = who
            rec["seconds"] += float(seconds)
            rec["capacity"] += pct
        self._save_lifetime()

    # -- leaderboards (session + lifetime) ----------------------------------- #
    def _lifetime_path(self) -> str:
        return os.path.join(config_store.DATA_DIR, "pumpers_lifetime.json")

    def _load_lifetime(self) -> dict:
        try:
            with open(self._lifetime_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save_lifetime(self) -> None:
        try:
            os.makedirs(config_store.DATA_DIR, exist_ok=True)
            tmp = self._lifetime_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._pump_life, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._lifetime_path())
        except OSError as e:  # noqa: BLE001
            self._log("error", f"couldn't save lifetime stats: {e}")

    def reset_lifetime(self) -> None:
        self._pump_life.clear()
        self._save_lifetime()
        self._log("bot", "LIFETIME leaderboard reset")

    def lifetime_board(self) -> dict:
        """The all-time leaderboard data (for config export)."""
        return dict(self._pump_life)

    def set_lifetime(self, data) -> None:
        """Replace the all-time leaderboard (config import/restore)."""
        if not isinstance(data, dict):
            return
        clean = {}
        for uid, rec in data.items():
            if not isinstance(rec, dict):
                continue
            try:
                clean[str(uid)] = {"name": str(rec.get("name") or "?"),
                                   "seconds": float(rec.get("seconds") or 0),
                                   "capacity": float(rec.get("capacity") or 0)}
            except (TypeError, ValueError):
                continue
        self._pump_life = clean
        self._save_lifetime()
        self._log("bot", f"LIFETIME leaderboard restored ({len(clean)} pumpers)")

    @staticmethod
    def _format_board(rows, header: str, empty: str) -> str:
        # Skip malformed rows instead of raising: a hand-edited lifetime file
        # must never make every command reply silently fail to render.
        clean = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                clean.append({"name": str(r.get("name") or "?"),
                              "seconds": float(r.get("seconds") or 0),
                              "capacity": float(r.get("capacity") or 0)})
            except (TypeError, ValueError):
                continue
        rows = sorted(clean, key=lambda r: r["seconds"], reverse=True)
        if not rows:
            return f"{header}\n{empty}"
        namew = max(4, min(20, max(len(r["name"]) for r in rows)))
        lines = [header, "```", f"{'NAME'.ljust(namew)}   {'SECONDS':>9}   {'CAPACITY':>9}"]
        for r in rows:
            nm = (r["name"] or "?")[:namew].ljust(namew)
            lines.append(f"{nm}   {r['seconds']:>8.1f}s   {r['capacity']:>8.1f}%")
        lines.append("```")
        return "\n".join(lines)

    def leaderboard_text(self) -> str:
        return self._format_board(self._pump_time.values(), "***TOP PUMPERS — THIS SESSION***",
                                  "Nobody has pumped yet this session.")

    def leaderboard_life_text(self) -> str:
        return self._format_board(self._pump_life.values(), "***ALL-TIME TOP PUMPERS***",
                                  "Nobody has pumped yet.")

    # -- per-range leaderboards (current capacity band) ---------------------- #
    def _range_board(self, key=None) -> dict:
        """The {uid: rec} pump board for the given range key, or the current
        range if none is given (empty dict if nobody's pumped it yet)."""
        if key is None:
            r = self.range_for(self.capacity)
            key = (r.get("min"), r.get("max"))
        return self._pump_range.get(key, {})

    @staticmethod
    def _board_leader(board: dict):
        """(uid, name, capacity%) of the #1 pumper on a board (ranked by seconds
        to match the board's #1), or (None, "", 0.0) when empty."""
        if not board:
            return (None, "", 0.0)
        uid, rec = max(board.items(), key=lambda kv: kv[1].get("seconds", 0.0))
        return (str(uid), rec.get("name") or "?", float(rec.get("capacity") or 0.0))

    def range_leader(self):
        """The top pumper in the CURRENT range (uid, name, capacity%)."""
        return self._board_leader(self._range_board())

    def session_leader(self):
        """The top pumper this SESSION overall (uid, name, capacity%) — for an
        end-of-session special prize."""
        return self._board_leader(self._pump_time)

    def range_board_text(self) -> str:
        return self._format_board(self._range_board().values(),
                                  f"***TOP PUMPERS — {self._range_label()}***",
                                  "Nobody has pumped this range yet.")

    def _name_for(self, uid) -> str:
        """A user's display name from any board we've seen them on, or ""."""
        u = str(uid)
        for board in (self._users, self._pump_time, self._pump_life):
            rec = board.get(u)
            if rec and rec.get("name"):
                return rec["name"]
        return ""

    def _range_prizes_ok(self) -> bool:
        """False when the current range has 'Allow Prize Commands' turned off — a
        granted bonus command can be HELD but not USED until an allowed range.
        A 'Only prize commands' range always allows them (that's the point)."""
        r = self.range_for(self.capacity)
        if r.get("prizes_only"):
            return True
        return r.get("allow_prizes", True) is not False

    def _range_prizes_only(self) -> bool:
        """True when the current range blocks ALL custom commands and the builtin
        roll EXCEPT granted bonus commands (per-range 'Only')."""
        return bool(self.range_for(self.capacity).get("prizes_only"))

    def bonus_holders_text(self) -> str:
        """The [bonus_holders] roster: each holder, the bonus command(s) they
        hold, what each does, and how many charges remain (best in an embed)."""
        prefix = self.cfg.get("command_prefix", "!")
        by_holder: dict[str, list] = {}
        for k, uid in self._winner_grants.items():
            if k.endswith("\x00stash"):
                continue
            by_holder.setdefault(str(uid), []).append(k)
        if not by_holder:
            return "Nobody holds a bonus command right now."
        out = []
        for uid, keys in by_holder.items():
            out.append(f"**{self._name_for(uid) or '?'}**")
            for k in keys:
                desc = ((self.find_command(k) or {}).get("description") or "").strip()
                ch = self._grant_charges.get(k)
                chs = "∞" if ch is None else str(ch)
                line = f"• {prefix}{k}" + (f" — {desc}" if desc else "")
                out.append(line + f" · {chs} charge" + ("" if ch == 1 else "s"))
        return "\n".join(out)

    # -- bonus amounts (award_amount → Bonus Round) -------------------------- #
    def total_bonus(self):
        """(secs, pct) pooled across every player's bank."""
        secs = sum(float(r.get("secs") or 0) for r in self._bonus_bank.values())
        pct = sum(float(r.get("pct") or 0) for r in self._bonus_bank.values())
        return (secs, pct)

    def user_bonus(self, uid):
        """(secs, pct) banked by one player."""
        r = self._bonus_bank.get(str(uid)) or {}
        return (float(r.get("secs") or 0), float(r.get("pct") or 0))

    def _bonus_holder_uids(self) -> list:
        """uids that currently hold any bonus (secs or pct)."""
        return [u for u, r in self._bonus_bank.items()
                if (r.get("secs") or 0) > 0 or (r.get("pct") or 0) > 0]

    def _top_bonus_holder(self):
        """uid of the biggest bonus holder (secs + pct), or None."""
        holders = self._bonus_holder_uids()
        if not holders:
            return None
        return max(holders, key=lambda u: (self._bonus_bank[u].get("secs") or 0)
                   + (self._bonus_bank[u].get("pct") or 0))

    def _resolve_award_target(self, target: str):
        """Resolve an award_prize/winner_button target string to (uid, name).
        Supports [range_leader] (current range's top pumper), [session_leader]
        (the whole session's top pumper), [winner] (most recent competition
        winner), a raw <@id>/numeric mention, or a known pumper's name. Returns
        (None, "") if unresolvable."""
        t = (target or "").strip()
        if not t:
            return (None, "")
        low = t.lower()
        if "[range_leader]" in low or low in ("range_leader", "range leader"):
            uid, nm, _ = self.range_leader()
            return (uid, nm) if uid else (None, "")
        if "[session_leader]" in low or low in ("session_leader", "session leader"):
            uid, nm, _ = self.session_leader()
            return (uid, nm) if uid else (None, "")
        if "[top_bonus_holder]" in low or low in ("top_bonus_holder", "top bonus holder"):
            uid = self._top_bonus_holder()
            return (str(uid), self._name_for(uid) or "?") if uid else (None, "")
        if "[runnerup]" in low or low == "runnerup":
            rname = (self._last_runnerup or "").strip().lower()
            if rname:
                for board in (self._pump_time, self._users):
                    for uid, rec in board.items():
                        if (rec.get("name") or "").strip().lower() == rname:
                            return (str(uid), rec.get("name"))
                return (None, self._last_runnerup)
            return (None, "")
        if "[winner]" in low or low == "winner":
            # match the last competition winner's name to a known uid
            wname = (self._last_winner or "").strip().lower()
            if wname:
                for board in (self._pump_time, self._users):
                    for uid, rec in board.items():
                        if (rec.get("name") or "").strip().lower() == wname:
                            return (str(uid), rec.get("name"))
                return (None, self._last_winner)   # name known, uid not → no grantable target
            return (None, "")
        m = re.search(r"\d{5,}", t)   # raw <@123>/<@!123>/bare id
        if m:
            uid = m.group(0)
            for board in (self._users, self._pump_time):
                if uid in board:
                    return (uid, board[uid].get("name") or t)
            return (uid, t)
        for board in (self._pump_time, self._users):   # match a known name
            for uid, rec in board.items():
                if (rec.get("name") or "").strip().lower() == low:
                    return (str(uid), rec.get("name"))
        return (None, "")

    def help_text(self, prefix: str) -> str:
        return "Available commands:\n" + self._commands_str(prefix)

    def _range_gate_ok(self, gate) -> bool:
        """True if the current capacity is inside the command's gated range.
        gate is 'all'/'' (always ok) or a 'min-max' string."""
        g = (gate or "all")
        if g == "all" or not str(g).strip():
            return True
        try:
            lo, hi = str(g).split("-")
            return float(lo) <= self.capacity <= float(hi)
        except (ValueError, TypeError):
            return True

    def find_command(self, name: str) -> dict | None:
        name = (name or "").lower()
        if name in set(self.builtin_names().values()):
            return None
        for c in self.cfg.get("commands", []):
            if (c.get("name") or "").strip().lower() == name:
                return c
        return None

    def _paused_result(self, who: str, uid) -> dict:
        """The quiet 'session is paused' reply for a command that arrived while
        paused. One notice per user per buffer window; repeats are silent. Never
        consumes a use or touches a cooldown."""
        if not self.buffer_ok(f"pausednote:{uid if uid is not None else who}"):
            return {"ok": False, "paused": True, "silent": True}
        tmpl = self.cfg.get("paused_notice_message") or "⏸️ [mention], the session is paused — hang tight until the operator resumes."
        return {"ok": False, "paused": True,
                "error": self.render(tmpl, {"user": who, "mention": self._mention(uid, who)})}

    async def roll_and_fire(self, who: str, uid: str | None = None,
                            dice: int | None = None, sides: int | None = None) -> dict:
        if self._paused:
            return self._paused_result(who, uid)
        if self._progression_lock is not None:
            return {"ok": False, "silent": True}  # frozen until the competition winner acts
        # command_gate: a specific block on 'roll', or a block_all / 'Only prize
        # commands' band that 'roll' isn't allow-listed through. Owner exempt.
        if uid is not None and not self._is_exempt(uid, who):
            if "roll" in self._cmd_gate_blocked_cmds:
                return {"ok": False, "silent": True}
            if (self._cmd_gate_block or self._range_prizes_only()) and "roll" not in self._cmd_gate_allow:
                return {"ok": False, "silent": True}
        target = self._active_id()
        if self._device(target) is None:
            return {"ok": False, "error": "no active device selected"}
        if not self.cmd_enabled_in_range("roll"):
            return {"ok": False, "silent": True}  # dice disabled in this range
        cd, scope = self._range_cd_scope(self.range_for(self.capacity), "roll")
        key_uid = str(uid) if scope == "user" else "*"
        exempt = uid is not None and self._is_exempt(uid, who)
        if uid is not None and not exempt:
            remaining = self.cooldown_remaining(key_uid, "roll")
            if remaining > 0:
                return {"ok": False, "cooldown": True, "error": self._cooldown_reply(who, remaining, uid, "roll")}
        if self.cfg.get("roll", {}).get("disable_at_100") and self.capacity >= self._capacity_cap():
            return {"ok": False, "error": "at 100% capacity — control disabled"}
        if uid is not None:
            self._track_user(uid, who)
            if scope == "command" or not exempt:
                self._touch_cooldown(key_uid, "roll", cd)

        p = self.preview_roll(dice, sides)
        fr = self._begin_or_extend(target, p["duration"], f"roll by {who}")
        extended = fr["status"] == "extended"
        # Credit the seconds actually delivered (a stacked roll near the hard
        # cap may add less than rolled) — keeps the leaderboards honest.
        self.credit_pump(uid, who, fr.get("added", p["duration"]), target)
        self._log("roll", f"{who} rolled {p['dice']}d{p['sides']} = {p['total']} "
                  f"({'+'.join(map(str, p['rolls']))}) → {p['duration']:.1f}s"
                  + (f"  [+{fr['added']:.1f}s → {fr['remaining']:.1f}s]" if extended else ""))

        dice_notation = f"{p['dice']}d{p['sides']}"
        default = "🎲 **[user]** rolled **[dice]** = **[result]** → **[secs]s** · capacity [capacity]%"
        base = {"dice": dice_notation, "result": p["total"], "total": p["total"],
                "secs": f"{p['duration']:.1f}", "seconds": f"{p['duration']:.1f}", "sides": p["sides"],
                "secs2capacity": self._secs_to_capacity(p["duration"], target),
                "timer": f"{fr.get('remaining', p['duration']):.1f}",
                "total_seconds": f"{fr.get('remaining', p['duration']):.1f}",
                "total_secs": f"{fr.get('remaining', p['duration']):.1f}"}
        tmpl = self.cfg.get("roll", {}).get("reply") or default
        anon = self._anon_label()
        reply = self.render(tmpl, {"user": who, "mention": self._mention(uid, who), **base})
        reply_anon = self.render(tmpl, {"user": anon, "mention": anon, **base})

        # Max Roll Prize: a "perfect" roll is the maximum possible total.
        if uid is not None and p["total"] == p["dice"] * p["sides"]:
            pt, pa = self._prize_progress({"uid": uid}, who, self._mention(uid, who))
            if pt:
                reply += "\n" + pt
                reply_anon += "\n" + pa

        return {"ok": True, "started": fr["status"] in ("started", "extended"),
                "extended": extended, "added": fr.get("added"), "remaining": fr.get("remaining"),
                **p, "reply": reply, "reply_anon": reply_anon,
                "announce": self.render(p["announce"]) if p.get("announce") else ""}

    async def run_custom(self, cmd: dict, who: str, uid: str | None) -> dict:
        typ = (cmd.get("type") or "fire").lower()
        name = cmd.get("name", "command")

        if self._paused:
            return self._paused_result(who, uid)

        cmdkey0 = name.lower()
        # Competition entry: while a competition is live, its entry command
        # scores into the contest (cooldown bypassed, doesn't fire) instead of
        # running normally.
        if uid is not None and self._comp is not None and self._comp["cmd"] == cmdkey0:
            return await self.competition_entry(cmd, who, uid)
        # Winner-only grant: a command locked to a competition winner is silent
        # for everyone else while the grant stands; the winner may run it even
        # if it isn't a member of the current range.
        winner_granted = False
        if uid is not None and cmdkey0 in self._winner_grants and not cmdkey0.endswith("\x00stash"):
            if str(uid) != self._winner_grants.get(cmdkey0):
                return {"ok": False, "silent": True}
            # per-range 'Allow Prize Commands': a held grant can't be USED until
            # a range that allows it (charge not spent, lock not lifted here).
            if not self._range_prizes_ok():
                return {"ok": False, "silent": True}
            winner_granted = True
            # the winner used it in time → cancel the idle deadline
            if self._bonus_pending and self._bonus_pending.get("cmd") == cmdkey0:
                self._bonus_pending["used"] = True
                self._cancel_deadline()
            # the winner using their granted command lifts the progression lock
            if self._progression_lock and self._progression_lock.get("cmd") == cmdkey0:
                self._progression_lock = None
                self._log("bot", "progression lock lifted — winner used their command")
            # award_prize charges: each accepted use spends one; when they run
            # out the grant (and its stash marker) is revoked so it re-locks.
            if cmdkey0 in self._grant_charges:
                self._grant_charges[cmdkey0] -= 1
                if self._grant_charges[cmdkey0] <= 0:
                    self._grant_charges.pop(cmdkey0, None)
                    self._winner_grants.pop(cmdkey0, None)
                    self._winner_grants.pop(cmdkey0 + "\x00stash", None)
        capev_enabled = cmdkey0 in self._capev_enabled_cmds() or winner_granted
        # command_gate: a specific block on this command, or a block_all / 'Only
        # prize commands' band it isn't allow-listed through. Owner + granted
        # commands (incl. a Winner Button target) pass.
        if uid is not None and not winner_granted and not self._is_exempt(uid, who):
            if cmdkey0 in self._cmd_gate_blocked_cmds:
                return {"ok": False, "silent": True}
            if (self._cmd_gate_block or self._range_prizes_only()) and cmdkey0 not in self._cmd_gate_allow:
                return {"ok": False, "silent": True}
        # Capacity-event gate (chat only — uid is None for range-start/system
        # calls). An event's enable-list wins over its own disables AND over
        # range membership; normal ranges yield to capacity events here. A
        # granted command (competition/award prize) overrides even a capev block.
        if uid is not None and not winner_granted and self._capev_cmd_blocked(cmdkey0):
            return {"ok": False, "silent": True}
        if not self.cmd_enabled_in_range(cmdkey0) and not capev_enabled:
            return {"ok": False, "silent": True}  # not a member of the current range → ignore quietly
        if not self._range_gate_ok(cmd.get("range_gate")) and not capev_enabled:
            return {"ok": False, "silent": True}  # gated to a different capacity band
        # progression lock: while a competition winner is expected to advance the
        # range, everyone else's capacity-affecting commands are frozen (the
        # winner's granted command already lifted the lock above).
        if (self._progression_lock is not None and not winner_granted
                and typ in ("fire", "roll", "chance")):
            return {"ok": False, "silent": True}
        # The owner is never limited by cooldowns or per-person max-uses (they're
        # still subject to the in-progress event guard). A granted command runs
        # under the same exemption so a prize's charges are the ONLY limit — e.g.
        # 3 back-to-back rolls with the cooldown bypassed.
        owner_exempt = (uid is not None and self._is_exempt(uid, who)) or winner_granted
        # Per-person session use budget (carries across ranges, never replenishes).
        cmdkey_uses = name.lower()
        left = self.cmd_uses_left(uid, cmdkey_uses) if (uid is not None and not owner_exempt) else None
        if left is not None and left <= 0:
            msg = f"🚫 {self._mention(uid, who)}, you're all out of uses for {self._cmd_display(cmdkey_uses)} this session."
            return {"ok": False, "used_up": True, "error": msg}

        def _spend_use():
            if uid is not None and not owner_exempt and self.cmd_max_uses(cmdkey_uses) is not None:
                d = self._cmd_uses.setdefault(str(uid), {})
                d[cmdkey_uses] = d.get(cmdkey_uses, 0) + 1

        def _remain():
            # None = the command has no per-person limit → show the word "unlimited".
            if owner_exempt:
                return "unlimited"
            r = self.cmd_uses_left(uid, cmdkey_uses)
            return "unlimited" if r is None else r

        if typ == "say":
            # Say commands respect cooldowns like every other type (the shipped
            # !pumproulette's 300s cooldown used to be decorative).
            cmdkey = name.lower()
            cd, scope = self._range_cd_scope(self.range_for(self.capacity), cmdkey)
            key_uid = str(uid) if scope == "user" else "*"
            if uid is not None and not owner_exempt:
                remaining = self.cooldown_remaining(key_uid, cmdkey)
                if remaining > 0:
                    return {"ok": False, "cooldown": True, "error": self._cooldown_reply(who, remaining, uid, cmdkey)}
            ev_posts, activated = await self.start_events(cmd.get("start_events"), uid, who)
            # A pure event-trigger command (e.g. !pumproulette) only costs a use
            # (or touches its cooldown) when it actually starts something. If
            # every event it targets was blocked (already running / on
            # cooldown), don't drain the user's budget.
            if not (cmd.get("start_events") and activated == 0):
                _spend_use()
                if uid is not None:
                    self._track_user(uid, who)
                    if scope == "command" or not owner_exempt:
                        self._touch_cooldown(key_uid, cmdkey, cd)
            anon = self._anon_label()
            return {"ok": True, "device": False, "started": False, "events_posted": ev_posts,
                    "reply": self.render(cmd.get("reply") or "", {"user": who, "mention": self._mention(uid, who), "cmd_remain": _remain()}) or f"{name}!",
                    "reply_anon": self.render(cmd.get("reply") or "", {"user": anon, "mention": anon, "cmd_remain": _remain()}) or f"{name}!"}

        if typ == "actions":
            # A full action SEQUENCE (like an event): message / broadcast /
            # fire / roll / capacity / wait / poll / competition / end_session,
            # in order. Same gating as any command; the block's fires credit
            # the runner. start_events still supported alongside.
            cmdkey = name.lower()
            cd, scope = self._range_cd_scope(self.range_for(self.capacity), cmdkey)
            key_uid = str(uid) if scope == "user" else "*"
            exempt = uid is not None and self._is_exempt(uid, who)
            if uid is not None and not exempt:
                remaining = self.cooldown_remaining(key_uid, cmdkey)
                if remaining > 0:
                    return {"ok": False, "cooldown": True, "error": self._cooldown_reply(who, remaining, uid, cmdkey)}
            if uid is not None:
                self._track_user(uid, who)
                if scope == "command" or not exempt:
                    self._touch_cooldown(key_uid, cmdkey, cd)
            _spend_use()
            ev_posts, _ = await self.start_events(cmd.get("start_events"), uid, who)
            await self._run_action_block(cmd.get("actions"), f"{name} by {who}", hdr=name, uid=uid, who=who)
            anon = self._anon_label()
            tmpl = cmd.get("reply") or ""
            return {"ok": True, "device": True, "started": True, "events_posted": ev_posts,
                    "reply": self.render(tmpl, {"user": who, "mention": self._mention(uid, who), "cmd_remain": _remain()}),
                    "reply_anon": self.render(tmpl, {"user": anon, "mention": anon, "cmd_remain": _remain()})}

        if typ == "poll":
            # Starts a named poll (same gating as other commands; the poll
            # itself runs in the background — its embed posts separately).
            cmdkey = name.lower()
            cd, scope = self._range_cd_scope(self.range_for(self.capacity), cmdkey)
            key_uid = str(uid) if scope == "user" else "*"
            if uid is not None and not owner_exempt:
                remaining = self.cooldown_remaining(key_uid, cmdkey)
                if remaining > 0:
                    return {"ok": False, "cooldown": True, "error": self._cooldown_reply(who, remaining, uid, cmdkey)}
            res = self.start_poll_bg(cmd.get("poll"), source=f"command {name}")
            if not res.get("ok"):
                return {"ok": False, "error": res.get("error")}
            if uid is not None:
                self._track_user(uid, who)
                if scope == "command" or not owner_exempt:
                    self._touch_cooldown(key_uid, cmdkey, cd)
            _spend_use()
            anon = self._anon_label()
            tmpl = cmd.get("reply") or ""
            return {"ok": True, "device": False, "started": False,
                    "reply": self.render(tmpl, {"user": who, "mention": self._mention(uid, who), "cmd_remain": _remain()}),
                    "reply_anon": self.render(tmpl, {"user": anon, "mention": anon, "cmd_remain": _remain()})}

        if typ.startswith("game-"):
            # Minigames run interactively via Discord buttons — here we only gate
            # them (membership already checked; now cooldown + per-person budget)
            # and hand a signal back so the bot posts the public "Play" button.
            cmdkey = name.lower()
            cd, scope = self._range_cd_scope(self.range_for(self.capacity), cmdkey)
            key_uid = str(uid) if scope == "user" else "*"
            if uid is not None and not owner_exempt:
                remaining = self.cooldown_remaining(key_uid, cmdkey)
                if remaining > 0:
                    return {"ok": False, "cooldown": True, "error": self._cooldown_reply(who, remaining, uid, cmdkey)}
            if uid is not None:
                self._track_user(uid, who)
                if scope == "command" or not owner_exempt:
                    self._touch_cooldown(key_uid, cmdkey, cd)
            _spend_use()
            intro = self.render(cmd.get("game_intro") or "",
                                {"user": who, "mention": self._mention(uid, who), "cmd_remain": _remain()})
            return {"ok": True, "game": True, "game_type": typ, "reply": intro}

        if typ == "chance":
            # A gamble: roll 1–100 vs the chance%. Win → post success + fire the
            # optional device rows; miss → post failure, no fire. Same cooldown /
            # scope / per-person budget / membership as any custom command.
            cmdkey = name.lower()
            cd, scope = self._range_cd_scope(self.range_for(self.capacity), cmdkey)
            key_uid = str(uid) if scope == "user" else "*"
            exempt = uid is not None and self._is_exempt(uid, who)
            if uid is not None and not exempt:
                remaining = self.cooldown_remaining(key_uid, cmdkey)
                if remaining > 0:
                    return {"ok": False, "cooldown": True, "error": self._cooldown_reply(who, remaining, uid, cmdkey)}
            if uid is not None:
                self._track_user(uid, who)
                if scope == "command" or not exempt:
                    self._touch_cooldown(key_uid, cmdkey, cd)
            try:
                chance = max(0.0, min(100.0, float(cmd.get("chance") if cmd.get("chance") is not None else 50)))
            except (TypeError, ValueError):
                chance = 50.0
            try:
                luck = float(cmd.get("luck") or 0)
            except (TypeError, ValueError):
                luck = 0.0
            roll = random.randint(1, 100)
            win = roll <= chance
            # Luck: positive % = chance to force a win; negative % = chance to force a loss.
            if luck > 0 and random.random() * 100 < min(100.0, luck):
                win = True
            elif luck < 0 and random.random() * 100 < min(100.0, -luck):
                win = False
            _spend_use()   # an attempt costs a use whether you win or lose
            # Win fires the `fires` rows; a miss fires the `fail_fires` rows (if any).
            fired = await self._run_fires(cmd.get("fires") if win else cmd.get("fail_fires"), who, uid)
            ev_posts = (await self.start_events(cmd.get("start_events"), uid, who))[0] if win else []
            # win_actions / miss_actions — the full action block for this outcome
            # (fires, capacity, award_prize, embed, competition, …), credited to
            # the player. Runs after any legacy fire rows above.
            outcome_actions = cmd.get("win_actions") if win else cmd.get("miss_actions")
            if outcome_actions:
                await self._run_action_block(
                    outcome_actions, f"chance {name} ({'win' if win else 'miss'})", hdr="🎲",
                    uid=uid, who=who,
                    extra_ctx={"mention": self._mention(uid, who),
                               "roll": roll, "chance": f"{chance:.0f}"})
            total_secs = round(sum(f["duration"] for f in fired), 1)
            self._log("roll", f"{who} gambled {name}: rolled {roll} vs {chance:.0f}%"
                      + (f" (luck {luck:+.0f})" if luck else "")
                      + f" → {'WIN' if win else 'miss'}"
                      + (f", fired {len(fired)} device(s) {total_secs}s" if fired else ""))
            base = {"roll": roll, "chance": f"{chance:.0f}", "luck": f"{luck:.0f}", "won": win,
                    "secs": f"{total_secs:.1f}", "seconds": f"{total_secs:.1f}",
                    "secs2capacity": (self._secs_to_capacity(total_secs, self._active_id()) if fired else "0"),
                    "cmd_remain": _remain()}
            dflt = f"🎲 **{{u}}** rolled {roll} vs {chance:.0f}% — " + ("**win!**" if win else "no luck.")
            tmpl = (cmd.get("success_reply") if win else cmd.get("failure_reply")) or ""
            anon = self._anon_label()
            reply = self.render(tmpl, {"user": who, "mention": self._mention(uid, who), **base}) or dflt.replace("{u}", who)
            reply_anon = self.render(tmpl, {"user": anon, "mention": anon, **base}) or dflt.replace("{u}", anon)
            return {"ok": True, "device": bool(fired), "started": bool(fired), "won": win,
                    "events_posted": ev_posts, "reply": reply, "reply_anon": reply_anon}

        target = cmd.get("device_id") or self._active_id()
        tdev = self._device(target)
        if tdev is None:
            return {"ok": False, "error": "target device not found"}
        cmdkey = name.lower()
        cd, scope = self._range_cd_scope(self.range_for(self.capacity), cmdkey)
        key_uid = str(uid) if scope == "user" else "*"
        exempt = uid is not None and self._is_exempt(uid, who)
        if uid is not None and not exempt:
            remaining = self.cooldown_remaining(key_uid, cmdkey)
            if remaining > 0:
                return {"ok": False, "cooldown": True, "error": self._cooldown_reply(who, remaining, uid, cmdkey)}
        if self.cfg.get("roll", {}).get("disable_at_100") and self.capacity >= self._capacity_cap():
            return {"ok": False, "error": "at 100% capacity — control disabled"}
        if uid is not None:
            self._track_user(uid, who)
            if scope == "command" or not exempt:
                self._touch_cooldown(key_uid, cmdkey, cd)

        total = dice = sides = None
        if typ == "roll":
            rd, rs = self.range_dice(self.range_for(self.capacity))
            dice = int(cmd.get("dice") or rd)
            sides = int(cmd.get("sides") or rs)
            total, rolls = self._roll_total(dice, sides)
            duration = self._duration_from_total(total)
            detail = f"rolled {dice}d{sides}={total} → {duration:.1f}s"
        else:  # "fire"
            hi = self._hard_cap()
            duration = round(max(0.1, min(hi, float(cmd.get("seconds") or 3))), 1)
            detail = f"{duration:.1f}s"

        ev_posts, _ = await self.start_events(cmd.get("start_events"), uid, who)
        # "fire until capacity %" (winner-only progression command): the fire
        # runs until capacity reaches fire_until, guaranteeing the range advances.
        until = None
        try:
            fu = cmd.get("fire_until")
            if fu not in (None, "", 0):
                until = max(1.0, min(999.0, float(fu)))
        except (TypeError, ValueError):
            until = None
        fr = self._begin_or_extend(target, duration, f"{name} by {who}", until_capacity=until)
        extended = fr["status"] == "extended"
        self.credit_pump(uid, who, fr.get("added", duration), target)
        # Extra "Add Device Fire" rows (fire type): each device runs independently.
        extra = await self._run_fires(cmd.get("fires"), who, uid) if typ == "fire" else []
        _spend_use()
        self._log("roll", f"{who} ran {name} on {tdev.get('label')} ({detail})"
                  + (f"  [+{fr['added']:.1f}s → {fr['remaining']:.1f}s]" if extended else "")
                  + (f"  +{len(extra)} extra device fire(s)" if extra else ""))
        dice_notation = f"{dice}d{sides}" if dice and sides else ""
        base = {"dice": dice_notation, "result": total, "total": total,
                "secs": f"{duration:.1f}", "seconds": f"{duration:.1f}", "sides": sides,
                "secs2capacity": self._secs_to_capacity(duration, target), "cmd_remain": _remain(),
                "timer": f"{fr.get('remaining', duration):.1f}",
                "total_seconds": f"{fr.get('remaining', duration):.1f}",
                "total_secs": f"{fr.get('remaining', duration):.1f}"}
        tmpl = cmd.get("reply") or ""
        anon = self._anon_label()
        reply = self.render(tmpl, {"user": who, "mention": self._mention(uid, who), **base}) or f"🔥 **{who}** ran **{name}** → {detail}"
        reply_anon = self.render(tmpl, {"user": anon, "mention": anon, **base}) or f"🔥 **{anon}** ran **{name}** → {detail}"
        if extended:
            tail = f"  ⏱️ +{fr['added']:.1f}s ({fr['remaining']:.1f}s running)"
            reply += tail; reply_anon += tail
        return {"ok": True, "device": True, "started": True, "extended": extended,
                "events_posted": ev_posts, "reply": reply, "reply_anon": reply_anon}

    # -- minigames (button/ephemeral games; the Views live in minigames.py) ---- #
    def pushluck_bust_pct(self, cmd: dict, pumps: int) -> float:
        """Bust chance for the next pump: starts at pl_bust_start, +pl_bust_step each pump."""
        try:
            start = float(cmd.get("pl_bust_start") if cmd.get("pl_bust_start") is not None else 15)
        except (TypeError, ValueError):
            start = 15.0
        try:
            step = float(cmd.get("pl_bust_step") if cmd.get("pl_bust_step") is not None else 12)
        except (TypeError, ValueError):
            step = 12.0
        return max(0.0, min(100.0, start + step * max(0, int(pumps))))

    def game_luck(self, cmd: dict) -> float:
        """Luck modifier for a minigame (a flat ± added to the final score). A range
        the command is a member of wins; otherwise its Always-On entry; else 0."""
        key = (cmd.get("name") or "").strip().lower()
        entry = (self.range_for(self.capacity).get("cooldowns") or {}).get(key)
        if entry is not None and entry.get("luck") not in (None, ""):
            try:
                return float(entry.get("luck"))
            except (TypeError, ValueError):
                pass
        ao = self._always_on_map().get(key)
        if ao and ao.get("luck") not in (None, ""):
            try:
                return float(ao.get("luck"))
            except (TypeError, ValueError):
                pass
        return 0.0

    def game_display_name(self, cmd: dict) -> str:
        """The full game name from the command type (e.g. 'Rock Paper Scissors'),
        falling back to the command's own name for non-game commands."""
        return GAME_DISPLAY_NAMES.get((cmd.get("type") or "").lower(), cmd.get("name", "game"))

    def slots_spin(self, cmd: dict):
        """Spin 3 reels. Returns (reels, score): 3-of-a-kind → 3, a pair → 1, else 0."""
        n = max(2, min(len(SLOT_SYMBOLS), int(cmd.get("sl_symbols") or 6)))
        syms = SLOT_SYMBOLS[:n]
        reels = [random.choice(syms) for _ in range(3)]
        if reels[0] == reels[1] == reels[2]:
            score = 3
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            score = 1
        else:
            score = 0
        return reels, score

    @staticmethod
    def _tier_match(score, op, val) -> bool:
        try:
            v = float(val)
        except (TypeError, ValueError):
            v = 0.0
        op = (op or ">=").strip()
        if op == ">":  return score > v
        if op == "<":  return score < v
        if op == "<=": return score <= v
        if op in ("=", "=="): return score == v
        if op == "!=": return score != v
        return score >= v   # default / ">="

    def game_tier_for(self, cmd: dict, score) -> dict:
        """The matching score→outcome tier with the highest value. Each tier is a
        comparison (op value) against the score; with all ops ">=" this is the
        classic 'highest threshold reached' behaviour."""
        best = None
        for t in (cmd.get("game_tiers") or []):
            try:
                v = float(t.get("min", 0))
            except (TypeError, ValueError):
                v = 0.0
            if self._tier_match(score, t.get("op"), v) and (best is None or v >= best[0]):
                best = (v, t)
        return best[1] if best else {}

    async def game_result(self, cmd: dict, score, who: str, uid) -> dict:
        """Fire the winning tier's devices (credited to the player) and build the
        public broadcast for a finished minigame. Always labels which game it was
        and returns both a real (named) and an anonymized version so the bot can
        respect cross-server anonymity (real name only where the player is a member)."""
        luck = self.game_luck(cmd)
        if luck:
            try:
                score = max(0, round(float(score) + luck))   # luck nudges the effective score
            except (TypeError, ValueError):
                pass
        tier = self.game_tier_for(cmd, score)
        fired = await self._run_fires(tier.get("fires"), who, uid)
        # Finishing a game activates its start_events, same as a say command
        # (the UI has always offered "Starts events" on game types).
        ev_posts, _ = await self.start_events(cmd.get("start_events"), uid, who)
        total = round(sum(f["duration"] for f in fired), 1)
        name = self.game_display_name(cmd)   # full game name (Rock Paper Scissors, …)
        base = {"score": score, "luck": f"{luck:+.0f}" if luck else "0",
                "secs": f"{total:.1f}", "seconds": f"{total:.1f}",
                "secs2capacity": (self._secs_to_capacity(total, self._active_id()) if fired else "0"),
                "game": name}
        header = f"🎮 **{name}**\n"   # every result says which game it was for
        tmpl = tier.get("message") or ""
        real = header + (self.render(tmpl, {"user": who, "mention": self._mention(uid, who), **base})
                         or f"**{who}** scored **{score}**.")
        anon = self._anon_label()
        anon_msg = header + (self.render(tmpl, {"user": anon, "mention": anon, **base})
                             or f"**{anon}** scored **{score}**.")
        self._log("roll", f"{who} finished {name} — score {score}" + (f", fired {total}s" if fired else ""))
        # The tier's optional ACTION BLOCK runs after the result is posted (the
        # bot calls run_actions), so a tier can do anything an event can.
        return {"real": real, "anon": anon_msg, "events_posted": ev_posts,
                "tier_actions": tier.get("actions") or [], "score": score}

    async def run_actions(self, actions, name: str, uid=None, who: str | None = None,
                          score=None) -> None:
        """Public entry to run an action block from the bot layer (e.g. a minigame
        tier's block, after its result is posted), crediting the player."""
        if not actions:
            return
        xc = {}
        if uid is not None or who:
            xc["mention"] = self._mention(uid, who or "")
            xc["target"] = who or ""
        if score is not None:
            xc["score"] = score
        await self._run_action_block(actions, name, hdr="🎮",
                                     uid=(str(uid) if uid is not None else None),
                                     who=who, extra_ctx=xc)

    async def _run_fires(self, fires, who: str, uid: str | None) -> list:
        """Fire each {device_id, seconds} row independently and concurrently.
        Shared by the fire type's extra rows and the chance type's win-fires.
        Returns [{device_id, duration, status}] for the rows that actually fired."""
        out = []
        hi = self._hard_cap()
        for row in (fires or []):
            dev_id = (row or {}).get("device_id") or self._active_id()
            if not dev_id or self._device(dev_id) is None:
                continue
            try:
                dur = round(max(0.1, min(hi, float((row or {}).get("seconds") or 3))), 1)
            except (TypeError, ValueError, AttributeError):
                dur = 3.0
            fr = self._begin_or_extend(dev_id, dur, f"{who}'s device fire")
            if fr.get("status") not in ("started", "extended"):
                continue   # paused / at cap / no device → nothing fired, credit nothing
            self.credit_pump(uid, who, fr.get("added", dur), dev_id)
            out.append({"device_id": dev_id, "duration": dur, "status": fr.get("status")})
        return out

    # -- firing (per-device) ------------------------------------------------- #
    def _hard_cap(self) -> float:
        return float(self.cfg.get("roll", {}).get("max_seconds", 20))

    def _remaining(self, device_id: str | None) -> float:
        f = self._fires.get(device_id)
        return max(0.0, f["deadline"] - time.monotonic()) if f else 0.0

    def _begin_or_extend(self, device_id: str, duration: float, reason: str,
                         bypass_lock: bool = False, until_capacity: float | None = None) -> dict:
        # THE pause gate. Every path that can energize a device funnels through
        # here (rolls, commands, events, payoffs, prizes, web fires) — so nothing
        # can switch a pump on while the session is paused, even code added later.
        if self._paused:
            return {"status": "paused"}
        # Progression lock: after a competition, everyone else's capacity gains
        # are frozen until the winner uses their granted command (which passes
        # bypass_lock). Keeps the range from advancing any other way.
        if self._progression_lock is not None and not bypass_lock:
            return {"status": "locked"}
        # disable_at_100 gates EVERY fire path here (chance, events, minigame
        # payoffs, prizes included). The threshold is the capacity cap so an
        # enabled End Sequence (which deliberately runs past 100%) still works.
        if self.cfg.get("roll", {}).get("disable_at_100") and self.capacity >= self._capacity_cap():
            return {"status": "at_cap"}
        dev = self._device(device_id)
        if dev is None:
            return {"status": "no_device"}
        now = time.monotonic()
        cap = self._hard_cap()

        if device_id in self._fires:
            f = self._fires[device_id]
            if until_capacity is not None:
                # extend far enough to reach the target (exempt from the single-
                # fire cap, same safety ceiling as a fresh until-fire).
                sec = float((dev.get("calibration_seconds_to_100") or 60))
                need = min(sec * 2, max(0.0, (until_capacity - self.capacity) / 100.0 * sec + 5))
                new_deadline = max(f["deadline"], now + need)
                f["until_capacity"] = until_capacity
            else:
                new_deadline = min(now + cap, f["deadline"] + duration)
            added = max(0.0, new_deadline - f["deadline"])
            f["deadline"] = new_deadline
            f["extend"].set()
            return {"status": "extended", "added": round(added, 1), "remaining": round(new_deadline - now, 1)}

        # "fire until capacity %" (winner-only progression command): run long
        # enough to reach the target, exempt from the single-fire hard cap, with
        # an absolute safety ceiling of ~2× a full fill.
        if until_capacity is not None:
            sec = (self._device(device_id) or {}).get("calibration_seconds_to_100") or 60
            actual = min(float(sec) * 2, max(0.5, (until_capacity - self.capacity) / 100.0 * float(sec) + 5))
        else:
            actual = min(cap, duration)
        f = {"deadline": now + actual, "abort": asyncio.Event(),
             "extend": asyncio.Event(), "alias": dev.get("label") or dev["host"],
             "until_capacity": until_capacity}
        self._fires[device_id] = f
        f["task"] = asyncio.create_task(self._run_fire(device_id, reason))
        if self._pump_id() == device_id:
            self._cap_since = now
        # "added" = seconds actually delivered (post-clamp) — what leaderboard
        # credit should use, for starts and extensions alike.
        return {"status": "started", "added": round(actual, 1), "remaining": round(f["deadline"] - now, 1)}

    async def _wait_abort_or_extend(self, f: dict) -> None:
        ab = asyncio.create_task(f["abort"].wait())
        ex = asyncio.create_task(f["extend"].wait())
        _, pending = await asyncio.wait({ab, ex}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

    async def _run_fire(self, device_id: str, reason: str) -> None:
        dev = self._device(device_id)
        f = self._fires.get(device_id)
        try:
            async with self._dev_lock(device_id):
                await self._set_state(dev, True)
            self._log("device", f"{f['alias']} ON ({reason})")
            while f and device_id in self._fires:
                # "fire until capacity %": stop the moment the target is reached
                until = f.get("until_capacity")
                if until is not None and self.capacity >= until:
                    self._log("device", f"{f['alias']} reached {until:g}% — stopping")
                    break
                remaining = f["deadline"] - time.monotonic()
                if remaining <= 0:
                    break
                f["extend"].clear()
                try:
                    # poll capacity ~4×/s while chasing a target, else sleep to the deadline
                    await asyncio.wait_for(self._wait_abort_or_extend(f),
                                           timeout=min(remaining, 0.25) if until is not None else remaining)
                except asyncio.TimeoutError:
                    if until is None:
                        break
                if f["abort"].is_set():
                    self._log("device", f"{f['alias']} interrupted")
                    break
        except Exception as e:  # noqa: BLE001
            self._log("error", f"fire failed on {f['alias'] if f else device_id}: {e}")
        finally:
            # Pop the fire entry BEFORE the (possibly slow) OFF network call, so
            # a roll landing in that window starts a fresh fire instead of
            # "extending" this dying one into nothing. The per-device lock
            # inside _force_off keeps that new fire's ON from interleaving.
            self._fires.pop(device_id, None)
            if self._pump_id() == device_id:
                self._cap_since = None
            # Give the retrying OFF room before the watchdog piles on.
            self._on_sanctioned[device_id] = time.monotonic() + 15.0
            if dev:
                await self._force_off(dev)
            if f:
                self._log("device", f"{f['alias']} OFF")

    async def fire(self, duration: float, reason: str = "manual test", device_id: str | None = None) -> dict:
        try:
            duration = round(float(duration), 1)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            return {"ok": False, "status": "bad_duration", "error": "duration must be positive"}
        target = device_id or self._active_id()
        res = self._begin_or_extend(target, duration, reason) if target else {"status": "no_device"}
        ok = res["status"] in ("started", "extended")
        err = None if ok else {"paused": "session is paused",
                               "at_cap": "at capacity — control disabled"}.get(res["status"], "no active device")
        return {"ok": ok, "status": res["status"], "error": err,
                "added": res.get("added"), "remaining": res.get("remaining")}

    async def abort(self, device_id: str | None = None, *, reason: str = "aborted") -> None:
        ids = [device_id] if device_id else list(self._fires.keys())
        if ids and self._fires:
            self._log("device", f"abort ({reason})")
        for did in ids:
            f = self._fires.get(did)
            if f:
                f["abort"].set()
        if not self._fires:
            # nothing running — make sure the active relay is off as a safety net
            dev = self._active_device_dict()
            if dev:
                try:
                    await self._set_state(dev, False)
                except Exception:
                    pass

    # -- session pause / resume ---------------------------------------------- #
    async def pause(self, who: str = "") -> dict:
        """PAUSE the session: stop every running fire, force all relays OFF,
        cancel running timed events and live minigames, latch out every device-on
        path until resume(), and broadcast the pause notice to all listen channels.
        The latch persists in config so a crash/restart comes back paused."""
        if self._paused:
            return {"ok": False, "already": True, "error": "session is already paused"}
        who = (who or "").strip() or "the operator"
        # 1. Close the gate FIRST (before anything awaits) and persist it.
        self._paused = True
        self._paused_by = who
        cfg = config_store.update({"session_paused": True, "session_paused_by": who})
        self.cfg = cfg
        self._log("bot", f"SESSION PAUSED by {who}")
        # 2. Abort every running fire (their tasks handle their own retried OFF).
        await self.abort(reason=f"paused by {who}")
        # 3. Belt and braces: force OFF anything we believe is ON (covers fires
        #    whose OFF is still in flight, calibration runs, and test pulses).
        self._on_sanctioned.clear()
        for did, on in list(self._last_commanded.items()):
            if on:
                await self._force_off(self._device(did))
        # 4. Cancel timed events: in-flight loops die (no end message) and enabled
        #    events re-arm fresh after resume. "Once" events that ran stay done.
        #    Running capacity-event blocks are cancelled too (their session-
        #    remainder locks and fired-markers survive the pause).
        self._capev_cancel_all(clear_session=False)
        self._cancel_poll_task()
        self._cancel_competition()
        self._cancel_winner_button()   # a pending Winner Button dies with the pause (lifts its freeze)
        self._cancel_bonus_round()     # a live Bonus Round dies too (banks survive the pause)
        self._cmd_gate_block = False    # STOP lifts any command gate; resume starts clean
        self._cmd_gate_allow.clear(); self._cmd_gate_blocked_cmds.clear()
        self._cancel_deadline()   # keep the grant, but don't auto-fire into a paused session
        self._event_last.clear()
        self._event_fires.clear()
        self._event_cooldown_until.clear()
        self._event_activator.clear()
        self._runtime_events_on.clear()
        self._event_run_id.clear()
        # 5. Cancel live minigames (the bot layer disables their views + refunds).
        if self.cancel_games_cb:
            try:
                await self.cancel_games_cb()
            except Exception as e:  # noqa: BLE001
                self._log("error", f"cancelling games failed: {e}")
        # 6. Tell every listen channel.
        tmpl = self.cfg.get("pause_message") or (
            "⏸️ **Session paused** by [user] — pumps are off and commands are "
            "disabled until the operator resumes.")
        await self._announce(self.render(tmpl, {"user": who, "mention": who}), None)
        return {"ok": True, "paused": True}

    async def resume(self, who: str = "") -> dict:
        """Lift the pause latch and broadcast the resume notice. Cancelled events
        do NOT auto-restart mid-loop — enabled ones re-arm on their own timers."""
        if not self._paused:
            return {"ok": False, "already": True, "error": "session is not paused"}
        who = (who or "").strip() or "the operator"
        self._paused = False
        self._paused_by = ""
        cfg = config_store.update({"session_paused": False, "session_paused_by": ""})
        self.cfg = cfg
        self._log("bot", f"SESSION RESUMED by {who}")
        tmpl = self.cfg.get("resume_message") or "▶️ **Session resumed** by [user] — pump away!"
        await self._announce(self.render(tmpl, {"user": who, "mention": who}), None)
        return {"ok": True, "paused": False}

    def refund_use(self, uid, cmdkey: str) -> None:
        """Return the use + cooldown a command charged (a pause cancelled the
        game before the player got anything)."""
        cmdkey = (cmdkey or "").strip().lower()
        if not cmdkey or uid is None:
            return
        d = self._cmd_uses.get(str(uid))
        if d and d.get(cmdkey, 0) > 0:
            d[cmdkey] -= 1
        for scope_key in (str(uid), "*"):
            recs = self._cooldowns.get(scope_key)
            if recs:
                recs.pop(cmdkey, None)

    async def test_device(self, dev: dict, seconds: float = 2.0) -> dict:
        """Raw on→wait→off on a SPECIFIC device. No capacity, no fire state."""
        if self._paused:
            return {"ok": False, "error": "session is paused"}
        alias = dev.get("label") or dev.get("host") or dev.get("id") or "device"
        if dev.get("id"):
            # Sanction the ON window so the watchdog doesn't kill the test pulse.
            self._on_sanctioned[dev["id"]] = time.monotonic() + seconds + 10.0

        async def _pulse():
            try:
                async with self._dev_lock(dev.get("id") or alias):
                    await self._set_state(dev, True)
                self._log("device", f"TEST {alias} ON {seconds:.0f}s (no capacity)")
                await asyncio.sleep(seconds)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"test failed on {alias}: {e}")
            finally:
                await self._force_off(dev)
                self._log("device", f"TEST {alias} OFF")

        self._pulse_task = asyncio.create_task(_pulse())
        return {"ok": True}

    async def device_on(self, device_id: str) -> dict:
        """Turn a specific device ON and leave it on (used by the calibration
        'time it to 100%' flow). No capacity tracking."""
        if self._paused:
            return {"ok": False, "error": "session is paused"}
        dev = self._device(device_id)
        if dev is None:
            return {"ok": False, "error": "device not found"}
        # Calibration deliberately leaves the relay ON — exempt it from the
        # watchdog until device_off ends the run.
        self._on_sanctioned[device_id] = float("inf")
        try:
            async with self._dev_lock(device_id):
                await self._set_state(dev, True)
        except Exception as e:  # noqa: BLE001
            self._log("error", f"calibration on failed: {e}")
            self._on_sanctioned.pop(device_id, None)
            return {"ok": False, "error": str(e)}
        self._log("device", f"{dev.get('label') or device_id} ON (calibration)")
        return {"ok": True}

    async def device_off(self, device_id: str) -> dict:
        """Turn a specific device OFF (end of a calibration run)."""
        dev = self._device(device_id)
        if dev is None:
            return {"ok": False, "error": "device not found"}
        self._on_sanctioned.pop(device_id, None)
        try:
            async with self._dev_lock(device_id):
                await self._set_state(dev, False)
        except Exception as e:  # noqa: BLE001
            self._log("error", f"calibration off failed: {e}")
            return {"ok": False, "error": str(e)}
        self._log("device", f"{dev.get('label') or device_id} OFF (calibration)")
        return {"ok": True}

    def reset_capacity(self) -> None:
        self.capacity = 0.0
        self._milestones_fired.clear()
        self._log("capacity", "reset to 0%")

    async def start_events(self, names, uid=None, who: str = "") -> tuple:
        """A command activates events by name — intelligently: an already-running
        event yields the global 'in process' message; one still on cooldown yields
        the global 'cooldown' message; otherwise it switches on (re-armed) and its
        own activation message is yielded. The activator (uid, who) is remembered so
        the event's pump fires credit that person's leaderboard.

        Returns (posts, activated): the channel messages to post AFTER the command's
        own reply (so 'already running' / 'on cooldown' / activation lines follow
        it), and a count of events that actually switched on. `activated` lets a
        pure event-trigger command skip charging a use when the event was blocked."""
        if self._paused:
            return [], 0
        now = time.monotonic()
        posts = []
        activated = 0
        for n in (names or []):
            key = str(n).strip().lower()
            if not key:
                continue
            ev = self._event_by_name(key)
            if ev is None:
                # No such event: activating nothing shouldn't count as an
                # activation (it used to consume the caller's use for a no-op).
                self._log("error", f"start_events: no event named '{key}'")
                continue
            hdr = self._evt_hdr(ev.get("name", ""))
            active = bool(ev.get("enabled")) or key in self._runtime_events_on
            if active and key not in self._events_done:
                msg = (self.cfg.get("event_in_process_message") or "").strip()
                if msg:
                    posts.append(hdr + self.render(msg, self._event_ctx(ev)))
                continue   # already running → blocked (no activation)
            cd_until = self._event_cooldown_until.get(key, 0.0)
            if now < cd_until:
                msg = (self.cfg.get("event_cooldown_message") or "").strip()
                if msg:
                    posts.append(hdr + self.render(msg, {**self._event_ctx(ev), "cooldown": f"{cd_until - now:.0f}"}))
                continue   # on cooldown → blocked (no activation)
            self._runtime_events_on.add(key)
            self._event_last.pop(key, None)    # re-arm the timer from now
            self._events_done.discard(key)
            self._event_fires.pop(key, None)   # fresh loop count for this activation
            activated += 1
            if uid is not None:
                self._event_activator[key] = (uid, who)   # credit this person for the event's pumps
            am = (ev.get("activation_message") or "").strip()
            if am:
                posts.append(hdr + self.render(am, self._event_ctx(ev)))
            if ev.get("fire_immediately"):
                # Fire round 1 right now, in order (after the activation line),
                # instead of racing the async tick loop. The tick then waits `every`.
                self._event_last[key] = now
                await self._fire_event_once(ev, key, sink=posts)
        return posts, activated

    def _event_by_name(self, key: str) -> dict | None:
        key = (key or "").strip().lower()
        for ev in self.cfg.get("events", []):
            if (ev.get("name") or "").strip().lower() == key:
                return ev
        return None

    def _event_ctx(self, ev: dict) -> dict:
        """Placeholders for an event's messages: [event] [loop_timer] [current_loop] [total_loops]."""
        try:
            every = float(ev.get("every") or 0)
        except (TypeError, ValueError):
            every = 0
        try:
            cap = int(ev.get("max_repeats") or 0)
        except (TypeError, ValueError):
            cap = 0
        name = (ev.get("name") or "").strip()
        return {"event": name, "loop_timer": f"{every:g}",
                "total_loops": ("∞" if cap <= 0 else str(cap)),
                "current_loop": self._event_fires.get(name.lower(), 0)}

    def session_reset(self) -> None:
        """Full session reset: capacity→0, clear all cooldowns, revert any
        command-started events, and re-arm every timed event."""
        self.capacity = 0.0
        self._milestones_fired.clear()
        self._capev_cancel_all(clear_session=True)
        self._cancel_poll_task()
        self._cooldowns.clear()
        self._runtime_events_on.clear()
        self._event_last.clear()
        self._events_done.clear()
        self._event_fires.clear()
        self._event_cooldown_until.clear()
        self._event_activator.clear()
        self._event_run_id.clear()
        self._perfect.clear()
        self._prize_uses.clear()
        self._pump_time.clear()
        self._pump_range.clear()
        self._cmd_uses.clear()
        self._last_fired.clear()
        self._last_poll_winner = None
        self._cancel_competition()
        self._cancel_winner_button()
        self._cancel_bonus_round()
        self._bonus_bank.clear()
        self._cmd_gate_block = False
        self._cmd_gate_allow.clear(); self._cmd_gate_blocked_cmds.clear()
        self._clear_winner_grants(all_incl_stash=True)
        self._last_winner = ""; self._last_winner_score = 0.0
        self._last_runnerup = ""; self._last_runnerup_score = 0.0
        self._last_total_score = 0.0
        self._last_results = ""
        self._current_range_key = None
        self._end_triggered = False
        # Uptime restarts with the session — but only ticks if a session is
        # actually running (Activation on); otherwise it goes back to 0.
        self._session_start = time.monotonic() if self.cfg.get("listener_enabled") else None
        self._session_frozen = 0.0
        self._log("bot", "SESSION RESET — capacity 0%, cooldowns cleared, events re-armed, prizes reset")

    # -- max roll prizes (multiple) ----------------------------------------- #
    def _prizes(self) -> list:
        """The prize list. Falls back to the legacy single max_roll_prize."""
        lst = self.cfg.get("prizes")
        if lst:
            return [p for p in lst if p.get("enabled")]
        mp = self.cfg.get("max_roll_prize") or {}
        return [mp] if mp.get("enabled") else []

    def _prize_key(self, p: dict) -> str:
        return (p.get("command") or "").strip().lower()

    def active_prize(self) -> dict | None:
        """The prize being tracked right now. An 'All'-gated prize overrides and
        disables any range-gated prizes; otherwise the prize whose range matches
        the current capacity (first match) is active."""
        prizes = self._prizes()
        for p in prizes:  # All-gated overrides everything
            if (p.get("range_gate") or "all") == "all":
                return p
        for p in prizes:
            if self._range_gate_ok(p.get("range_gate")):
                return p
        return None

    def prize_command_names(self) -> set:
        return {self._prize_key(p) for p in self._prizes() if self._prize_key(p)}

    def find_prize_by_command(self, name: str) -> dict | None:
        name = (name or "").strip().lower()
        for p in self._prizes():
            if self._prize_key(p) == name:
                return p
        return None

    def _uses_left(self, uid, pkey: str) -> int:
        return self._prize_uses.get(str(uid), {}).get(pkey, 0)

    def _prize_progress(self, ev: dict, who: str, mention: str) -> tuple[str, str]:
        """On a perfect roll, advance the person's CUMULATIVE count (carries
        across ranges) and unlock the currently-active prize when reached."""
        uid = ev.get("uid")
        if uid is None:
            return "", ""
        p = self.active_prize()
        if not p:
            return "", ""
        pkey = self._prize_key(p)
        if self._uses_left(uid, pkey) > 0:
            return "", ""  # this prize already unlocked for them
        cnt = self._perfect.get(str(uid), 0) + 1
        self._perfect[str(uid)] = cnt          # cumulative — never per-prize
        goal = max(1, int(p.get("goal") or 1))
        anon = self._anon_label()
        if cnt >= goal:
            uses = max(1, int(p.get("uses") or 1))
            self._prize_uses.setdefault(str(uid), {})[pkey] = uses
            self._log("bot", f"{who} unlocked prize '{pkey}' ({goal} perfects)")
            prefix = self.cfg.get("command_prefix", "!")
            pc = {"prize_cmd": f"{prefix}{pkey}", "prize_desc": p.get("description") or "", "uses": uses}
            tmpl = p.get("unlock_message") or "[mention] has unlocked: [prize_cmd] — [prize_desc]\\nYou can use this command a total of [uses] times."
            return (self.render(tmpl, {"user": who, "mention": mention, **pc}),
                    self.render(tmpl, {"user": anon, "mention": anon, **pc}))
        rem = goal - cnt
        tmpl = p.get("progress_message") or "[mention] rolled a perfect score [[count]/[goal]] — [remaining] more will unlock a bonus command!"
        c = {"count": cnt, "goal": goal, "remaining": rem}
        return (self.render(tmpl, {"user": who, "mention": mention, **c}),
                self.render(tmpl, {"user": anon, "mention": anon, **c}))

    async def use_prize_command(self, who: str, uid: str | None, prize: dict) -> dict:
        """Run an unlocked prize command (the person keeps uses across ranges;
        the prize's own range gate controls where it may be USED)."""
        if self._paused:
            return self._paused_result(who, uid)
        pkey = self._prize_key(prize)
        left = self._uses_left(uid, pkey)
        if left <= 0:
            return {"ok": False, "silent": True}  # not unlocked / exhausted
        if not self._range_gate_ok(prize.get("range_gate")):
            return {"ok": False, "silent": True}  # can't be used in this range

        typ = (prize.get("action") or "fire").lower()
        extra_ctx = {"user": who, "mention": self._mention(uid, who), "uses_left": left - 1}
        anon_ctx = {"user": self._anon_label(), "mention": self._anon_label(), "uses_left": left - 1}

        def _spend():
            self._prize_uses.setdefault(str(uid), {})[pkey] = left - 1

        if typ == "say":
            _spend()
            tmpl = prize.get("reply") or "[mention] used the prize! ([uses_left] left)"
            return {"ok": True, "device": False, "started": False,
                    "reply": self.render(tmpl, extra_ctx), "reply_anon": self.render(tmpl, anon_ctx)}

        target = prize.get("device_id") or self._active_id()
        if self._device(target) is None:
            return {"ok": False, "error": "prize target device not found"}
        if typ == "roll":
            rd, rs = self.range_dice(self.range_for(self.capacity))
            dice = int(prize.get("dice") or rd); sides = int(prize.get("sides") or rs)
            total, _ = self._roll_total(dice, sides)
            duration = self._duration_from_total(total)
            extra_ctx.update({"dice": f"{dice}d{sides}", "result": total, "total": total})
            anon_ctx.update({"dice": f"{dice}d{sides}", "result": total, "total": total})
        else:
            duration = round(max(0.1, min(self._hard_cap(), float(prize.get("seconds") or 5))), 1)
        s2c = self._secs_to_capacity(duration, target)
        extra_ctx.update({"secs": f"{duration:.1f}", "seconds": f"{duration:.1f}", "secs2capacity": s2c})
        anon_ctx.update({"secs": f"{duration:.1f}", "seconds": f"{duration:.1f}", "secs2capacity": s2c})

        _spend()
        self._begin_or_extend(target, duration, f"prize by {who}")
        self._log("roll", f"{who} used prize '{pkey}' → {duration:.1f}s ({left-1} left)")
        tmpl = prize.get("reply") or "✨ [mention] used the prize command! ([uses_left] uses left)"
        return {"ok": True, "device": True, "started": True,
                "reply": self.render(tmpl, extra_ctx), "reply_anon": self.render(tmpl, anon_ctx)}

    def set_capacity(self, value) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        self.capacity = max(0.0, min(self._capacity_cap(), v))
        # mark already-passed milestones as fired so a manual jump doesn't spam
        self._milestones_fired = {r.get("min") for r in self.cfg.get("capacity_ranges", [])
                                  if r.get("min", 0) > 0 and self.capacity >= r.get("min", 0)}
        self._log("capacity", f"set to {self.capacity:.1f}%")

    # -- reporting ----------------------------------------------------------- #
    def _range_label(self) -> str:
        """The current range as 'min%-max%' for command-list headers."""
        r = self.range_for(self.capacity)
        return f"{r.get('min')}%-{r.get('max')}%"

    def _custom_command_lines(self, prefix: str) -> list[str]:
        """Non-system commands usable right now — members of the CURRENT range OR
        always-on (cmd_enabled_in_range covers both), enabled and not hidden."""
        lines = []
        for c in self.cfg.get("commands", []):
            nm = (c.get("name") or "").strip()
            if not nm or not c.get("enabled", True) or c.get("hide_in_list"):
                continue
            if not self.cmd_enabled_in_range(nm.lower()):
                continue  # not in this range (always-on commands always pass)
            line = f"**{prefix}{nm}**"
            desc = (c.get("description") or "").strip()
            if desc:
                line += f" - {desc}"
            lines.append(line)
        return lines

    def _system_command_lines(self, prefix: str) -> list[str]:
        """The built-in system commands (always excluded from [custom_commands])."""
        bn = self.builtin_names()
        lines = []
        if self.cfg.get("roll_enabled", True) and self.cmd_enabled_in_range("roll"):
            lines.append(f"**{prefix}{bn['roll']}** - roll the dice")
        lines.append(f"**{prefix}{bn['capacity']}** - check capacity")
        lines.append(f"**{prefix}{bn['leaderboard']}** - show the leaderboard (this session)")
        lines.append(f"**{prefix}{bn['leaderboard_life']}** - all-time top pumpers")
        lines.append(f"**{prefix}{bn['pumptimer']}** - time left on the pump")
        if self._poll is not None:
            lines.append(f"**{prefix}{bn['vote']} <1-{len(self._poll['opts'])}>** - vote in the running poll")
        return lines

    def custom_commands_str(self, prefix: str) -> str:
        """[custom_commands] — a headed list of the custom commands active in the
        current range (system commands excluded). Header shows the range span."""
        header = f"**Capacity Range Commands ({self._range_label()}):**"
        lines = self._custom_command_lines(prefix)
        return header + ("\n" + "\n".join(lines) if lines else "")

    def _commands_str(self, prefix: str) -> str:
        """[commands] — the custom commands usable now (current range + always-on)
        on top, then the system commands underneath, each under its own header."""
        parts = [self.custom_commands_str(prefix)]
        sys_lines = self._system_command_lines(prefix)
        if sys_lines:
            parts.append("**System Commands:**\n" + "\n".join(sys_lines))
        return "\n".join(parts)

    def auto_report_text(self, prefix: str) -> str:
        cmds_str = self._commands_str(prefix)
        custom = ((self.cfg.get("auto_report") or {}).get("message") or "").strip()
        if custom:
            return self.render(custom, {"commands": cmds_str})
        r = self.range_for(self.capacity)
        rd, rs = self.range_dice(r)
        lines = [f"📊 **Capacity: {round(self.capacity, 1)}%** — rolling "
                 f"**{rd}d{rs}** (range {r.get('min')}–{r.get('max')}%)"]
        ann = self.render((r.get("announce") or "").strip())
        if ann:
            lines.append(f"> {ann}")
        cd = self._cooldown()
        lines.append(f"Commands ({f'once per {int(cd)}s' if cd else 'no cooldown'}): " + cmds_str)
        return "\n".join(lines)

    # -- state / log --------------------------------------------------------- #
    def _log(self, kind: str, msg: str) -> None:
        self.events.appendleft({"t": _now_hms(), "kind": kind, "msg": msg})

    def _device_log(self, msg: str) -> None:
        """Sink for device telemetry → Activity log. 'Silence ON/OFF calls'
        hides the noisy set_state on/off lines here (they still print to console);
        discover/add/get always show."""
        if self.cfg.get("silence_onoff_log") and msg.startswith("USE  set_state"):
            return
        self._log("device", msg)

    def _users_view(self) -> list[dict]:
        now = time.monotonic()
        out = []
        for uid, rec in self._users.items():
            # summarize: the longest cooldown this user is currently sitting on
            cds = self._cooldowns.get(uid, {})
            worst = max((self.cooldown_remaining(uid, k) for k in cds), default=0.0)
            out.append({"id": uid, "name": rec["name"], "count": rec["count"],
                        "ago": round(now - rec["last"], 1), "cooldown": round(worst, 1)})
        out.sort(key=lambda u: u["ago"])
        return out

    def snapshot(self) -> dict:
        fires = {did: round(self._remaining(did), 1) for did in self._fires}
        return {
            "capacity": round(self.capacity, 1),
            "uptime": self._fmt_duration(self.session_uptime()),
            "uptime_seconds": int(self.session_uptime()),
            "pump_on": self._pump_id() in self._fires,
            "firing": bool(self._fires),
            "remaining": round(self._remaining(self._active_id()), 1),
            "fires": fires,
            "active_device": self._active_device_dict(),
            "current_range": self.range_for(self.capacity),
            "bot_connected": self.bot_connected,
            "paused": self._paused,
            "paused_by": self._paused_by,
            "mock_mode": bool(self.cfg.get("mock_mode")),
            "listener_enabled": bool(self.cfg.get("listener_enabled")),
            "users": self._users_view(),
            # Activity LOG — distinct key from the timed-"events" config which
            # _public_state also exposes (they used to collide on "events").
            "log": list(self.events)[:60],
        }


def _now_hms() -> str:
    lt = time.localtime()
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
