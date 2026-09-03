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

import minigames

# Short token used for the actor name inside an output header when the destination
# isn't allowed to see the real name. Kept terse on purpose (the long
# anon_user_label sentence is for message bodies, not the compact [label · x] tag).
_HDR_ANON = "ANON"


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
        try:
            await self._client.start(token)
        except asyncio.CancelledError:
            pass
        except discord.LoginFailure:
            self.last_error = "invalid bot token"
            self.engine._log("error", "Discord login failed: invalid token")
            self.engine.bot_connected = False
        except discord.PrivilegedIntentsRequired:
            self.last_error = "enable the Message Content Intent in the Developer Portal"
            self.engine._log("error", "Discord: Message Content Intent not enabled")
            self.engine.bot_connected = False
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self.engine._log("error", f"Discord client error: {e}")
            self.engine.bot_connected = False

    # -- wiring -------------------------------------------------------------- #
    def _build_client(self) -> discord.Client:
        intents = discord.Intents.default()
        intents.message_content = True  # required to read "!roll" text
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            self.engine.bot_connected = True
            self.engine._log("bot", f"connected as {client.user}")

        @client.event
        async def on_disconnect():
            self.engine.bot_connected = False

        @client.event
        async def on_message(message: discord.Message):
            await self._handle(client, message)

        return client

    async def _announce_channel(self) -> discord.abc.Messageable | None:
        cfg = self.get_config()
        cid = str(cfg.get("announce_channel_id") or "").strip()
        if not cid or not self._client or not self._client.is_ready():
            return None
        try:
            cid_int = int(cid)
        except ValueError:
            return None
        ch = self._client.get_channel(cid_int)
        if ch is None:
            try:
                ch = await self._client.fetch_channel(cid_int)
            except Exception:
                return None
        return ch

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
                cfg = self.get_config()
                ar = cfg.get("auto_report", {})
                # Stop reporting when the listener is off (e.g. after an End
                # Sequence deactivates the session) — no game, no announcements.
                if not ar.get("enabled") or not cfg.get("listener_enabled"):
                    elapsed = 0
                    continue
                if not self._client or not self._client.is_ready():
                    continue
                elapsed += 5
                if elapsed >= max(15, int(ar.get("seconds", 300))):
                    elapsed = 0
                    await self.broadcast(self.engine.auto_report_text(cfg.get("command_prefix", "!")), None)
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

    async def _send(self, ch, text: str, image: str | None):
        """Send one message; returns the sent discord.Message (or None on failure)."""
        text = (text or "").strip()
        try:
            if image and image.lower().startswith(("http://", "https://")):
                return await ch.send((f"{text}\n{image}" if text else image))
            elif image and os.path.exists(image):
                return await ch.send(content=text or None, file=discord.File(image))
            elif text:
                return await ch.send(text)
        except Exception as e:  # noqa: BLE001
            self.engine._log("error", f"send failed: {e}")
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
                        replace_key: str | None = None) -> None:
        """Post to every listen channel (across all servers). Used for events,
        milestones, snapshots, and cross-server echoes. When `replace_key` is set
        (a clean_previous loop round), the prior round's message in each channel is
        deleted before the new one is posted."""
        cfg = self.get_config()
        chan_ids = {str(t["channel_id"]) for t in self._targets(cfg)}
        if exclude_channel_id is not None:
            chan_ids.discard(str(exclude_channel_id))
        for cid in chan_ids:
            ch = await self._channel(cid)
            if ch is None:
                continue
            if replace_key:
                await self._delete_tracked(replace_key, cid)
            msg = await self._send(ch, text, image)
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
            extra = {"secs": f"{seconds:.1f}", "seconds": f"{seconds:.1f}",
                     "secs2capacity": self.engine._secs_to_capacity(seconds, target)}
            await self.broadcast(self.engine.render(tmpl, extra), None)
        return res

    async def operator_stop(self, who: str) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        who = (who or "").strip() or self._bot_name()
        await self.engine.abort(f"stop by {who}")
        await self.broadcast(f"🛑 **{who}** stopped the pump.", None)
        return {"ok": True}

    async def operator_broadcast_capacity(self) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        rd, rs = self.engine.range_dice(self.engine.range_for(self.engine.capacity))
        default = "📊 Capacity **[capacity]%**\n[capacity_bar]\nRolling **[dice]** · [announce]"
        tmpl = cfg.get("capacity_message") or default
        msg = self.engine.render(tmpl, {"dice": f"{rd}d{rs}", "sides": rs}) or "📊"
        await self.broadcast(msg, None)
        return {"ok": True}

    async def operator_broadcast_leaderboard(self) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        await self.broadcast(self.engine.leaderboard_text(), None)
        return {"ok": True}

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
        await self.broadcast(self.engine.render(text), None)
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
                await _reply(message, f"⚠️ {res.get('error', 'could not run')}")
                return
            await _reply(message, res["reply"])
            await self._echo(message, res.get("reply_anon"), "", res.get("reply"))
            return


        if action == "help":
            text = self.engine.help_text(prefix)
            mention = message.author.mention   # <@id> — pings them in the channel
            try:
                await message.author.send(text)      # DM the command list
                if message.guild is not None:
                    await _reply(message, f"📬 {mention}, I've sent you a DM with the command list.")
            except Exception:
                await _reply(message, f"⚠️ {mention}, I couldn't DM you — enable DMs from server members and try again.")
            return

        if action == "leaderboard":
            await _reply(message, self.engine.leaderboard_text())
            return

        if action == "leaderboard_life":
            await _reply(message, self.engine.leaderboard_life_text())
            return

        if action == "pumptimer":
            tmpl = cfg.get("pumptimer_message") or "⏱️ [timer] seconds left on the pump timer."
            await _reply(message, self.engine.render(tmpl))
            return

        if action == "capacity":
            rd, rs = self.engine.range_dice(self.engine.range_for(self.engine.capacity))
            default = "📊 Capacity **[capacity]%**\n[capacity_bar]\nRolling **[dice]** · [announce]"
            tmpl = cfg.get("capacity_message") or default
            await _reply(message, self.engine.render(tmpl, {"dice": f"{rd}d{rs}", "sides": rs}) or "📊")
            return

        if custom is not None:
            if custom.get("owner_only") and not self.engine.is_owner(message.author.id, who):
                return  # owner-only command → silently ignore for everyone else
            res = await self.engine.run_custom(custom, who, uid=str(message.author.id))
            if res.get("silent"):
                return  # gated out (wrong range) → ignore quietly
            if not res.get("ok"):
                # cooldown + out-of-uses messages go through as-is; other errors get a ⚠️
                await _reply(message, res["error"] if (res.get("cooldown") or res.get("used_up"))
                             else f"⚠️ {res.get('error', 'could not run')}")
                return
            if res.get("game"):
                # Minigame: post the public Play button (locked to the author). The
                # game itself runs ephemerally; the result is broadcast at the end.
                glabel = self.engine.game_display_name(custom)
                intro = (res.get("reply") or "").strip() or f"🎮 **{who}** started **{custom.get('name')}** — press Play!"
                view = minigames.make_play_view(self, custom, who, str(message.author.id))
                try:
                    await message.channel.send(self._hdr(cfg, glabel, who) + intro, view=view)
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
            await _reply(message, res["error"] if res.get("cooldown")
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


async def _reply(message: discord.Message, text: str, as_reply: bool = False) -> None:
    try:
        if as_reply:
            await message.reply(text)      # a Discord reply — pings the author
        else:
            await message.channel.send(text)
    except Exception:
        # a reply can fail if the original message was deleted — fall back to send
        try:
            await message.channel.send(text)
        except Exception:
            pass
