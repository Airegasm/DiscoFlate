"""
discord_bot.py — the Discord listener (a real bot account, not a self-bot).

Reacts to prefix commands (default "!"):
  !roll        roll the current capacity range's dice, fire the active device
  !capacity    report current capacity + which dice are active

Optional extras (all opt-in from the web UI):
  * per-user cooldown on !roll
  * an auto-report posted to a channel every N seconds
  * per-range one-time milestone messages, with optional image (URL or file)

The listener can be muted from the web UI (listener_enabled) without dropping
the connection. Optional guild/channel/user allowlists scope where it responds.
"""

from __future__ import annotations

import asyncio
import os
import time
import discord

import config_store
import minigames


def _resolve_img(image: str | None) -> str | None:
    """Uploaded images are stored as 'images/<name>' relative paths — resolve
    them against the data dir (absolute paths from older configs pass through)."""
    if image and not image.lower().startswith(("http://", "https://")) and not os.path.isabs(image):
        return os.path.join(config_store.DATA_DIR, "images", os.path.basename(image))
    return image

# Short token used for the actor name inside an output header when the destination
# isn't allowed to see the real name. Kept terse on purpose (the long
# anon_user_label sentence is for message bodies, not the compact [label · x] tag).
_HDR_ANON = "ANON"


class PollVoteButton(discord.ui.Button):
    def __init__(self, bot, idx: int, label: str):
        super().__init__(label=f"{idx + 1} · {(label or '')[:70]}",
                         style=discord.ButtonStyle.primary)
        self._bot = bot
        self._idx = idx

    async def callback(self, interaction: discord.Interaction):
        await self._bot.handle_vote_interaction(interaction, self._idx + 1)


class PollVoteView(discord.ui.View):
    """Tap-to-vote buttons attached to every live poll embed. timeout=None so
    late taps route to cast_vote, which answers 'no poll is running' cleanly
    after the poll ends (instead of Discord's 'interaction failed')."""

    def __init__(self, bot, labels: list):
        super().__init__(timeout=None)
        for i, lab in enumerate((labels or [])[:4]):
            self.add_item(PollVoteButton(bot, i, str(lab)))


class CompetitionEnterView(discord.ui.View):
    """The public competition embed's 'Enter Challenge' button — opens each
    player's private (ephemeral) roller."""

    def __init__(self, bot, meta):
        super().__init__(timeout=None)
        self.bot = bot
        self.meta = meta or {}

    @discord.ui.button(label="🎲 Enter Challenge", style=discord.ButtonStyle.success)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        eng = self.bot.engine
        if not eng.competition_window_open():
            await interaction.response.send_message("⌛ This challenge has ended.", ephemeral=True)
            return
        res = eng.competition_join(str(interaction.user.id), interaction.user.display_name)
        if not res.get("ok"):
            await interaction.response.send_message("🚫 " + res.get("error", "can't join"), ephemeral=True)
            return
        view = RollerView(self.bot, interaction.user.display_name, res["rolls"], res["rerolls"])
        await interaction.response.send_message(view.text(), view=view, ephemeral=True)


class RollerView(discord.ui.View):
    """A player's private roller: roll N times; the latest roll can be rerolled
    up to `rerolls` times; each Roll locks the previous; the final Roll becomes
    Submit, which posts their result to the channel all at once."""

    def __init__(self, bot, who, rolls: int, rerolls: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.who = who
        self.n = max(1, int(rolls))
        self.rerolls_left = max(0, int(rerolls))
        self.locked = []
        self.pending = self.bot.engine.competition_roll_value(0)   # first roll (slot 0)
        self._sync_buttons()

    def _final(self):
        return len(self.locked) + 1 >= self.n   # the pending roll is the last one

    def text(self):
        slot = len(self.locked) + 1
        line = f"🎲 **Roll {slot}/{self.n}: {self.pending:g}**"
        if self.locked:
            line += "\nLocked: " + ", ".join(f"{v:g}" for v in self.locked)
        if self.rerolls_left:
            line += f"\n🔄 Rerolls left: {self.rerolls_left}"
        line += "\n\n" + ("**Submit** to lock it all in!" if self._final()
                          else "**Roll** to keep this and roll the next.")
        return line

    def _sync_buttons(self):
        self.roll_btn.label = "✅ Submit" if self._final() else "🎲 Roll"
        self.roll_btn.style = discord.ButtonStyle.success if self._final() else discord.ButtonStyle.primary
        self.reroll_btn.label = f"🔄 Reroll ({self.rerolls_left})"
        self.reroll_btn.disabled = self.rerolls_left <= 0

    @discord.ui.button(label="🎲 Roll", style=discord.ButtonStyle.primary)
    async def roll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.is_finished():
            return
        self.locked.append(self.pending)      # lock the current (latest) roll
        if len(self.locked) >= self.n:        # that was the final → submit
            for c in self.children:
                c.disabled = True
            self.stop()
            res = self.bot.engine.competition_submit(
                str(interaction.user.id), self.who, self.locked)
            total = res.get("total", sum(self.locked))
            try:
                await interaction.response.edit_message(
                    content=f"✅ Locked in: {', '.join(f'{v:g}' for v in self.locked)} → **{total:g}**", view=self)
            except Exception:  # noqa: BLE001
                pass
            if res.get("ok") and res.get("summary"):
                await self.bot.broadcast(res["summary"], None)   # per-player, all at once
            return
        self.pending = self.bot.engine.competition_roll_value(len(self.locked))  # next slot
        self._sync_buttons()
        await interaction.response.edit_message(content=self.text(), view=self)

    @discord.ui.button(label="🔄 Reroll", style=discord.ButtonStyle.secondary)
    async def reroll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.is_finished() or self.rerolls_left <= 0:
            return
        self.rerolls_left -= 1
        self.pending = self.bot.engine.competition_roll_value(len(self.locked))  # reroll the current slot only
        self._sync_buttons()
        await interaction.response.edit_message(content=self.text(), view=self)


class WinnerButtonView(discord.ui.View):
    """A one-press prize button handed to a competition winner (or any target).
    Only the target may press it; the press runs the button's mini action block
    once, then the button greys out. Non-targets get a private 'not for you'."""

    def __init__(self, bot, meta):
        super().__init__(timeout=None)
        self.bot = bot
        self.meta = meta or {}
        btn = discord.ui.Button(label=(self.meta.get("label") or "🎁 Claim your prize")[:80],
                                style=discord.ButtonStyle.success)
        btn.callback = self._press
        self.add_item(btn)

    async def _press(self, interaction: discord.Interaction):
        eng = self.bot.engine
        uid = str(interaction.user.id)
        if not eng.winner_button_can_press(uid):
            await interaction.response.send_message(
                "🚫 This prize isn't yours to claim.", ephemeral=True)
            return
        for c in self.children:      # grey it the instant it's claimed
            c.disabled = True
        self.stop()
        try:
            await interaction.response.edit_message(view=self)
        except Exception:  # noqa: BLE001 — expired/double-ack
            pass
        await eng.press_winner_button(uid, interaction.user.display_name)


class BonusRoundView(discord.ui.View):
    """A teamwork Bonus Round's confirm button. Only players holding a banked
    bonus can press; once the needed holders confirm, the round's action block
    runs (pooling everyone's [total_bonus_*]). Non-holders get a private notice."""

    def __init__(self, bot, meta):
        super().__init__(timeout=None)
        self.bot = bot
        self.meta = meta or {}
        btn = discord.ui.Button(label="🤝 Confirm bonus", style=discord.ButtonStyle.success)
        btn.callback = self._press
        self.add_item(btn)

    async def _press(self, interaction: discord.Interaction):
        eng = self.bot.engine
        uid = str(interaction.user.id)
        if not eng.bonus_round_can_press(uid):
            await interaction.response.send_message(
                "🚫 You have no banked bonus to contribute to this round.", ephemeral=True)
            return
        res = await eng.bonus_round_press(uid, interaction.user.display_name)
        if not res.get("ok"):
            await interaction.response.send_message("🚫 This round has ended.", ephemeral=True)
            return
        try:
            if res.get("activated"):
                for c in self.children:
                    c.disabled = True
                self.stop()
                await interaction.response.send_message("✅ Confirmed — bonus cashed in!", ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"✅ Confirmed ({res.get('have')}/{res.get('need')}). Waiting on the rest…",
                    ephemeral=True)
        except Exception:  # noqa: BLE001
            pass


class BotManager:
    def __init__(self, engine, get_config) -> None:
        self.engine = engine
        self.get_config = get_config
        self._client: discord.Client | None = None
        self._task: asyncio.Task | None = None
        self._auto_task: asyncio.Task | None = None
        self._token: str | None = None
        self.last_error: str | None = None
        # replace_key -> {channel_id: Message} for loop events with clean_previous,
        # so the next round can delete the message it replaces. Bounded (see _track_msg).
        self._loop_msgs: "dict[str, dict[str, discord.Message]]" = {}
        # Every live minigame View (Play buttons + the ephemeral games behind
        # them), so a session pause can cancel them all. Views are pruned once
        # finished; a pause disables + refunds whatever is still live.
        self._active_views: set = set()

    # -- minigame view registry ---------------------------------------------- #
    def _register_view(self, view) -> None:
        self._active_views = {v for v in self._active_views if not v.is_finished()}
        self._active_views.add(view)

    async def cancel_all_games(self) -> None:
        """Session pause: stop every live game view, grey its buttons, tell the
        player, and refund the use+cooldown their command charged (once per
        player+command, even when a Play button and its game are both live)."""
        views, self._active_views = list(self._active_views), set()
        refunded = set()
        for v in views:
            if v.is_finished():
                continue
            v.stop()
            for c in v.children:
                c.disabled = True
            uid, cmd = getattr(v, "uid", None), getattr(v, "cmd", None) or {}
            key = (str(uid), (cmd.get("name") or "").strip().lower())
            if uid is not None and key[1] and key not in refunded:
                refunded.add(key)
                self.engine.refund_use(uid, key[1])
            note = "⏸️ Session paused — game cancelled (your use was refunded)."
            try:
                msg = getattr(v, "message", None)
                if msg is not None:
                    await msg.edit(content=note, view=v)
                elif getattr(v, "_interaction", None) is not None:
                    await v._interaction.edit_original_response(content=note, view=v)
            except Exception as e:  # noqa: BLE001
                self.engine._log("error", f"couldn't grey a cancelled game: {e}")

    # -- lifecycle ----------------------------------------------------------- #
    def _alive(self) -> bool:
        """True only if the client task is actually still running."""
        return self._task is not None and not self._task.done()

    async def ensure(self, token: str | None, force: bool = False) -> None:
        token = (token or "").strip() or None
        # Reconnect if forced, if the token changed, or if the last attempt died
        # (e.g. it failed because Message Content Intent wasn't enabled yet).
        if not force and token == self._token and self._alive():
            return
        await self.stop()
        self._token = token
        if not token:
            return
        self.last_error = None
        self._client = self._build_client()
        self._task = asyncio.create_task(self._runner(token))
        self._auto_task = asyncio.create_task(self._auto_loop())

    async def reconnect(self) -> None:
        await self.ensure(self._token, force=True)

    async def stop(self) -> None:
        for task in (self._auto_task, self._task):
            if task is not None:
                task.cancel()
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        self._client = None
        self._task = None
        self._auto_task = None
        self.engine.bot_connected = False

    async def _runner(self, token: str) -> None:
        delay = 30
        while True:
            try:
                await self._client.start(token)
                return
            except asyncio.CancelledError:
                return
            except discord.LoginFailure:
                self.last_error = "invalid bot token"
                self.engine._log("error", "Discord login failed: invalid token")
                self.engine.bot_connected = False
                return   # a bad token won't fix itself — wait for the user
            except discord.PrivilegedIntentsRequired:
                self.last_error = "enable the Message Content Intent in the Developer Portal"
                self.engine._log("error", "Discord: Message Content Intent not enabled")
                self.engine.bot_connected = False
                return
            except Exception as e:  # noqa: BLE001
                # Network-shaped failure (offline at launch, DNS blip, Discord
                # outage): keep retrying with backoff instead of staying dead.
                self.last_error = f"{e} — retrying in {delay}s"
                self.engine._log("error", f"Discord client error: {e} — retrying in {delay}s")
                self.engine.bot_connected = False
                try:
                    await self._client.close()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(delay)
                delay = min(300, delay * 2)
                self._client = self._build_client()

    # -- wiring -------------------------------------------------------------- #
    def _build_client(self) -> discord.Client:
        intents = discord.Intents.default()
        intents.message_content = True  # required to read "!roll" text
        client = discord.Client(intents=intents)

        # /vote — the silent ballot: the invocation never appears in chat and
        # the confirmation is ephemeral. Fixed name (slash names are registered
        # with Discord; the renameable !agvote text command still works too).
        tree = discord.app_commands.CommandTree(client)
        self._tree = tree

        @tree.command(name="vote", description="Vote in the running poll (only you see the confirmation)")
        @discord.app_commands.describe(option="The option number to vote for")
        async def slash_vote(interaction: discord.Interaction,
                             option: discord.app_commands.Range[int, 1, 4]):
            await self.handle_vote_interaction(interaction, int(option))

        @client.event
        async def on_ready():
            self.engine.bot_connected = True
            self.engine._log("bot", f"connected as {client.user}")
            # Per-guild sync is instant (global registration can take up to an
            # hour, so copy into each guild the bot is in).
            try:
                for g in client.guilds:
                    tree.copy_global_to(guild=g)
                    await tree.sync(guild=g)
                self.engine._log("bot", f"/vote slash command synced to {len(client.guilds)} server(s)")
            except Exception as e:  # noqa: BLE001 — slash sync failing must not kill the bot
                self.engine._log("error", f"slash command sync failed: {e}")

        @client.event
        async def on_disconnect():
            self.engine.bot_connected = False

        @client.event
        async def on_resumed():
            # a transient gateway blip RESUMEs without a fresh on_ready — the
            # dashboard pill used to show "disconnected" forever after one
            self.engine.bot_connected = True

        @client.event
        async def on_message(message: discord.Message):
            await self._handle(client, message)

        return client

    async def announce(self, text: str, image: str | None = None, replace_key: str | None = None) -> None:
        """Called by the engine to post events/milestones — broadcast to every
        listen channel across all servers (plus the announce channel)."""
        await self.broadcast(text, image, replace_key=replace_key)

    async def _auto_loop(self) -> None:
        """Post the capacity/commands report every auto_report.seconds."""
        elapsed = 0
        try:
            while True:
                await asyncio.sleep(5)
                try:
                    cfg = self.get_config()
                    ar = cfg.get("auto_report", {})
                    # Stop reporting when the listener is off (e.g. after an End
                    # Sequence deactivates the session) — no game, no announcements.
                    if not ar.get("enabled") or not cfg.get("listener_enabled") or self.engine.paused:
                        elapsed = 0
                        continue
                    if not self._client or not self._client.is_ready():
                        continue
                    try:
                        period = max(15, int(float(ar.get("seconds") or 300)))
                    except (TypeError, ValueError):
                        period = 300
                    elapsed += 5
                    if elapsed >= period:
                        elapsed = 0
                        txt = self.engine.auto_report_text(cfg.get("command_prefix", "!"))
                        await self.broadcast(txt, None, embed=self._status_embed("auto", txt))
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — one bad tick must not kill the loop
                    self.engine._log("error", f"auto-report: {e}")
        except asyncio.CancelledError:
            pass

    # -- command handling ---------------------------------------------------- #
    def list_guilds(self) -> list[dict]:
        """Servers the bot is in, with their text channels (for the UI picker)."""
        if not self._client or not self._client.is_ready():
            return []
        out = []
        for g in self._client.guilds:
            chans = [{"id": str(c.id), "name": c.name} for c in g.text_channels]
            out.append({"id": str(g.id), "name": g.name, "channels": chans})
        out.sort(key=lambda x: x["name"].lower())
        return out

    def invite_url(self) -> str | None:
        """OAuth2 invite URL for this bot, once we know its application id."""
        if not self._client or not self._client.is_ready():
            return None
        app_id = self._client.application_id or (self._client.user and self._client.user.id)
        if not app_id:
            return None
        # View Channels + Send Messages + Embed Links + Attach Files + Read History
        perms = 1024 | 2048 | 16384 | 32768 | 65536
        return (
            "https://discord.com/api/oauth2/authorize"
            f"?client_id={app_id}&permissions={perms}&scope=bot%20applications.commands"
        )

    def _targets(self, cfg: dict) -> list[dict]:
        """Every channel the bot listens/broadcasts in. Uses listen_targets if
        set, else falls back to the single legacy listen_guild/channel."""
        targets = [t for t in (cfg.get("listen_targets") or [])
                   if str(t.get("guild_id") or "").strip() and str(t.get("channel_id") or "").strip()]
        if targets:
            return targets
        gid = str(cfg.get("listen_guild_id") or "").strip()
        cid = str(cfg.get("listen_channel_id") or "").strip()
        if gid and cid:
            return [{"guild_id": gid, "channel_id": cid}]
        return []

    def _allowed(self, cfg: dict, message: discord.Message) -> bool:
        uids = (cfg.get("allow", {}) or {}).get("user_ids") or []

        # Direct messages: opt-in, and still subject to the user allowlist.
        if message.guild is None:
            if not cfg.get("allow_dms"):
                return False
            if uids and message.author.id not in _ints(uids):
                return False
            return True

        # Guild: allow ONLY in an explicitly-selected channel. No channel picked
        # for a server means silent there (never "all channels").
        gid, cid = str(message.guild.id), str(message.channel.id)
        if not any(str(t["guild_id"]) == gid and str(t["channel_id"]) == cid for t in self._targets(cfg)):
            return False
        if uids and message.author.id not in _ints(uids):
            return False
        return True

    async def _channel(self, cid):
        if not self._client or not self._client.is_ready():
            return None
        try:
            cid_int = int(cid)
        except (TypeError, ValueError):
            return None
        ch = self._client.get_channel(cid_int)
        if ch is None:
            try:
                ch = await self._client.fetch_channel(cid_int)
            except Exception:
                return None
        return ch

    async def _send(self, ch, text: str, image: str | None, embed=None, view=None):
        """Send one message; returns the sent discord.Message (or None on failure).
        When `embed` is given the text is carried as the embed (rich card) instead."""
        text = _clip(text)
        image = _resolve_img(image)
        try:
            if embed is not None:
                if view is not None:
                    return await ch.send(embed=embed, view=view)
                return await ch.send(embed=embed)
            if image and image.lower().startswith(("http://", "https://")):
                return await ch.send(_clip(f"{text}\n{image}" if text else image))
            elif image and os.path.exists(image):
                return await ch.send(content=text or None, file=discord.File(image))
            elif text:
                return await ch.send(text)
        except Exception as e:  # noqa: BLE001
            self.engine._log("error", f"send failed: {e}")
        return None

    # Embed accent colors per status kind (a colored stripe helps them read apart).
    _EMBED_COLORS = {"capacity": 0x5865F2, "leaderboard": 0xF1C40F,
                     "leaderboard_life": 0xE67E22, "auto": 0x2ECC71,
                     "broadcast": 0x9B59B6}

    def _status_embed(self, kind: str, text: str, title: str | None = None):
        """Wrap a status/report block in a bordered, colored embed card. Returns None
        if rich_output is off or discord.Embed isn't available (falls back to text)."""
        if not self.get_config().get("rich_output"):
            return None
        try:
            e = discord.Embed(description=(text or "")[:4096],
                              color=self._EMBED_COLORS.get(kind, 0x5865F2))
            if title:
                e.title = title[:256]
            return e
        except Exception:  # noqa: BLE001
            return None

    def _track_msg(self, replace_key: str, cid: str, msg) -> None:
        """Remember the message posted for (replace_key, channel) so the next round
        can delete it. Bound the map so long-running sessions don't leak entries."""
        self._loop_msgs.setdefault(replace_key, {})[cid] = msg
        if len(self._loop_msgs) > 40:  # drop the oldest run's tracking (not the messages)
            self._loop_msgs.pop(next(iter(self._loop_msgs)), None)

    async def _delete_tracked(self, replace_key: str, cid: str) -> None:
        prev = self._loop_msgs.get(replace_key, {}).pop(cid, None)
        if prev is not None:
            try:
                await prev.delete()
            except Exception:  # noqa: BLE001 — message already gone / no perms
                pass

    async def broadcast(self, text: str, image: str | None = None, exclude_channel_id=None,
                        replace_key: str | None = None, embed=None, view=None) -> None:
        """Post to every listen channel (across all servers). Used for events,
        milestones, snapshots, and cross-server echoes. When `replace_key` is set
        (a clean_previous loop round), the prior round's message in each channel is
        deleted before the new one is posted. `embed` posts a rich card instead."""
        cfg = self.get_config()
        chan_ids = {str(t["channel_id"]) for t in self._targets(cfg)}
        # The optional announce channel gets every broadcast too (auto-reports,
        # milestones, events, pause/resume) — deduped if it's also a listen target.
        ann = str(cfg.get("announce_channel_id") or "").strip()
        if ann:
            chan_ids.add(ann)
        if exclude_channel_id is not None:
            chan_ids.discard(str(exclude_channel_id))
        for cid in chan_ids:
            ch = await self._channel(cid)
            if ch is None:
                continue
            if replace_key:
                await self._delete_tracked(replace_key, cid)
            msg = await self._send(ch, text, image, embed=embed, view=view)
            if replace_key and msg is not None:
                self._track_msg(replace_key, cid, msg)

    # -- operator controls (dashboard buttons that act as the owner in-channel) --
    def _operator_ready(self, cfg) -> str | None:
        """None if operator Controls can run; else a short reason string."""
        if not cfg.get("listener_enabled"):
            return "activation is off"
        if not self._client or not self._client.is_ready():
            return "bot not connected"
        if not self._targets(cfg):
            return "no server/channel selected"
        return None

    def _bot_name(self) -> str:
        """The bot's own Discord name — used when no Owner is configured."""
        try:
            u = self._client.user if self._client else None
            return (getattr(u, "display_name", None) or u.name) if u else "the bot"
        except Exception:  # noqa: BLE001
            return "the bot"

    async def operator_roll(self, who: str, dice=None, sides=None) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        who = (who or "").strip() or self._bot_name()
        res = await self.engine.roll_and_fire(who, uid=None, dice=dice, sides=sides)
        if res.get("silent"):
            return {"ok": False, "error": "dice are disabled in this range"}
        if res.get("ok") and res.get("reply"):
            await self.broadcast(res["reply"], None)
        return res

    async def operator_pump(self, who: str, seconds: float) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        who = (who or "").strip() or self._bot_name()
        seconds = float(seconds)
        res = await self.engine.fire(seconds, reason=f"pump by {who}")
        if res.get("ok"):
            target = self.engine._active_id()
            default = ("**[secs]** seconds have been added to the pump timer, and will "
                       "increase [operator]'s volume by **+[secs2capacity]%**\n"
                       "Current Capacity: **[capacity]%** Remaining Pump Timer: **[timer]**s")
            tmpl = cfg.get("pump_message") or default
            # broadcast what was actually delivered (the hard cap may clamp it)
            actual = res.get("added") if res.get("added") is not None else seconds
            extra = {"secs": f"{actual:.1f}", "seconds": f"{actual:.1f}",
                     "secs2capacity": self.engine._secs_to_capacity(actual, target)}
            await self.broadcast(self.engine.render(tmpl, extra), None)
        return res

    async def operator_stop(self, who: str) -> dict:
        """STOP = pause the whole session. Deliberately NOT gated on
        _operator_ready: stopping the pump must work even with activation off or
        the bot down (the broadcast is simply best-effort then)."""
        who = (who or "").strip() or self._bot_name()
        return await self.engine.pause(who)

    async def operator_resume(self, who: str) -> dict:
        who = (who or "").strip() or self._bot_name()
        return await self.engine.resume(who)

    async def operator_start_poll(self, name: str) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        return self.engine.start_poll_bg(name, source="dashboard")

    async def operator_broadcast_capacity(self) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        rd, rs = self.engine.range_dice(self.engine.range_for(self.engine.capacity))
        default = "📊 Capacity **[capacity]%**\n[capacity_bar]\nRolling **[dice]** · [announce]"
        tmpl = cfg.get("capacity_message") or default
        msg = self.engine.render(tmpl, {"dice": f"{rd}d{rs}", "sides": rs}) or "📊"
        await self.broadcast(msg, None, embed=self._status_embed("capacity", msg))
        return {"ok": True}

    async def operator_broadcast_leaderboard(self) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        txt = self.engine.leaderboard_text()
        await self.broadcast(txt, None, embed=self._status_embed("leaderboard", txt))
        return {"ok": True}

    async def post_embed(self, title: str, text: str, options: list | None = None) -> None:
        """Broadcast a rich embed (polls). Always an embed — not gated on
        rich_output. When `options` (poll option labels) is given, the embed
        carries tap-to-vote buttons; each tap answers the voter EPHEMERALLY
        (only they see their choice) and fires the quiet vote broadcast."""
        try:
            e = discord.Embed(title=(title or "")[:256], description=(text or "")[:4096],
                              color=0x9B59B6)
        except Exception:  # noqa: BLE001 — no embed support → plain text
            await self.broadcast(f"**{title}**\n{text}", None)
            return
        view = PollVoteView(self, options) if options else None
        await self.broadcast("", None, embed=e, view=view)

    async def post_broadcast_embed(self, text: str) -> None:
        """A broadcast-preset action → a rich embed card (always an embed, like
        the operator Broadcast Custom's, regardless of rich_output)."""
        try:
            e = discord.Embed(description=(text or "")[:4096],
                              color=self._EMBED_COLORS.get("broadcast", 0x9B59B6))
        except Exception:  # noqa: BLE001 — no embed support → plain text
            await self.broadcast(text, None)
            return
        await self.broadcast("", None, embed=e)

    async def post_competition_embed(self, title: str, text: str, meta: dict) -> None:
        """Competition announcement: a rich embed with an 'Enter Challenge'
        button that opens each player's private roller."""
        try:
            e = discord.Embed(title=(title or "")[:256], description=(text or "")[:4096], color=0xE67E22)
        except Exception:  # noqa: BLE001
            await self.broadcast(f"**{title}**\n{text}", None)
            return
        await self.broadcast("", None, embed=e, view=CompetitionEnterView(self, meta))

    async def post_winner_button(self, title: str, text: str, meta: dict) -> None:
        """A one-press Winner Button embed — only the target can press it."""
        try:
            e = discord.Embed(title=(title or "")[:256], description=(text or "")[:4096], color=0xF1C40F)
        except Exception:  # noqa: BLE001
            await self.broadcast(f"**{title}**\n{text}", None)
            return
        await self.broadcast("", None, embed=e, view=WinnerButtonView(self, meta))

    async def post_bonus_round_embed(self, title: str, text: str, meta: dict) -> None:
        """A teamwork Bonus Round embed with a Confirm button for bonus holders."""
        try:
            e = discord.Embed(title=(title or "")[:256], description=(text or "")[:4096], color=0x2ECC71)
        except Exception:  # noqa: BLE001
            await self.broadcast(f"**{title}**\n{text}", None)
            return
        await self.broadcast("", None, embed=e, view=BonusRoundView(self, meta))

    async def handle_vote_interaction(self, interaction, option_number: int) -> None:
        """Shared by the vote buttons and the /vote slash command: cast the
        ballot, confirm privately (ephemeral), broadcast the quiet notice."""
        res = self.engine.cast_vote(str(interaction.user.id),
                                    interaction.user.display_name, str(option_number))
        try:
            if res is None:
                await interaction.response.send_message("🗳 No poll is running.", ephemeral=True)
                return
            if res.get("reply"):
                await interaction.response.send_message(res["reply"], ephemeral=True)
                return
            await interaction.response.send_message(
                f"🗳 You voted for **{res.get('label', '')}** — only you can see this.", ephemeral=True)
        except Exception as e:  # noqa: BLE001 — interaction expired / double-ack
            self.engine._log("error", f"vote interaction reply failed: {e}")
        if res and res.get("broadcast"):
            await self.broadcast(res["broadcast"], None)

    def _hdr(self, cfg: dict, label: str | None, name: str | None) -> str:
        """The **[label · name]** output-header prefix (empty unless output_headers
        is on). `name` is the actor name the destination is allowed to see, so the
        tag never leaks a real name where the body would show the anon label."""
        if not cfg.get("output_headers") or not label:
            return ""
        return f"**[{label} · {name}]** " if name else f"**[{label}]** "

    async def _broadcast_named(self, real: str, anon: str, uid, exclude_channel_id=None,
                               label: str | None = None, who: str | None = None) -> None:
        """Broadcast a per-player result: each destination shows the real name if
        the player is a member of that server, else the anonymized version (same
        cross-server rule as command echoes)."""
        cfg = self.get_config()
        try:
            author_id = int(uid) if uid else None
        except (TypeError, ValueError):
            author_id = None
        for t in self._targets(cfg):
            cid = str(t.get("channel_id") or "")
            if not cid or (exclude_channel_id and cid == str(exclude_channel_id)):
                continue
            ch = await self._channel(cid)
            if ch is None:
                continue
            show_real = bool(real) and author_id is not None and await self._is_member(getattr(ch, "guild", None), author_id)
            text = real if show_real else (anon or real)
            if text:
                await self._send(ch, self._hdr(cfg, label, who if show_real else _HDR_ANON) + text, None)

    async def game_payoff(self, cmd: dict, score, who: str, uid) -> None:
        """A minigame finished — fire the tier's devices (credited to the player)
        and broadcast the game-labeled result, respecting cross-server anonymity."""
        try:
            res = await self.engine.game_result(cmd, score, who, uid)
            label = self.engine.game_display_name(cmd)
            await self._broadcast_named(res.get("real"), res.get("anon"), uid, label=label, who=who)
            # events the game's start_events activated (activation lines + any
            # fire_immediately first rounds) follow the result
            for post in (res.get("events_posted") or []):
                if isinstance(post, dict):
                    await self.broadcast(post.get("text", ""), post.get("image"),
                                         replace_key=post.get("replace_key"))
                else:
                    await self.broadcast(post, None)
        except Exception as e:  # noqa: BLE001
            self.engine._log("error", f"game payoff failed: {e}")

    async def operator_broadcast_custom(self, message: str) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        text = (message or "").strip()
        if not text:
            return {"ok": False, "error": "no message selected"}
        rendered = self.engine.render(text)
        await self.broadcast(rendered, None, embed=self._status_embed("broadcast", rendered))
        return {"ok": True}

    async def _handle(self, client: discord.Client, message: discord.Message) -> None:
        if message.author.bot or (client.user and message.author.id == client.user.id):
            return
        cfg = self.get_config()
        prefix = cfg.get("command_prefix", "!")
        content = (message.content or "").strip()
        if not content.startswith(prefix):
            return
        cmd = content[len(prefix):].split(" ", 1)[0].lower()
        bn = self.engine.builtin_names()               # {roll,capacity,help,…} → names
        action = next((k for k, v in bn.items() if v == cmd), None)
        if action == "roll" and not cfg.get("roll_enabled", True):
            action = None  # dice disabled → ignore quietly
        custom = self.engine.find_command(cmd)
        if custom is not None and not custom.get("enabled", True):
            custom = None  # disabled command → ignore quietly
        prize = self.engine.find_prize_by_command(cmd)
        is_prize = prize is not None
        if action is None and custom is None and not is_prize:
            return
        if not self._allowed(cfg, message):
            return
        if not cfg.get("listener_enabled"):
            await _reply(message, "🔇 Activation is currently **off**.")
            return

        who = message.author.display_name

        # Anti-spam buffer: quietly ignore one PERSON spamming the SAME command
        # back-to-back. Keyed per-user, so different people are never buffered
        # against each other.
        bufkey = (prize.get("command", "").lower() if is_prize
                  else custom.get("name", "").lower() if custom is not None else action)
        if not self.engine.buffer_ok(f"buf:{bufkey}:{message.author.id}"):
            return

        if is_prize:
            res = await self.engine.use_prize_command(who, str(message.author.id), prize)
            if res.get("silent"):
                return  # not unlocked / used up → ignore quietly
            if not res.get("ok"):
                await _reply(message, res["error"] if res.get("paused")
                             else f"⚠️ {res.get('error', 'could not run')}")
                return
            await _reply(message, res["reply"])
            await self._echo(message, res.get("reply_anon"), "", res.get("reply"))
            return


        if action == "vote":
            # only live during a poll — cast_vote returns None otherwise (silent)
            parts = content[len(prefix):].split(None, 1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            res = self.engine.cast_vote(str(message.author.id), who, arg)
            if not res:
                return
            if res.get("reply"):                       # usage errors → just the voter's channel
                await _reply(message, res["reply"])
            if res.get("broadcast"):                   # quiet vote notice → everywhere
                await self.broadcast(res["broadcast"], None)
            return

        if action == "enter":
            # only live during a competition — enter_competition returns None otherwise
            reply = self.engine.enter_competition(str(message.author.id), who)
            if reply:
                await self.broadcast(reply, None)
            return

        if action == "help":
            text = self.engine.help_text(prefix)
            mention = message.author.mention   # <@id> — pings them in the channel
            try:
                await message.author.send(_clip(text))      # DM the command list
                if message.guild is not None:
                    await _reply(message, f"📬 {mention}, I've sent you a DM with the command list.")
            except discord.Forbidden:
                await _reply(message, f"⚠️ {mention}, I couldn't DM you — enable DMs from server members and try again.")
            except Exception as e:  # noqa: BLE001 — not a DM-privacy problem; say what happened
                self.engine._log("error", f"help DM failed: {e}")
                await _reply(message, f"⚠️ {mention}, couldn't send the command list: {e}")
            return

        if action == "leaderboard":
            txt = self.engine.leaderboard_text()
            await _reply(message, txt, embed=self._status_embed("leaderboard", txt))
            return

        if action == "leaderboard_life":
            txt = self.engine.leaderboard_life_text()
            await _reply(message, txt, embed=self._status_embed("leaderboard_life", txt))
            return

        if action == "pumptimer":
            tmpl = cfg.get("pumptimer_message") or "⏱️ [timer] seconds left on the pump timer."
            await _reply(message, self.engine.render(tmpl))
            return

        if action == "capacity":
            rd, rs = self.engine.range_dice(self.engine.range_for(self.engine.capacity))
            default = "📊 Capacity **[capacity]%**\n[capacity_bar]\nRolling **[dice]** · [announce]"
            tmpl = cfg.get("capacity_message") or default
            txt = self.engine.render(tmpl, {"dice": f"{rd}d{rs}", "sides": rs}) or "📊"
            await _reply(message, txt, embed=self._status_embed("capacity", txt))
            return

        if custom is not None:
            if custom.get("owner_only") and not self.engine.is_owner(message.author.id, who):
                return  # owner-only command → silently ignore for everyone else
            res = await self.engine.run_custom(custom, who, uid=str(message.author.id))
            if res.get("silent"):
                return  # gated out (wrong range) → ignore quietly
            if not res.get("ok"):
                # cooldown/out-of-uses/paused messages go through as-is; other errors get a ⚠️
                await _reply(message, res["error"] if (res.get("cooldown") or res.get("used_up") or res.get("paused"))
                             else f"⚠️ {res.get('error', 'could not run')}")
                return
            if res.get("game"):
                # Minigame: post the public Play button (locked to the author). The
                # game itself runs ephemerally; the result is broadcast at the end.
                glabel = self.engine.game_display_name(custom)
                intro = (res.get("reply") or "").strip() or f"🎮 **{who}** started **{custom.get('name')}** — press Play!"
                view = minigames.make_play_view(self, custom, who, str(message.author.id))
                try:
                    view.message = await message.channel.send(self._hdr(cfg, glabel, who) + intro, view=view)
                except Exception as e:  # noqa: BLE001
                    await _reply(message, f"⚠️ couldn't start the game: {e}")
                return
            line = res["reply"]
            tail = "\n⏳ (a fire is already running — ignored)" if (res.get("device") and not res.get("started")) else ""
            label = custom.get("name") or ""
            # react_only: acknowledge with a reaction instead of a text reply (spam
            # cut for rapid-fire commands). Cross-server echo is skipped (a reaction
            # is local); falls back to a normal reply if the emoji can't be added.
            reacted = False
            if custom.get("react_only"):
                emoji = (str(custom.get("react_emoji") or "").strip()) or "💨"
                try:
                    await message.add_reaction(emoji)
                    reacted = True
                except Exception as e:  # noqa: BLE001
                    self.engine._log("error", f"react failed ({emoji}): {e}")
            if not reacted:
                await _reply(message, self._hdr(cfg, label, who) + line + tail, as_reply=bool(custom.get("mention")))
                await self._echo(message, res.get("reply_anon"), tail, res.get("reply"), label=label)
            # Event activation / in-process / cooldown lines come AFTER the reply.
            # A clean_previous loop's first round arrives as a dict carrying its
            # replace_key so subsequent rounds can delete it; others are plain text.
            for post in (res.get("events_posted") or []):
                if isinstance(post, dict):
                    await self.broadcast(post.get("text", ""), post.get("image"),
                                         replace_key=post.get("replace_key"))
                else:
                    await self.broadcast(post, None)
            return

        # action == "roll"
        res = await self.engine.roll_and_fire(who, uid=str(message.author.id))
        if res.get("silent"):
            return  # dice disabled in this range → ignore quietly
        if not res.get("ok"):
            await _reply(message, res["error"] if (res.get("cooldown") or res.get("paused"))
                         else f"⚠️ {res.get('error', 'could not roll')}")
            return
        line = res["reply"]
        # No extend notice: the reply's [timer] already shows the new remaining
        # time, so a separate "added to the running fire" line would be redundant.
        roll_label = self.engine.builtin_names().get("roll") or "roll"
        await _reply(message, self._hdr(cfg, roll_label, who) + line)
        await self._echo(message, res.get("reply_anon"), "", res.get("reply"), label=roll_label)

    async def _is_member(self, guild, uid: int) -> bool:
        """True if user `uid` is a member of `guild`. Checks the member cache, then
        does a single fetch (works even without the Server Members intent), with a
        short TTL cache so repeated commands don't hammer the API."""
        if guild is None:
            return False
        if guild.get_member(uid) is not None:
            return True
        if not hasattr(self, "_member_cache"):
            self._member_cache = {}
        key = (guild.id, uid)
        now = time.monotonic()
        hit = self._member_cache.get(key)
        if hit is not None and now - hit[1] < 300:
            return hit[0]
        try:
            await guild.fetch_member(uid)
            ok = True
        except Exception:
            ok = False
        self._member_cache[key] = (ok, now)
        return ok

    async def _echo(self, message: discord.Message, anon_text: str | None, tail: str,
                    real_text: str | None = None, label: str | None = None) -> None:
        """Echo a copy of a reply to the OTHER listen channels so a shared game
        reads across servers. Each destination shows the actor's real name if they
        are a member of that server (they'd be visible there anyway); otherwise the
        anonymized version is used, so we never leak who/where to a server they
        aren't in."""
        if not (anon_text or real_text):
            return
        cfg = self.get_config()
        origin = str(message.channel.id) if message.guild is not None else None
        author_id = message.author.id
        for t in self._targets(cfg):
            cid = str(t.get("channel_id") or "")
            if not cid or cid == origin:
                continue
            ch = await self._channel(cid)
            if ch is None:
                continue
            show_real = bool(real_text) and await self._is_member(getattr(ch, "guild", None), author_id)
            text = real_text if show_real else anon_text
            if text:
                hdr = self._hdr(cfg, label, message.author.display_name if show_real else _HDR_ANON)
                await self._send(ch, (hdr + text + tail).strip(), None)


def _ints(xs) -> list[int]:
    out = []
    for x in xs:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


def _clip(text: str) -> str:
    """Discord hard-caps messages at 2000 chars — truncate instead of 400ing."""
    text = (text or "").strip()
    return text if len(text) <= 2000 else text[:1997] + "…"


async def _reply(message: discord.Message, text: str, as_reply: bool = False, embed=None) -> None:
    kwargs = {"embed": embed} if embed is not None else {"content": _clip(text)}
    try:
        if as_reply:
            await message.reply(**kwargs)      # a Discord reply — pings the author
        else:
            await message.channel.send(**kwargs)
    except Exception:
        # a reply can fail if the original message was deleted — fall back to send
        try:
            await message.channel.send(**kwargs)
        except Exception:
            pass
