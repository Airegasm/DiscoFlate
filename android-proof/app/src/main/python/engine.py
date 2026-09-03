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
        # Max Roll Prize tracking (in-memory; resets on session reset / restart).
        self._perfect: dict[str, int] = {}       # uid -> perfect-roll count
        self._prize_uses: dict[str, int] = {}    # uid -> remaining uses (present = unlocked)
        self._pump_time: dict[str, dict] = {}    # uid -> {name, seconds, capacity} (session leaderboard)
        # Lifetime (all-time) leaderboard — persisted to data/, survives session
        # resets AND app restarts. Separate from the per-session _pump_time above.
        self._pump_life: dict[str, dict] = self._load_lifetime()
        self._cmd_uses: dict[str, dict] = {}     # uid -> {cmdname: times used this session}
        self._last_fired: dict[str, float] = {}  # cmdkey -> last-fired monotonic (anti-spam buffer)
        self._current_range_key = None           # (min,max) of the range we're in, for entry detection
        self._end_triggered = False               # End Sequence final threshold already fired
        # Session uptime: monotonic marker set when the engine starts ticking and
        # re-stamped on every session_reset (matches the "this session" scope used
        # by the leaderboard / use budgets). In-memory only; resets on app restart.
        self._session_start: float = time.monotonic()

        self.announce_cb = None                # async (text, image) -> None
        self.end_session_cb = None             # async () -> None : deactivate without the off-message
        self.bot_connected: bool = False

        # Device add/search/use debug flows into the Activity log too (not just stdout).
        device_control.set_log_sink(self._device_log)

    # -- config / device lookup --------------------------------------------- #
    def set_config(self, cfg: dict) -> None:
        self.cfg = cfg
        now_on = bool(cfg.get("listener_enabled"))
        if now_on and not self._listener_was:
            # Activation: re-arm all event timers so loops/once start fresh, and
            # re-trigger range entry for the current range.
            self._event_last.clear()
            self._events_done.clear()
            self._event_fires.clear()
            self._event_cooldown_until.clear()
            self._event_activator.clear()
            self._current_range_key = None
            self._end_triggered = False
        elif self._listener_was and not now_on:
            # Deactivation: quietly clear cooldowns and cancel timed events.
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
        await device_control.set_state(dev, on, self._vendor_creds(dev))

    # -- lifecycle ----------------------------------------------------------- #
    def start(self) -> None:
        if not self._tick_task:
            self._session_start = time.monotonic()   # session clock starts now
            self._tick_task = asyncio.create_task(self._capacity_loop())

    async def stop(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
            self._tick_task = None
        await self.abort(reason="shutdown")

    async def _capacity_loop(self) -> None:
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
                await self._check_milestones()
                await self._check_range_entry()
                await self._check_end_sequence()
                await self._check_cooldown_resets()
                await self._check_events()
            except asyncio.CancelledError:
                break
            except Exception as e:  # never let the loop die
                self._log("error", f"capacity loop: {e}")

    # -- message rendering --------------------------------------------------- #
    def _render(self, template: str, ctx: dict) -> str:
        out = template or ""
        for k, v in ctx.items():
            s = "" if v is None else str(v)
            out = out.replace(f"[{k}]", s).replace("{" + k + "}", s)
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
        pz = self.active_prize() or {}
        ctx["max_roll_goal"] = pz.get("goal") if pz.get("goal") not in (None, "") else ""
        ctx["max_roll_command"] = f"{prefix}{(pz.get('command') or '').strip()}" if pz.get("command") else ""
        ctx["max_roll_desc"] = pz.get("description") or ""
        if extra:
            ctx.update(extra)
        # [announce] = the current range's announce text (its own placeholders
        # resolved; [announce] inside it stays literal to avoid recursion).
        ann = (self.range_for(self.capacity).get("announce") or "").strip()
        ctx["announce"] = self._render(ann, ctx)
        return self._render(template, ctx)

    def session_uptime(self) -> float:
        """Seconds the current session has been running (since start / last reset)."""
        return max(0.0, time.monotonic() - self._session_start)

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
                    if res.get("ok") and res.get("reply"):
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

    async def _announce(self, text: str, image: str | None, replace_key: str | None = None) -> None:
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
        for key_uid, cmds in self._cooldowns.items():
            for cmdkey, rec in cmds.items():
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

    # -- timed events -------------------------------------------------------- #
    async def _check_events(self) -> None:
        """Fire each enabled event when its interval elapses. Events only run
        while the listener is enabled (so they don't fire on a paused/off bot)."""
        if not self.cfg.get("listener_enabled"):
            return
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

    async def _run_event(self, ev: dict, extra: dict | None = None, sink: list | None = None,
                         replace_key: str | None = None) -> None:
        name = ev.get("name", "event")
        action = (ev.get("action") or "message").lower()
        msg = (ev.get("message") or "").strip()
        # If a command started this event, credit that person's leaderboard for its pumps.
        act_uid, act_who = self._event_activator.get(name.strip().lower(), (None, ""))

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
                self._begin_or_extend(target, dur, f"event {name}")
                self.credit_pump(act_uid, act_who, dur, target)
                self._log("bot", f"event '{name}': rolled {dice}d{sides}={total}")
            else:
                secs = round(max(0.1, min(self._hard_cap(), float(ev.get("seconds") or 3))), 1)
                self._begin_or_extend(target, secs, f"event {name}")
                self.credit_pump(act_uid, act_who, secs, target)
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
                "pumptimer": (n.get("pumptimer") or "pumptimer").strip().lower()}

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
        for board in (self._pump_time, self._pump_life):
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
            os.replace(tmp, self._lifetime_path())
        except OSError as e:  # noqa: BLE001
            self._log("error", f"couldn't save lifetime stats: {e}")

    def reset_lifetime(self) -> None:
        self._pump_life.clear()
        self._save_lifetime()
        self._log("bot", "LIFETIME leaderboard reset")

    @staticmethod
    def _format_board(rows, header: str, empty: str) -> str:
        rows = sorted(rows, key=lambda r: r["seconds"], reverse=True)
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
        return self._format_board(self._pump_time.values(), "***TOP PUMPERS***",
                                  "Nobody has pumped yet this session.")

    def leaderboard_life_text(self) -> str:
        return self._format_board(self._pump_life.values(), "***ALL-TIME TOP PUMPERS***",
                                  "Nobody has pumped yet.")

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

    async def roll_and_fire(self, who: str, uid: str | None = None,
                            dice: int | None = None, sides: int | None = None) -> dict:
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
        if self.cfg.get("roll", {}).get("disable_at_100") and self.capacity >= 100:
            return {"ok": False, "error": "at 100% capacity — control disabled"}
        if uid is not None:
            self._track_user(uid, who)
            if scope == "command" or not exempt:
                self._touch_cooldown(key_uid, "roll", cd)

        p = self.preview_roll(dice, sides)
        fr = self._begin_or_extend(target, p["duration"], f"roll by {who}")
        extended = fr["status"] == "extended"
        self.credit_pump(uid, who, p["duration"], target)
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

        if not self.cmd_enabled_in_range(name.lower()):
            return {"ok": False, "silent": True}  # not a member of the current range → ignore quietly
        # The owner is never limited by cooldowns or per-person max-uses (they're
        # still subject to the in-progress event guard).
        owner_exempt = uid is not None and self._is_exempt(uid, who)
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
            ev_posts = await self.start_events(cmd.get("start_events"), uid, who)
            _spend_use()
            anon = self._anon_label()
            return {"ok": True, "device": False, "started": False, "events_posted": ev_posts,
                    "reply": self.render(cmd.get("reply") or "", {"user": who, "mention": self._mention(uid, who), "cmd_remain": _remain()}) or f"{name}!",
                    "reply_anon": self.render(cmd.get("reply") or "", {"user": anon, "mention": anon, "cmd_remain": _remain()}) or f"{name}!"}

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
            ev_posts = await self.start_events(cmd.get("start_events"), uid, who) if win else []
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
        if self.cfg.get("roll", {}).get("disable_at_100") and self.capacity >= 100:
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

        ev_posts = await self.start_events(cmd.get("start_events"), uid, who)
        fr = self._begin_or_extend(target, duration, f"{name} by {who}")
        extended = fr["status"] == "extended"
        self.credit_pump(uid, who, duration, target)
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
        return {"real": real, "anon": anon_msg}

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
                dur = round(max(0.1, min(hi, float(row.get("seconds") or 3))), 1)
            except (TypeError, ValueError):
                dur = 3.0
            fr = self._begin_or_extend(dev_id, dur, f"{who}'s device fire")
            self.credit_pump(uid, who, dur, dev_id)
            out.append({"device_id": dev_id, "duration": dur, "status": fr.get("status")})
        return out

    # -- firing (per-device) ------------------------------------------------- #
    def _hard_cap(self) -> float:
        return float(self.cfg.get("roll", {}).get("max_seconds", 20))

    def _remaining(self, device_id: str | None) -> float:
        f = self._fires.get(device_id)
        return max(0.0, f["deadline"] - time.monotonic()) if f else 0.0

    def _begin_or_extend(self, device_id: str, duration: float, reason: str) -> dict:
        dev = self._device(device_id)
        if dev is None:
            return {"status": "no_device"}
        now = time.monotonic()
        cap = self._hard_cap()

        if device_id in self._fires:
            f = self._fires[device_id]
            new_deadline = min(now + cap, f["deadline"] + duration)
            added = max(0.0, new_deadline - f["deadline"])
            f["deadline"] = new_deadline
            f["extend"].set()
            return {"status": "extended", "added": round(added, 1), "remaining": round(new_deadline - now, 1)}

        f = {"deadline": now + min(cap, duration), "abort": asyncio.Event(),
             "extend": asyncio.Event(), "alias": dev.get("label") or dev["host"]}
        self._fires[device_id] = f
        f["task"] = asyncio.create_task(self._run_fire(device_id, reason))
        if self._pump_id() == device_id:
            self._cap_since = now
        return {"status": "started", "remaining": round(f["deadline"] - now, 1)}

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
            await self._set_state(dev, True)
            self._log("device", f"{f['alias']} ON ({reason})")
            while f and device_id in self._fires:
                remaining = f["deadline"] - time.monotonic()
                if remaining <= 0:
                    break
                f["extend"].clear()
                try:
                    await asyncio.wait_for(self._wait_abort_or_extend(f), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if f["abort"].is_set():
                    self._log("device", f"{f['alias']} interrupted")
                    break
        except Exception as e:  # noqa: BLE001
            self._log("error", f"fire failed on {f['alias'] if f else device_id}: {e}")
        finally:
            try:
                if dev:
                    await self._set_state(dev, False)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"turn-off failed: {e}")
            self._fires.pop(device_id, None)
            if self._pump_id() == device_id:
                self._cap_since = None
            if f:
                self._log("device", f"{f['alias']} OFF")

    async def fire(self, duration: float, reason: str = "manual test", device_id: str | None = None) -> dict:
        target = device_id or self._active_id()
        res = self._begin_or_extend(target, round(float(duration), 1), reason) if target else {"status": "no_device"}
        ok = res["status"] in ("started", "extended")
        return {"ok": ok, "status": res["status"], "error": None if ok else "no active device"}

    async def abort(self, device_id: str | None = None, reason: str = "aborted") -> None:
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

    async def test_device(self, dev: dict, seconds: float = 2.0) -> dict:
        """Raw on→wait→off on a SPECIFIC device. No capacity, no fire state."""
        alias = dev.get("label") or dev.get("host") or dev.get("id") or "device"

        async def _pulse():
            try:
                await self._set_state(dev, True)
                self._log("device", f"TEST {alias} ON {seconds:.0f}s (no capacity)")
                await asyncio.sleep(seconds)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"test failed on {alias}: {e}")
            finally:
                try:
                    await self._set_state(dev, False)
                except Exception as e:  # noqa: BLE001
                    self._log("error", f"test off failed: {e}")
                self._log("device", f"TEST {alias} OFF")

        asyncio.create_task(_pulse())
        return {"ok": True}

    async def device_on(self, device_id: str) -> dict:
        """Turn a specific device ON and leave it on (used by the calibration
        'time it to 100%' flow). No capacity tracking."""
        dev = self._device(device_id)
        if dev is None:
            return {"ok": False, "error": "device not found"}
        try:
            await self._set_state(dev, True)
        except Exception as e:  # noqa: BLE001
            self._log("error", f"calibration on failed: {e}")
            return {"ok": False, "error": str(e)}
        self._log("device", f"{dev.get('label') or device_id} ON (calibration)")
        return {"ok": True}

    async def device_off(self, device_id: str) -> dict:
        """Turn a specific device OFF (end of a calibration run)."""
        dev = self._device(device_id)
        if dev is None:
            return {"ok": False, "error": "device not found"}
        try:
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

    async def start_events(self, names, uid=None, who: str = "") -> list:
        """A command activates events by name — intelligently: an already-running
        event yields the global 'in process' message; one still on cooldown yields
        the global 'cooldown' message; otherwise it switches on (re-armed) and its
        own activation message is yielded. The activator (uid, who) is remembered so
        the event's pump fires credit that person's leaderboard.

        Returns the list of channel messages to post AFTER the command's own reply
        (so 'already running' / 'on cooldown' / activation lines follow it)."""
        now = time.monotonic()
        posts = []
        for n in (names or []):
            key = str(n).strip().lower()
            if not key:
                continue
            ev = self._event_by_name(key)
            if ev is None:
                self._runtime_events_on.add(key)   # unknown name → legacy behaviour
                self._event_last.pop(key, None)
                self._events_done.discard(key)
                continue
            active = bool(ev.get("enabled")) or key in self._runtime_events_on
            if active and key not in self._events_done:
                msg = (self.cfg.get("event_in_process_message") or "").strip()
                if msg:
                    posts.append(self.render(msg, self._event_ctx(ev)))
                continue
            cd_until = self._event_cooldown_until.get(key, 0.0)
            if now < cd_until:
                msg = (self.cfg.get("event_cooldown_message") or "").strip()
                if msg:
                    posts.append(self.render(msg, {**self._event_ctx(ev), "cooldown": f"{cd_until - now:.0f}"}))
                continue
            self._runtime_events_on.add(key)
            self._event_last.pop(key, None)    # re-arm the timer from now
            self._events_done.discard(key)
            self._event_fires.pop(key, None)   # fresh loop count for this activation
            if uid is not None:
                self._event_activator[key] = (uid, who)   # credit this person for the event's pumps
            am = (ev.get("activation_message") or "").strip()
            if am:
                posts.append(self.render(am, self._event_ctx(ev)))
            if ev.get("fire_immediately"):
                # Fire round 1 right now, in order (after the activation line),
                # instead of racing the async tick loop. The tick then waits `every`.
                self._event_last[key] = now
                await self._fire_event_once(ev, key, sink=posts)
        return posts

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
        self._cmd_uses.clear()
        self._current_range_key = None
        self._end_triggered = False
        self._session_start = time.monotonic()   # uptime clock restarts with the session
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
