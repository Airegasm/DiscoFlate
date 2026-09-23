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
import json
import os
import time
from collections import deque
import discord

import config_store
import minigames
import mp_games
import multiplayer as mp


def _install_id() -> str:
    """Which INSTALL this is: version + host. Stamped on the Activation-off
    notice, because that notice is the only thing a spare install on the same
    token ever says — and without a stamp two of them are indistinguishable
    from one, which is exactly the hole this fell into."""
    import json as _json
    import socket
    ver = "?"
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "version.json"), "r", encoding="utf-8") as fh:
            ver = str(_json.load(fh).get("version") or "?")
    except (OSError, ValueError):
        pass
    try:
        host = socket.gethostname()[:24]
    except Exception:  # noqa: BLE001
        host = "?"
    return f"v{ver} on {host}"


# Rich output is a property of the BOT, not of each call site. Wiring it at
# every send is how half the messages ended up plain: the games, the game
# intros, the pause notice, the Activation notice — each one a separate path
# somebody had to remember. _auto_embed() is consulted by the two chokepoints
# below instead, so a message is a card because rich output is on, full stop.
_RICH_CFG = None          # set by BotManager.__init__ -> () -> cfg dict


def _auto_embed(text, embed=None, image=None):
    """The card a plain message becomes when rich output is on. Returns the
    caller's own embed untouched when it has one, and None when there's
    nothing to wrap (an image post IS the picture; empty text is not a card)."""
    if embed is not None or image:
        return embed
    if not str(text or "").strip():
        return None
    try:
        if not (_RICH_CFG and (_RICH_CFG() or {}).get("rich_output")):
            return None
        return discord.Embed(description=str(text)[:4096], color=0x3BA55D)
    except Exception:  # noqa: BLE001 — never lose a message to a bad embed
        return None


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
        if res.get("wordle"):
            view = WordleView(self.bot, interaction.user.display_name,
                              str(interaction.user.id), res["rows"])
            await interaction.response.send_message(view.text(res["board"]),
                                                    view=view, ephemeral=True)
            return
        view = RollerView(self.bot, interaction.user.display_name, res["rolls"], res["rerolls"])
        await interaction.response.send_message(view.text(), view=view, ephemeral=True)


class WordleGuessModal(discord.ui.Modal, title="Your guess"):
    """Text entry for one guess. A modal rather than buttons because 26 letters
    don't fit on a component row, and typing is the game."""

    word = discord.ui.TextInput(label="Five-letter word", min_length=2,
                                max_length=12, placeholder="crane", required=True)

    def __init__(self, view):
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        await self._view.submit(interaction, str(self.word.value))


class WordleView(discord.ui.View):
    """A player's private board in a Wordle competition. Everyone races the
    SAME answer, so the grid stays ephemeral — a public board would hand the
    word to the rest of the field."""

    def __init__(self, bot, who, uid, rows):
        super().__init__(timeout=900)
        self.bot, self.who, self.uid, self.rows = bot, who, uid, rows

    def text(self, board, head=None):
        return (head or f"🟩 **Word race** — {self.rows} guesses.") + "\n" + board

    @discord.ui.button(label="✏️ Guess", style=discord.ButtonStyle.success)
    async def guess_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WordleGuessModal(self))

    async def submit(self, interaction: discord.Interaction, guess: str):
        r = self.bot.engine.wordle_guess(self.uid, self.who, guess)
        if not r.get("ok"):
            await interaction.response.send_message("🚫 " + r.get("error", "no"), ephemeral=True)
            return
        if r.get("done"):
            for c in self.children:
                c.disabled = True
            self.stop()
            head = (f"🎉 **Solved in {r['used']}** — worth **{r['score']:g}**."
                    if r.get("solved")
                    else f"❌ Out of guesses. It was **{r.get('word','').upper()}**.")
            await interaction.response.edit_message(content=self.text(r["board"], head), view=self)
            # the channel hears the RESULT, never the word — the rest of the
            # field is still racing it
            await self.bot.broadcast(
                f"🟩 **{self.who}** " + (f"solved it in **{r['used']}**!"
                                         if r.get("solved") else "ran out of guesses."), None)
            return
        left = r["rows"] - r["used"]
        await interaction.response.edit_message(
            content=self.text(r["board"], f"🟩 **Word race** — {left} guess{'' if left==1 else 'es'} left."),
            view=self)


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


def _does(t: dict, job: str) -> bool:
    """Does this channel do `job` ('listen' or 'announce')?

    Two independent tickboxes, both on by default. Older configs carried a
    single `active` (both jobs) and briefly an `announce_only` (announce
    without listen) — both are still honoured so nothing changes under anyone.
    """
    if job in t:
        return bool(t[job])
    if t.get("announce_only"):
        return job == "announce"
    return bool(t.get("active", True))


async def _ignore(interaction) -> None:
    """Acknowledge a click from somebody this button isn't for, and say
    nothing. Deferring stops Discord showing them "interaction failed" while
    putting no message in the channel — a match is watched, and an audience
    pressing things must not be able to fill the venue with refusals."""
    try:
        await interaction.response.defer()
    except Exception:  # noqa: BLE001 — a click we are ignoring anyway
        pass


class MpSpreadModal(discord.ui.Modal):
    """The number box. A modal rather than buttons because the answer is a
    quantity, and preset buttons would turn a judgement into a menu."""

    def __init__(self, view, side: str, gap: float):
        super().__init__(title="Your bet")
        self._view, self._side = view, side
        self.bet = discord.ui.TextInput(
            label=f"The gap is {gap:g}% now — where will it END?",
            placeholder="a number, e.g. 18", max_length=4, required=True)
        self.add_item(self.bet)

    async def on_submit(self, interaction: discord.Interaction):
        self._view.place(self._side, str(self.bet.value or ""))
        await interaction.response.send_message(
            f"Locked in: **{mp_games.spread_clamp(self.bet.value):g}%**",
            ephemeral=True)


class MpSpreadView(discord.ui.View):
    """Two buttons, one per player. Each opens a private number box.

    Bets stay HIDDEN until the round ends. If you could bet second and see
    their number you would simply bid one under it, which is the flaw in
    every 'closest without going over' game played in the open.
    """

    def __init__(self, bot, uids: dict, names: dict, gap: float, timeout: float):
        super().__init__(timeout=max(10.0, float(timeout)))
        self.bot = bot
        self.uids = {k: str(v or "") for k, v in uids.items()}
        self.names = dict(names)
        self.gap = float(gap)
        self.bets: dict = {}
        self.done = asyncio.Event()
        self.message = None
        for side in ("host", "guest"):
            b = discord.ui.Button(label=f"{self.names.get(side, side)} — place bet",
                                  style=discord.ButtonStyle.primary)
            b.callback = self._press(side)
            self.add_item(b)

    def _side(self, uid: str) -> str:
        for side, want in self.uids.items():
            if want and str(uid) == want:
                return side
        return ""

    def place(self, side: str, raw) -> None:
        self.bets[side] = mp_games.spread_clamp(raw)
        if len(self.bets) >= 2:
            self.done.set()

    def _press(self, side: str):
        async def cb(interaction: discord.Interaction):
            who = self._side(interaction.user.id)
            if who != side:
                await _ignore(interaction)        # not your button; say nothing
                return
            if side in self.bets:
                await _ignore(interaction)        # already placed
                return
            await interaction.response.send_modal(
                MpSpreadModal(self, side, self.gap))
        return cb


class MpCardsView(discord.ui.View):
    """Blackjack at a two-seat table. Both hands FACE UP, one dealer, one
    hidden hole card, and both players acting at once.

    Face-up is how a shoe game actually deals, and it is also what makes this
    hard rather than solved. Blackjack against a dealer has one right answer
    per total; but when you can see the other player standing on 20 while you
    hold 17, the right answer is "stand" and standing loses. Second place
    pays, so you have to hit into a bad spot.
    """

    def __init__(self, bot, uids: dict, names: dict, hands: dict, up: int,
                 timeout: float):
        super().__init__(timeout=max(15.0, float(timeout)))
        self.bot = bot
        self.uids = {k: str(v or "") for k, v in uids.items()}
        self.names = dict(names)
        self.hands = {k: list(v) for k, v in hands.items()}
        self.up = int(up)
        self.stood: set = set()
        self.done = asyncio.Event()
        self.message = None
        for side in ("me", "peer"):
            who = self.names.get(side, side)
            for label, stand in ((f"🃏 {who} hit", False), (f"✋ {who} stand", True)):
                b = discord.ui.Button(
                    label=label,
                    style=(discord.ButtonStyle.success if stand
                           else discord.ButtonStyle.primary))
                b.callback = self._press(side, stand)
                self.add_item(b)

    def _side(self, uid: str) -> str:
        for side, want in self.uids.items():
            if want and str(uid) == want:
                return side
        return ""

    def table(self, reveal=None) -> str:
        """The whole table as one block — this is what the room watches."""
        rows = []
        for side in ("me", "peer"):
            h = self.hands[side]
            t = mp_games.hand_total(h)
            mark = (" 💥" if t > 21 else (" ✋" if side in self.stood else ""))
            rows.append(f"**{self.names.get(side, side)}** — "
                        f"{mp_games.hand_text(h)} = **{t}**{mark}")
        if reveal is None:
            rows.append(f"**Dealer** — {('A' if self.up == 11 else self.up)}, 🂠")
        else:
            rows.append(f"**Dealer** — {mp_games.hand_text(reveal)} = "
                        f"**{mp_games.hand_total(reveal)}**"
                        + (" 💥" if mp_games.hand_total(reveal) > 21 else ""))
        return "\n".join(rows)

    def _finished(self, side: str) -> bool:
        return side in self.stood or mp_games.hand_total(self.hands[side]) > 21

    def _press(self, side: str, stand: bool):
        async def cb(interaction: discord.Interaction):
            if self._side(interaction.user.id) != side or self._finished(side):
                await _ignore(interaction)        # not yours, or you're out
                return
            if stand:
                self.stood.add(side)
            else:
                self.hands[side].append(mp_games.card_draw())
            for c in self.children:               # grey out a finished seat
                for s2 in ("me", "peer"):
                    if self._finished(s2) and self.names.get(s2, s2) in (c.label or ""):
                        c.disabled = True
            if all(self._finished(s2) for s2 in ("me", "peer")):
                self.done.set()
            try:
                await interaction.response.edit_message(
                    embed=self.bot._mp_card("🃏 Blackjack", self.table()), view=self)
            except Exception:  # noqa: BLE001
                pass
        return cb


class MpChoiceView(discord.ui.View):
    """A Player Choice: buttons only one named player may press.

    The HOST posts this, always — including the choices the guest's player
    makes — because a component interaction only ever reaches the app that owns
    the message. There is no way to delegate it and no reason to want to: the
    whole decision stays host-side and nothing about it touches the wire.
    """

    def __init__(self, bot, options, allow_uid: str, who: str, timeout: float):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.who = who
        self.allow_uid = str(allow_uid or "")
        self.picked: str | None = None
        self.done = asyncio.Event()
        self.message = None
        styles = {"danger": discord.ButtonStyle.danger,
                  "primary": discord.ButtonStyle.primary,
                  "success": discord.ButtonStyle.success,
                  "secondary": discord.ButtonStyle.secondary}
        for opt in options:
            b = discord.ui.Button(label=opt["label"],
                                  style=styles.get(opt["style"], discord.ButtonStyle.secondary))
            b.callback = self._press(opt["value"], opt["label"])
            self.add_item(b)

    def _press(self, value: str, label: str):
        async def cb(interaction: discord.Interaction):
            # Gated to ONE person. Anyone else is IGNORED — deferred, so
            # Discord doesn't show them "interaction failed", but told nothing.
            # A match has an audience, and an audience clicking things should
            # not be able to fill the channel with the bot telling them off.
            if self.allow_uid and str(interaction.user.id) != self.allow_uid:
                await _ignore(interaction)
                return
            if self.picked is not None:
                return
            self.picked = value
            for c in self.children:
                c.disabled = True
            try:
                await interaction.response.edit_message(view=self)
            except Exception:  # noqa: BLE001
                pass
            self.done.set()
            self.stop()
        return cb

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:  # noqa: BLE001
            pass
        self.done.set()


class MpReadyView(discord.ui.View):
    """Host Ready / Guest Ready on ONE embed, each button scoped to one person.

    The host posts it — a component interaction only ever reaches the app that
    owns the message, so the guest's player presses a button the HOST owns.
    That is not a limitation to work around: it means the whole ready exchange
    costs the wire nothing.
    """

    def __init__(self, bot, *, host_uid: str, guest_uid: str,
                 host_name: str, guest_name: str, timeout: float = 600.0):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.uids = {"host": str(host_uid or ""), "guest": str(guest_uid or "")}
        self.names = {"host": host_name or "the host", "guest": guest_name or "the guest"}
        self.ready = {"host": False, "guest": False}
        self.done = asyncio.Event()
        self.message = None
        for seat in ("host", "guest"):
            b = discord.ui.Button(label=f"{self.names[seat]} Ready",
                                  style=discord.ButtonStyle.success)
            b.callback = self._press(seat)
            self.add_item(b)

    def _press(self, seat: str):
        async def cb(interaction: discord.Interaction):
            want = self.uids.get(seat) or ""
            if want and str(interaction.user.id) != want:
                await _ignore(interaction)      # not yours; say nothing
                return
            self.ready[seat] = True
            for i, s2 in enumerate(("host", "guest")):
                if self.ready[s2] and i < len(self.children):
                    self.children[i].disabled = True
                    self.children[i].style = discord.ButtonStyle.secondary
            try:
                await interaction.response.edit_message(view=self)
            except Exception:  # noqa: BLE001
                pass
            if all(self.ready.values()):
                self.done.set()
                self.stop()
        return cb

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:  # noqa: BLE001
            pass
        self.done.set()


class MpDuelView(discord.ui.View):
    """One embed, both players, simultaneous pick.

    Every button is pressable by EITHER racer, once each, and what they chose
    stays hidden until both are in — an RPS where the second player can see the
    first one's move is not RPS. The host owns the message, as it owns every
    component, so nothing about this touches the wire.
    """

    def __init__(self, bot, options, uids: dict, names: dict, timeout: float):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.uids = {k: str(v or "") for k, v in uids.items()}   # side -> user id
        self.names = names
        self.picks: dict = {}                                    # side -> value
        self.done = asyncio.Event()
        self.message = None
        styles = {"danger": discord.ButtonStyle.danger,
                  "primary": discord.ButtonStyle.primary,
                  "success": discord.ButtonStyle.success,
                  "secondary": discord.ButtonStyle.secondary}
        for opt in options:
            b = discord.ui.Button(label=opt["label"],
                                  style=styles.get(opt["style"], discord.ButtonStyle.primary))
            b.callback = self._press(opt["value"], opt["label"])
            self.add_item(b)

    def _side(self, uid: str) -> str:
        for side, want in self.uids.items():
            if want and str(uid) == want:
                return side
        return ""

    def _press(self, value: str, label: str):
        async def cb(interaction: discord.Interaction):
            side = self._side(interaction.user.id)
            if not side:
                await _ignore(interaction)   # a viewer pressing it; say nothing
                return
            if side in self.picks:
                try:
                    await interaction.response.send_message(
                        f"You've already locked in **{self.picks[side]}**.", ephemeral=True)
                except Exception:  # noqa: BLE001
                    pass
                return
            self.picks[side] = value
            try:
                # ephemeral, so the other racer learns nothing from it
                await interaction.response.send_message(
                    f"🔒 Locked in **{label}**.", ephemeral=True)
            except Exception:  # noqa: BLE001
                pass
            if len(self.picks) >= 2:
                for c in self.children:
                    c.disabled = True
                try:
                    if self.message is not None:
                        await self.message.edit(view=self)
                except Exception:  # noqa: BLE001
                    pass
                self.done.set()
                self.stop()
        return cb

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:  # noqa: BLE001
            pass
        self.done.set()


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
        # Chat tab: rolling per-channel message log for the watched channels
        # (fed by on_message — the bot's own posts included) + which channels
        # already got a one-time history backfill.
        self._chat_logs: dict[str, deque] = {}
        self._chat_hist: set = set()
        # who has already been told Activation is off during THIS off period —
        # cleared the moment a command runs again
        self._off_told: set = set()
        global _RICH_CFG          # the two senders ask this, not each caller
        _RICH_CFG = self.get_config
        # Owner voice: per-channel webhook (posts AS the owner — their name +
        # avatar) and the cached owner (avatar url, display name).
        self._webhooks: dict = {}
        self._owner_ident: dict = {}
        # Multiplayer: the live mp.Session (None until the bot knows its own
        # id — every address in the protocol is a bot user id), the clock task,
        # a rolling log for the panel, and a match found on disk at boot that is
        # waiting for the operator to confirm the rejoin. Resume is NEVER silent.
        self.link: "mp.Session | None" = None
        self._rounds_task: asyncio.Task | None = None
        self._start_task: asyncio.Task | None = None
        self._mp_curtain_sid = ""        # the match this curtain was dropped for
        self._mp_begun_sid = ""          # match whose start has been handled
        self._mp_ended_sid = ""          # match whose End Condition has run
        self.mp_activate_cb = None       # the HOST's session start, parked here
        self._ready_view = None        # the live Host/Guest Ready embed
        self._link_task: asyncio.Task | None = None
        self._link_notes: deque = deque(maxlen=60)
        self._link_resume: dict | None = None

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
        self._link_task = asyncio.create_task(self._link_loop())
        self._link_find_resume()

    async def reconnect(self) -> None:
        await self.ensure(self._token, force=True)

    async def stop(self) -> None:
        for task in (self._auto_task, self._link_task, self._task):
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
        self._link_task = None
        # The Session is keyed to the bot user id; a token change makes a new
        # install, so drop it rather than carry a stale identity across.
        self.link = None
        self.engine.mp_ctx_cb = None
        for t in ("_rounds_task", "_start_task"):
            task = getattr(self, t, None)
            if task is not None:
                task.cancel()
                setattr(self, t, None)
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
        listen channel across all servers (plus the announce channel).

        With `rich_output` on these go out as embed cards. That toggle used to
        reach only the status/report blocks, so a config whose action rows were
        all plain `message` rows — which every shipped one is — produced a wall
        of bare text however rich you'd asked for. A row that sets `style:
        embed` still gets one with the toggle off; this is the default, not an
        override. An image post stays plain: the picture IS the message."""
        embed = None if image else self._status_embed("command", text)
        await self.broadcast(text, image, replace_key=replace_key, embed=embed)

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
                        are = None
                        if ar.get("embed"):
                            try:
                                ttl = self.engine.render((ar.get("title") or "").strip()) or "📊 Auto-report"
                                are = discord.Embed(title=ttl[:256], description=(txt or "")[:4096],
                                                    color=self._EMBED_COLORS.get("auto", 0x2ECC71))
                            except Exception:  # noqa: BLE001
                                are = None
                        await self.broadcast(txt, None, embed=are or self._status_embed("auto", txt))
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
            chans = ([{"id": str(c.id), "name": c.name, "kind": "text"} for c in g.text_channels]
                     + [{"id": str(c.id), "name": c.name, "kind": "voice"} for c in g.voice_channels]
                     + [{"id": str(c.id), "name": c.name, "kind": "voice"} for c in g.stage_channels])
            out.append({"id": str(g.id), "name": g.name, "channels": chans})
        out.sort(key=lambda x: x["name"].lower())
        return out

    async def set_avatar(self, data: bytes) -> None:
        """Set the bot's Discord profile picture. Raises RuntimeError if the bot
        isn't connected; discord.HTTPException on a rate limit / invalid image."""
        if not self._client or not self._client.is_ready() or self._client.user is None:
            raise RuntimeError("the bot isn't connected — connect it first")
        await self._client.user.edit(avatar=data)

    def invite_url(self, minimal: bool = False) -> str | None:
        """OAuth2 invite URL for this bot, once we know its application id.
        `minimal` omits Manage Webhooks — for servers whose admins won't grant
        it; the Chat tab's owner voice then falls back to a '**Owner:** …' bot
        post there. Everything else works identically."""
        if not self._client or not self._client.is_ready():
            return None
        app_id = self._client.application_id or (self._client.user and self._client.user.id)
        if not app_id:
            return None
        # View Channels + Send Messages + Embed Links + Attach Files + Read History
        perms = 1024 | 2048 | 16384 | 32768 | 65536
        if not minimal:
            perms |= 536870912   # + Manage Webhooks (the Chat tab's owner voice)
        return (
            "https://discord.com/api/oauth2/authorize"
            f"?client_id={app_id}&permissions={perms}&scope=bot%20applications.commands"
        )

    def _targets_raw(self, cfg: dict) -> list[dict]:
        """Every channel the bot listens in. Uses listen_targets if set, else
        falls back to the single legacy listen_guild/channel. NEVER narrowed by
        Isolate — the bot must keep hearing commands everywhere (and the Chat
        tab must keep listing every channel so you can switch/un-isolate)."""
        # `active` defaults to True so configs written before the tickbox
        # existed keep working; unticking one mutes that channel entirely —
        # no listening, no posting — without forgetting it.
        configured = [t for t in (cfg.get("listen_targets") or [])
                      if str(t.get("guild_id") or "").strip()
                      and str(t.get("channel_id") or "").strip()]
        if configured:
            # unticking BOTH jobs means SILENT — never fall through to the
            # legacy single channel, which would resurrect a surprise target
            return [t for t in configured
                    if _does(t, "listen") or _does(t, "announce")]
        gid = str(cfg.get("listen_guild_id") or "").strip()
        cid = str(cfg.get("listen_channel_id") or "").strip()
        if gid and cid:
            return [{"guild_id": gid, "channel_id": cid}]
        return []

    def _targets(self, cfg: dict) -> list[dict]:
        """As above, minus bot_network. That channel is plumbing, not a venue:
        if it ever leaked into the target list the protocol's own envelopes
        would show up in the Chat tab and be offered as command sources."""
        net = self._mp_chan(cfg, "bot_network")
        rows = self._targets_raw(cfg)
        return [t for t in rows if str(t.get("channel_id") or "") != net] if net else rows

    @staticmethod
    def _muted(cfg: dict) -> set:
        """Channel ids doing neither job — silenced everywhere."""
        return {str(t.get("channel_id"))
                for t in (cfg.get("listen_targets") or [])
                if not _does(t, "listen") and not _does(t, "announce")}

    def _isolated_to(self, cfg: dict) -> str | None:
        """The channel every broadcast is pinned to, or None. Fails open when
        the pinned channel isn't a live target (never silence everything)."""
        iso = str(cfg.get("chat_isolate_channel") or "").strip()
        if not (cfg.get("chat_isolate") and iso):
            return None
        known = {str(t.get("channel_id")) for t in self._targets(cfg)}
        return iso if iso in known else None

    def _listen_targets(self, cfg: dict) -> list[dict]:
        """Channels the bot accepts COMMANDS from."""
        return [t for t in self._targets(cfg) if _does(t, "listen")]

    def _broadcast_targets(self, cfg: dict) -> list[dict]:
        """Where broadcasts (events, milestones, echoes, owner voice) go. This
        is the ONLY place Isolate applies: pinned → just that channel, across
        every server."""
        targets = [t for t in self._targets(cfg) if _does(t, "announce")]
        iso = self._isolated_to(cfg)
        if iso is None:
            return targets
        hit = [t for t in targets if str(t.get("channel_id")) == iso]
        # the pinned channel may be the announce channel (not a listen target)
        return hit or [{"guild_id": "", "channel_id": iso}]

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
        if not any(str(t["guild_id"]) == gid and str(t["channel_id"]) == cid
                   for t in self._listen_targets(cfg)):
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
        # a view (a Play button) is content in its own right; everything else
        # empty means there is no message to send
        if embed is None and view is None and not image and not str(text or "").strip():
            return None
        embed = _auto_embed(text, embed, image)
        try:
            if embed is not None:
                kw = {"embed": embed}
                if view is not None:
                    kw["view"] = view
                if image and not image.lower().startswith(("http://", "https://")) \
                        and os.path.exists(image):
                    kw["file"] = discord.File(image)   # shows inside the embed via attachment://
                return await ch.send(**kw)
            if image and image.lower().startswith(("http://", "https://")):
                return await ch.send(_clip(f"{text}\n{image}" if text else image))
            elif image and os.path.exists(image):
                return await ch.send(content=text or None, file=discord.File(image))
            elif text:
                return await ch.send(text, **({"view": view} if view is not None else {}))
            elif view is not None:
                return await ch.send(view=view)   # a button with no words is still a post
        except Exception as e:  # noqa: BLE001
            self.engine._log("error", f"send failed: {e}")
        return None

    # Embed accent colors per status kind (a colored stripe helps them read apart).
    _EMBED_COLORS = {"capacity": 0x5865F2, "leaderboard": 0xF1C40F,
                     "leaderboard_life": 0xE67E22, "auto": 0x2ECC71,
                     "broadcast": 0x9B59B6, "command": 0x3BA55D}

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

    def _cfg_embed(self, cfg, key: str, text: str, default_title: str, kind: str = "capacity"):
        """Per-message embed toggle: when cfg[f"{key}_embed"] is on, wrap `text`
        in an embed titled cfg[f"{key}_title"] (blank = default_title). Works
        independently of rich_output; returns None when the toggle is off."""
        if not cfg.get(f"{key}_embed"):
            return None
        try:
            ttl = self.engine.render((cfg.get(f"{key}_title") or "").strip()) or default_title
            return discord.Embed(title=ttl[:256], description=(text or "")[:4096],
                                 color=self._EMBED_COLORS.get(kind, 0x5865F2))
        except Exception:  # noqa: BLE001
            return None

    def _out_embed(self, cfg, label: str, who: str, body: str):
        """One command, one embed. `rich_output` decides; the title carries the
        command and who ran it, which is what the **[label · name]** header did
        when it rode on top of the first of several plain posts.

        Returns None when rich output is off, and _reply then posts the header
        + body as plain text exactly as before."""
        if not cfg.get("rich_output") or not str(body or "").strip():
            return None
        try:
            ttl = f"{label} · {who}" if (label and who) else (label or who or "​")
            return discord.Embed(title=ttl[:256], description=str(body)[:4096],
                                 color=self._EMBED_COLORS.get("command", 0x5865F2))
        except Exception:  # noqa: BLE001 — never lose a reply to a bad embed
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
        chan_ids = {str(t["channel_id"]) for t in self._broadcast_targets(cfg)}
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

    # -- Chat tab (panel-side owner cockpit) ---------------------------------- #
    def _chat_watched(self, cfg) -> dict:
        """{channel_id: label} for every watched channel."""
        out = {}
        for t in self._targets(cfg):
            out[str(t["channel_id"])] = (f'{t.get("guild_name") or "?"} · '
                                         f'#{t.get("channel_name") or t["channel_id"]}'
                                         + ("" if _does(t, "listen") else " · announce only"))
        return out

    def _chan_kind(self, cid: str) -> str:
        """'voice' when the id resolves to a voice/stage channel (video-capable),
        else 'text'. Cache-only lookup; unknown → text."""
        try:
            ch = self._client.get_channel(int(cid)) if self._client else None
            if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                return "voice"
        except Exception:  # noqa: BLE001
            pass
        return "text"

    def chat_channels(self) -> list:
        return [{"channel_id": cid, "label": lbl, "kind": self._chan_kind(cid)}
                for cid, lbl in self._chat_watched(self.get_config()).items()]

    def _chat_entry(self, message) -> dict:
        """One Chat-tab row. Embeds travel STRUCTURED so the panel can draw the
        card Discord draws — they used to be squashed to a "▧ title · body"
        line, which lost the shape entirely and went blank-ish for an embed
        with no description."""
        # clean_content resolves <@id> / <#id> / <@&id> to readable names; the
        # raw form leaked user IDs into the panel
        try:
            text = (message.clean_content or "").strip()
        except Exception:  # noqa: BLE001
            text = (message.content or "").strip()
        embeds = []
        for e in (message.embeds or [])[:4]:
            try:
                col = e.color.value if getattr(e, "color", None) is not None else None
            except Exception:  # noqa: BLE001
                col = None
            fields = []
            for f in (getattr(e, "fields", None) or [])[:12]:
                fields.append({"name": (f.name or "")[:256],
                               "value": (f.value or "")[:1024],
                               "inline": bool(getattr(f, "inline", False))})
            foot = getattr(e, "footer", None)
            auth = getattr(e, "author", None)
            embeds.append({
                "title": ((e.title or "").strip() if e.title else "")[:256],
                "description": ((e.description or "").strip() if e.description else "")[:2000],
                "color": col, "fields": fields,
                "footer": (getattr(foot, "text", "") or "")[:256] if foot else "",
                "author": (getattr(auth, "name", "") or "")[:256] if auth else "",
                "image": bool(getattr(getattr(e, "image", None), "url", None)),
            })
        if message.attachments:
            text = (text + "\n" if text else "") + f"[{len(message.attachments)} attachment(s)]"
        return {"id": str(message.id),
                "t": message.created_at.strftime("%H:%M:%S"),
                "author": getattr(message.author, "display_name", None) or message.author.name,
                "bot": bool(message.author.bot),
                "text": text[:1500], "embeds": embeds}

    def _chat_capture(self, message) -> None:
        try:
            cid = str(message.channel.id)
            if cid not in self._chat_watched(self.get_config()):
                return
            self._chat_logs.setdefault(cid, deque(maxlen=200)).append(self._chat_entry(message))
        except Exception:  # noqa: BLE001 — the log must never break dispatch
            pass

    def chat_perms(self, channel_id) -> dict:
        """The bot's effective permissions in a watched channel — what the Chat
        tab needs to gate its UX. Local cache math only (no API calls)."""
        cid = str(channel_id or "").strip()
        try:
            ch = self._client.get_channel(int(cid)) if self._client else None
        except (TypeError, ValueError):
            ch = None
        if ch is None or getattr(ch, "guild", None) is None or ch.guild.me is None:
            return {"known": False}
        p = ch.permissions_for(ch.guild.me)
        missing = [label for ok, label in (
            (p.view_channel, "View Channel"),
            (p.send_messages, "Send Messages"),
            (p.embed_links, "Embed Links"),
            (p.attach_files, "Attach Files"),
            (p.read_message_history, "Read Message History"),
            (p.manage_webhooks, "Manage Webhooks"),
        ) if not ok]
        return {"known": True,
                "can_send": bool(p.view_channel and p.send_messages),
                "owner_voice": bool(p.manage_webhooks),
                "history": bool(p.read_message_history),
                "missing": missing}

    async def chat_log(self, channel_id, after: str = "") -> dict:
        cid = str(channel_id or "").strip()
        if cid not in self._chat_watched(self.get_config()):
            return {"ok": False, "error": "not a watched channel"}
        if cid not in self._chat_hist:
            # one-time backfill so the tab opens with recent context
            self._chat_hist.add(cid)
            ch = await self._channel(cid)
            if ch is not None:
                try:
                    hist = [m async for m in ch.history(limit=40)]
                    log = self._chat_logs.setdefault(cid, deque(maxlen=200))
                    have = {e["id"] for e in log}
                    merged = [self._chat_entry(m) for m in hist if str(m.id) not in have]
                    both = sorted(merged + list(log), key=lambda e: int(e["id"]))
                    log.clear()
                    log.extend(both)
                except Exception as e:  # noqa: BLE001 — e.g. no Read History perm
                    self.engine._log("error", f"chat history fetch failed: {e}")
        entries = list(self._chat_logs.get(cid) or [])
        if after:
            try:
                a = int(after)
                entries = [e for e in entries if int(e["id"]) > a]
            except (TypeError, ValueError):
                pass
        return {"ok": True, "messages": entries, "perms": self.chat_perms(cid)}

    async def _owner_identity(self, cfg) -> tuple:
        """(display name, avatar url|None) for the OWNER: the Dashboard Owner
        name (cooldown_exempt_names[0]; operator_name = legacy/manual override),
        else the owner user's own Discord display name — the bot's name only
        when nothing else exists. Avatar = the owner user's."""
        ids = cfg.get("cooldown_exempt_user_ids") or []
        uid = str(ids[0]).strip() if ids and str(ids[0]).strip() else None
        av = dn = ""
        if uid and self._client:
            hit = self._owner_ident.get(uid)
            if hit is None:
                try:
                    u = await self._client.fetch_user(int(uid))
                    hit = (str(u.display_avatar.url),
                           getattr(u, "display_name", None) or u.name)
                except Exception:  # noqa: BLE001
                    hit = ("", "")
                self._owner_ident[uid] = hit
            av, dn = hit
        names = cfg.get("cooldown_exempt_names") or []
        nm = str(names[0]).strip() if names else ""
        who = ((cfg.get("operator_name") or "").strip() or nm
               or (dn or "").strip() or self._bot_name())
        return who, (av or None)

    async def _channel_webhook(self, ch):
        """The channel's DiscoFlate webhook (created on demand; needs the bot to
        have Manage Webhooks there). None when unavailable — callers fall back."""
        cid = str(ch.id)
        wh = self._webhooks.get(cid)
        if wh is not None:
            return wh
        try:
            hooks = await ch.webhooks()
            wh = next((h for h in hooks if h.name == "DiscoFlate" and h.token), None)
            if wh is None:
                wh = await ch.create_webhook(name="DiscoFlate", reason="DiscoFlate owner voice")
            self._webhooks[cid] = wh
            return wh
        except Exception as e:  # noqa: BLE001 — usually missing Manage Webhooks
            self.engine._log("error", f"owner webhook unavailable in this channel: {e}")
            return None

    async def owner_say(self, ch, text: str, strict: bool = False) -> bool:
        """Post AS THE OWNER: a webhook message wearing their name + avatar.
        Without webhook access: strict=False falls back to a '**Owner:** …'
        line from the bot; strict=True SKIPS QUIETLY (logged, nothing posted,
        never raises) — for message rows marked 'as the OWNER'."""
        text = (text or "").strip()
        if not text:
            return False
        cfg = self.get_config()
        who, av = await self._owner_identity(cfg)
        wh = await self._channel_webhook(ch)
        if wh is not None:
            try:
                await wh.send(content=_clip(text), username=(who or "Owner")[:80],
                              avatar_url=av)
                return True
            except Exception as e:  # noqa: BLE001
                self.engine._log("error", f"owner webhook send failed: {e}")
                self._webhooks.pop(str(ch.id), None)   # stale hook — re-create next time
        if strict:
            self.engine._log("bot", "owner message skipped (no Manage Webhooks here)")
            return False
        await self._send(ch, f"**{who}:** {text}", None)
        return False

    async def owner_broadcast(self, text: str, strict: bool = False) -> None:
        """Owner-voiced text to every listen channel (#owner-command rows and
        'as the OWNER' message rows inside action blocks). Never raises."""
        cfg = self.get_config()
        for t in self._broadcast_targets(cfg):
            ch = await self._channel(str(t["channel_id"]))
            if ch is not None:
                await self.owner_say(ch, text, strict=strict)

    async def owner_chat(self, channel_id, text: str) -> dict:
        """Send from the panel Chat tab: plain text posts to the channel as the
        bot; a !command executes AS THE OWNER (owner id when set → cooldown-
        exempt + owner-only allowed) with its reply posted to that channel."""
        cfg = self.get_config()
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "nothing to send"}
        cid = str(channel_id or "").strip()
        if cid not in self._chat_watched(cfg):
            return {"ok": False, "error": "pick a watched channel"}
        ch = await self._channel(cid)
        if ch is None:
            return {"ok": False, "error": "channel unavailable — is the bot connected?"}
        prefix = cfg.get("command_prefix", "!")
        if text.startswith("#") and prefix != "#":
            oc = self.engine.find_owner_command(text[1:].split(" ", 1)[0])
            if oc is None:
                return {"ok": False, "error": f"no owner command named {text.split(' ', 1)[0]}"}
            who, _av = await self._owner_identity(cfg)
            body = self.engine.render((oc.get("message") or "").strip(),
                                      {"user": who, "mention": who})
            if body:
                voiced = await self.owner_say(ch, body)
                return {"ok": True, "owner_voice": voiced}
            return {"ok": True}
        if not text.startswith(prefix):
            # plain text speaks AS THE OWNER (webhook name+avatar; bot-prefixed fallback)
            voiced = await self.owner_say(ch, text)
            return {"ok": True, "owner_voice": voiced}
        if not cfg.get("listener_enabled"):
            return {"ok": False, "error": "activation is off"}
        name = text[len(prefix):].split(" ", 1)[0].lower()
        cmd = self.engine.find_command(name)
        if cmd is None or not cmd.get("enabled", True):
            return {"ok": False, "error": f"no enabled command named {prefix}{name}"}
        ids = cfg.get("cooldown_exempt_user_ids") or []
        uid = str(ids[0]).strip() if ids and str(ids[0]).strip() else None
        who, _av = await self._owner_identity(cfg)
        # Typed into the app's own chat box — this IS the operator, whatever
        # their Discord display name happens to be. Never cooldown or budget it.
        res = await self.engine.run_custom(cmd, who, uid=uid, as_owner=True)
        if res.get("game"):
            if uid is None:
                return {"ok": False, "error": "set the Owner user ID (Game tab) to start games from here"}
            # A minigame ACTION hands back its own row (game + tiers + settings)
            # plus a token for the block waiting on it. A legacy game-typed
            # command has neither, so it falls back to the command itself.
            gcmd = res.get("game_cmd") or cmd
            gcmd = {**gcmd, "__resume": res.get("resume_token")}
            glabel = self.engine.game_display_name(gcmd)
            intro = (res.get("reply") or "").strip() or f"🎮 **{who}** started **{name}** — press Play!"
            view = minigames.make_play_view(self, gcmd, who, uid)
            try:
                itxt = self._hdr(cfg, glabel, who) + intro
                iemb = _auto_embed(itxt) or self._out_embed(cfg, glabel, who, intro)
                view.message = await (ch.send(embed=iemb, view=view) if iemb
                                      else ch.send(itxt, view=view))
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"couldn't start the game: {e}"}
            return {"ok": True}
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "couldn't run"}
        if (res.get("reply") or "").strip():
            await self._send(ch, self._hdr(cfg, cmd.get("name") or "", who) + res["reply"], None)
        for post in (res.get("events_posted") or []):
            if isinstance(post, dict):
                await self.broadcast(post.get("text", ""), post.get("image"),
                                     replace_key=post.get("replace_key"))
            else:
                await self.broadcast(post, None)
        return {"ok": True}

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

    async def operator_pump(self, who: str, seconds: float,
                            device_id: str | None = None,
                            untimed: bool = False) -> dict:
        """The Chat tab's Pump button. `device_id` picks which pump (None =
        the active one). `untimed` runs it until STOP instead of for a set
        number of seconds — the hardware has no 'on forever', so it takes the
        session's hard cap and relies on the abort."""
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        who = (who or "").strip() or self._bot_name()
        seconds = self._UNTIMED_SECS if untimed else float(seconds)
        res = await self.engine.fire(seconds, reason=f"pump by {who}",
                                     device_id=device_id or None)
        if res.get("ok") and untimed:
            # "just on" has no duration to announce — saying "3600 seconds have
            # been added" would be a lie, so this one stays quiet until STOP.
            return res
        if res.get("ok"):
            target = device_id or self.engine._active_id()
            default = ("**[secs]** seconds have been added to the pump timer, and will "
                       "increase [operator]'s volume by **+[secs2capacity]%**\n"
                       "Current Capacity: **[capacity]%** Remaining Pump Timer: **[timer]**s")
            tmpl = cfg.get("pump_message") or default
            # broadcast what was actually delivered (the hard cap may clamp it)
            actual = res.get("added") if res.get("added") is not None else seconds
            extra = {"secs": f"{actual:.1f}", "seconds": f"{actual:.1f}",
                     "secs2capacity": self.engine._secs_to_capacity(actual, target)}
            msg = self.engine.render(tmpl, extra)
            await self.broadcast(msg, None, embed=self._cfg_embed(cfg, "pump", msg, "💨 Pump"))
        return res

    _UNTIMED_SECS = 3600.0    # "on until STOP" — the abort is what really ends it

    async def operator_pump_stop(self, who: str = "") -> dict:
        """Stop whatever the pump is doing, whichever device it was. This is
        NOT the session pause — it just ends the fire."""
        await self.engine.abort(reason=f"stop by {(who or '').strip() or self._bot_name()}")
        return {"ok": True, "stopped": True}

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

    async def operator_start_competition(self, name: str) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        return self.engine.start_competition_bg(name, source="operator")

    async def operator_start_bonus_round(self, name: str) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        return self.engine.start_bonus_round_bg(name, source="operator")

    async def operator_broadcast_capacity(self) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        rd, rs = self.engine.range_dice(self.engine.range_for(self.engine.capacity))
        default = "📊 Capacity **[capacity]%**\n[capacity_bar]\nRolling **[dice]** · [announce]"
        tmpl = cfg.get("capacity_message") or default
        msg = self.engine.render(tmpl, {"dice": f"{rd}d{rs}", "sides": rs}) or "📊"
        await self.broadcast(msg, None, embed=self._cfg_embed(cfg, "capacity", msg, "📊 Capacity")
                             or self._status_embed("capacity", msg))
        return {"ok": True}

    async def operator_broadcast_leaderboard(self) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        txt = self.engine.leaderboard_text()
        await self.broadcast(txt, None, embed=self._status_embed("leaderboard", txt))
        return {"ok": True}

    async def operator_broadcast_leaderboard_life(self) -> dict:
        cfg = self.get_config()
        err = self._operator_ready(cfg)
        if err:
            return {"ok": False, "error": err}
        txt = self.engine.leaderboard_life_text()
        await self.broadcast(txt, None, embed=self._status_embed("leaderboard_life", txt))
        return {"ok": True}

    async def operator_cleanup(self, n: int) -> dict:
        """Delete the bot's OWN last `n` messages in each broadcast channel
        (listen targets + announce channel). Deleting one's own messages needs no
        Manage-Messages permission. Not gated on activation — cleanup should work
        even after the session's off."""
        if not self._client or not self._client.is_ready():
            return {"ok": False, "error": "bot not connected"}
        cfg = self.get_config()
        chan_ids = {str(t["channel_id"]) for t in self._targets(cfg)}
        if not chan_ids:
            return {"ok": False, "error": "no server/channel selected"}
        try:
            n = max(1, min(100, int(n or 1)))
        except (TypeError, ValueError):
            n = 1
        me = self._client.user
        deleted = 0
        for cid in chan_ids:
            ch = await self._channel(cid)
            if ch is None:
                continue
            try:
                mine = []
                async for msg in ch.history(limit=300):
                    if me and msg.author and msg.author.id == me.id:
                        mine.append(msg)
                        if len(mine) >= n:
                            break
                for msg in mine:
                    try:
                        await msg.delete()
                        deleted += 1
                    except Exception as e:  # noqa: BLE001 — already gone / too old / perms
                        self.engine._log("error", f"cleanup delete failed: {e}")
            except Exception as e:  # noqa: BLE001 — no read-history perm, etc.
                self.engine._log("error", f"cleanup history failed: {e}")
        self.engine._log("bot", f"cleanup — deleted {deleted} of the bot's own message(s)")
        return {"ok": True, "deleted": deleted}

    async def post_embed(self, title: str, text: str, options: list | None = None,
                         image: str | None = None, footer: str | None = None) -> None:
        """Broadcast a rich embed (polls, embed-ticked milestones & activation
        messages). Always an embed — not gated on rich_output. `options` (poll
        labels) adds tap-to-vote buttons; `image` shows inside the embed (a
        local file attaches, a URL links); `footer` is embed subtext."""
        try:
            e = discord.Embed(title=(title or "")[:256], description=(text or "")[:4096],
                              color=0x9B59B6)
            if footer:
                e.set_footer(text=footer[:2048])
            img_attach = None
            if image:
                if str(image).lower().startswith(("http://", "https://")):
                    e.set_image(url=image)
                else:
                    img_attach = image
                    resolved = _resolve_img(image) or image
                    e.set_image(url=f"attachment://{os.path.basename(resolved)}")
        except Exception:  # noqa: BLE001 — no embed support → plain text
            await self.broadcast(f"**{title}**\n{text}", image)
            return
        view = PollVoteView(self, options) if options else None
        await self.broadcast("", img_attach, embed=e, view=view)

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
        for t in self._broadcast_targets(cfg):
            cid = str(t.get("channel_id") or "")
            if not cid or (exclude_channel_id and cid == str(exclude_channel_id)):
                continue
            ch = await self._channel(cid)
            if ch is None:
                continue
            show_real = bool(real) and author_id is not None and await self._is_member(getattr(ch, "guild", None), author_id)
            text = real if show_real else (anon or real)
            if text:
                seen = who if show_real else _HDR_ANON
                # a minigame result is a per-player result like any other — it
                # was the one that still went out as bare text with rich output on
                await self._send(ch, self._hdr(cfg, label, seen) + text, None,
                                 embed=self._out_embed(cfg, label, seen, text))

    async def game_payoff(self, cmd: dict, score, who: str, uid) -> None:
        """A minigame finished — fire the tier's devices (credited to the player)
        and broadcast the game-labeled result, respecting cross-server anonymity."""
        try:
            token = (cmd or {}).get("__resume")
            res = (await self.engine.resume_minigame(token, score, who, uid)
                   if token else await self.engine.game_result(cmd, score, who, uid))
            label = self.engine.game_display_name(cmd)
            if (res.get("real") or "").strip():   # tier message rows may replace the score line
                await self._broadcast_named(res.get("real"), res.get("anon"), uid, label=label, who=who)
            # the winning tier's optional action block runs AFTER its result posts
            bits = []
            if res.get("secs") and float(res.get("secs") or 0) > 0:
                bits.append(f"+{float(res['secs']):g}s")      # legacy tier fires
            if res.get("tier_actions"):
                tier_ctx = await self.engine.run_actions(
                    res["tier_actions"], f"{label} tier",
                    uid=uid, who=who, score=res.get("score"), game=label)
                if (tier_ctx or {}).get("fired_desc"):
                    bits.append(tier_ctx["fired_desc"])
            # …and then the REST of the block that was waiting on this game,
            # for this player. Their score and the game's totals ride along so
            # later rows can use [score] / [secs] / [game].
            if token:
                rest = await self.engine.resume_block(token, {
                    "score": res.get("score"), "game": res.get("game") or label,
                    "secs": res.get("secs"), "seconds": res.get("seconds"),
                    "secs2capacity": res.get("secs2capacity"),
                    "luck": res.get("luck"), "user": who})
                if (rest or {}).get("fired_desc"):
                    bits.append(rest["fired_desc"])
                # the command's notification was held back at typing time —
                # play it now, with the score and everything the game fired
                await self.engine.notify_game_done(
                    token, score=res.get("score"), game=res.get("game") or label,
                    desc=" · ".join(b for b in bits if b))
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

    # ══ Multiplayer ═════════════════════════════════════════════════════════ #
    #
    # Multiplayer does NOT reuse single player's channel plumbing, and does not
    # branch it either. There is exactly one broadcast channel for a whole match
    # — named in config, agreed by the handshake — so there is no target list,
    # no pinning, no fail-open/fail-closed question and no Isolate. It gets its
    # own send path, its own target resolution and its own listener gate. What
    # IS shared with solo is engine primitives: devices, calibration, the
    # capacity meter, the fire path, the action-block runner. Any sentence of
    # the form "multiplayer is solo's X with a flag" is a design smell.

    @staticmethod
    def _mp(cfg: dict) -> dict:
        return cfg.get("multiplayer") or {}

    def _mp_cfg(self) -> dict:
        """The live config with NO disk read. `get_config` is
        `config_store.load` — a full read, migrate, deep-merge and template
        seed — while the envelope path runs on every message in every channel
        and the match clock runs every second. `mode` and `multiplayer` are
        top-level and never scene-scoped, so the engine's in-memory copy is the
        right answer and costs nothing to ask."""
        return self.engine.cfg or {}

    def _mp_live(self, cfg=None) -> bool:
        """Multiplayer mode is selected. Everything below is inert otherwise,
        so a solo install pays nothing for any of it."""
        cfg = self._mp_cfg() if cfg is None else cfg
        return str(cfg.get("mode") or "solo") == "multi"

    def _mp_chan(self, cfg: dict, which: str) -> str:
        return str((self._mp(cfg).get(which) or {}).get("channel_id") or "").strip()

    def _mp_player(self, cfg: dict) -> str:
        """What this person is called in messages. One of the four things that
        carries over from single player — "Dave-bot fired 10%" is plumbing
        leaking into the show."""
        names = cfg.get("cooldown_exempt_names") or []
        return (str(names[0]).strip() if names and str(names[0]).strip()
                else str(cfg.get("operator_name") or "").strip())

    def _mp_venue(self, cfg: dict) -> dict:
        """Human labels for the two channels. Ids travel for routing; NAMES
        travel so the guest's popup can be answered — "is 901 right?" is not a
        question anyone can settle, "#gameshow in The Den" is."""
        out = {}
        for key, which in (("net", "bot_network"), ("cast", "broadcast")):
            cid = self._mp_chan(cfg, which)
            if not cid:
                continue
            try:
                ch = self._client.get_channel(int(cid)) if self._client else None
            except (TypeError, ValueError):
                ch = None
            g = getattr(ch, "guild", None)
            out[key] = {"channel": str(getattr(ch, "name", "") or cid),
                        "guild": str(getattr(g, "name", "") or ""),
                        "kind": self._chan_kind(cid)}
        return out

    def _mp_cap(self) -> float:
        """How far this match can take anyone — the host's capacity ceiling.

        A number, not "is past 100% allowed": the guest is agreeing to an
        amount of inflation, and it is the cap that tells them what losing
        looks like. "Up to 100" and "up to 300" are not the same offer.

        The End Condition's lose-at capacity wins when it is set, because that
        is literally the losing line — quoting the rig's ceiling instead would
        describe a limit nobody is playing to.
        """
        top = mp_games.end_capacity(self._mp_cfg().get("mp_end") or {})
        if top > 0:
            return float(top)
        try:
            return float(self.engine._capacity_cap())
        except Exception:  # noqa: BLE001
            return 100.0

    def _mp_calibration(self):
        """The PRIMARY pump's seconds-to-100%. This is what makes a race between
        two different rigs fair: % says how much, calibration says how fast."""
        return (self.engine._device(self.engine._active_id()) or {}).get(
            "calibration_seconds_to_100")

    async def mp_search_members(self, query: str, limit: int = 25) -> dict:
        """Members of the BROADCAST server whose name starts with `query`.

        A name search, not a member list. That distinction is what makes this
        work with no privileged intent: `fetch_members` (the bulk list) needs
        Intents.members, `query_members` does not — it is an explicit
        gateway lookup by name prefix.

        Bots are members like anyone else, so the bot you want to play is in
        here beside its owner. Discord never says which human owns which bot;
        it doesn't have to, because you know the name you're looking for and
        the handshake confirms the rest.
        """
        q = str(query or "").strip()
        if len(q) < 2:
            return {"ok": False, "error": "type at least two characters"}
        cid = self._mp_chan(self._mp_cfg(), "broadcast")
        try:
            ch = self._client.get_channel(int(cid)) if (self._client and cid) else None
        except (TypeError, ValueError):
            ch = None
        guild = getattr(ch, "guild", None)
        if guild is None:
            return {"ok": False, "error": "pick a broadcast channel first — "
                                          "the search is scoped to its server"}
        try:
            found = await guild.query_members(query=q, limit=max(5, min(100, int(limit))))
        except asyncio.TimeoutError:
            return {"ok": False, "error": "the server didn't answer in time"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"search failed: {e}"}
        # A bot that has said hello on bot_network is RUNNING DiscoFlate. That
        # is the difference between "this bot exists here" and "this bot will
        # answer an invite", and it is otherwise indistinguishable.
        seen = {str(b) for b in self._mp_seen_bots()}
        me = str(getattr(getattr(self._client, "user", None), "id", "") or "")
        out = []
        for m in found:
            if str(m.id) == me:
                continue                       # you cannot play yourself
            out.append({"id": str(m.id),
                        "name": str(getattr(m, "name", "") or ""),
                        "display": str(getattr(m, "display_name", "") or ""),
                        "bot": bool(getattr(m, "bot", False)),
                        "seen": str(m.id) in seen})
        out.sort(key=lambda r: (not r["seen"], not r["bot"], r["name"].lower()))
        return {"ok": True, "guild": str(getattr(guild, "name", "")), "members": out}

    def _mp_seen_bots(self) -> list:
        """Bot ids that have advertised on bot_network this run."""
        return list(getattr(self, "_mp_hellos", {}) or {})

    def _mp_set_calibration(self, secs) -> None:
        """Write an agreed seconds-to-100% to the PRIMARY pump.

        Only ever from a split-the-difference both sides took, and only for the
        length of the match — `_settle()` sends the old number back through
        here however the match ends.
        """
        try:
            secs = round(float(secs), 2)
        except (TypeError, ValueError):
            return
        if secs <= 0:
            return
        did = self.engine._active_id()
        cfg = config_store.load()
        for d in (cfg.get("devices") or []):
            if str(d.get("id")) == str(did):
                if float(d.get("calibration_seconds_to_100") or 0) == secs:
                    return                      # already there — no churn
                d["calibration_seconds_to_100"] = secs
                config_store.save(cfg)
                self.engine.set_config(cfg)
                self.engine._log("bot", f"multiplayer: {d.get('label') or did} "
                                        f"calibrated to {secs:g}s to 100%")
                return
        self.engine._log("error", "multiplayer: no primary pump to calibrate")

    def link_session(self, cfg=None):
        """The live Session, built once the bot knows who it is. Config-derived
        terms are refreshed only while idle — once a match starts, the terms in
        force are the ones both sides agreed to, not whatever the panel says
        now."""
        cfg = self._mp_cfg() if cfg is None else cfg
        user = getattr(self._client, "user", None) if self._client else None
        if user is None:
            return None
        m = self._mp(cfg)
        if self.link is None or self.link.link.me != str(user.id):
            owner_ids = cfg.get("cooldown_exempt_user_ids") or []
            link = mp.Link(str(user.id),
                           owner=str(owner_ids[0]).strip() if owner_ids else "",
                           version=_install_id().split(" on ")[0].lstrip("v"),
                           install=_install_id(),
                           name=str(getattr(user, "name", "") or ""))
            self.link = mp.Session(link, peer_name=str(m.get("peer_bot_name") or ""))
        s = self.link
        # The PLAYER's name and the command surface are live even mid-match:
        # they change what messages say, never what the match agreed to.
        s.player = self._mp_player(cfg)
        if s.state in (mp.S_IDLE, mp.S_ADVERTISED):
            s.peer_name = str(m.get("peer_bot_name") or "")
            s.role_pref = str(m.get("role_pref") or "host")
            s.blocked = [b for b in (m.get("blocked") or []) if isinstance(b, dict)]
            s.channels = {"net": self._mp_chan(cfg, "bot_network"),
                          "cast": self._mp_chan(cfg, "broadcast")}
            s.calibration = self._mp_calibration()
        # Read-only match values, always available to render(): an overlay
        # label tracking [multi_peer_pct] has to keep resolving on screen, and
        # the compositor re-renders text through engine.render ~4x/sec.
        self.engine.mp_ctx_cb = self._mp_ctx
        return s

    # -- multiplayer's own send paths ---------------------------------------- #
    async def _link_send(self, line: str) -> bool:
        """The protocol's send. Deliberately NOT _send(): that one swallows
        every exception and returns None, and a silently dropped envelope is a
        stalled match. Envelopes are idempotent by seq, so a sender can safely
        retry the same one — but only if it's told the send failed."""
        cfg = self._mp_cfg()
        cid = self._mp_chan(cfg, "bot_network")
        kind = line.split(" ", 2)[1] if line.count(" ") >= 2 else "?"
        if not cid:
            self.engine._log("error", f"multiplayer: no bot_network channel — {kind} not sent")
            return False
        ch = await self._channel(cid)
        if ch is None:
            self.engine._log("error",
                             f"multiplayer: bot_network #{cid} unreachable — {kind} not sent")
            return False
        try:
            await ch.send(line)
            return True
        except Exception as e:  # noqa: BLE001 — surfaced, never swallowed
            self.engine._log("error", f"multiplayer: {kind} not sent: {e}")
            return False

    async def _link_say(self, text: str, *, embed=None, view=None, image=None):
        """The venue. Resolved straight from config — never _broadcast_targets:
        there is exactly one channel, so there is nothing to fan out to and
        nothing to narrow."""
        cid = self._mp_chan(self._mp_cfg(), "broadcast")
        if not cid:
            self.engine._log("error", "multiplayer: no broadcast channel set")
            return None
        ch = await self._channel(cid)
        if ch is None:
            self.engine._log("error", f"multiplayer: broadcast #{cid} unreachable")
            return None
        return await self._send(ch, text, image, embed=embed, view=view)

    async def _link_board(self, text: str, embed=None) -> None:
        """The scoreboard: ONE message, owned by the host, edited in place.

        An edit is one API call where delete-and-repost is two, and the
        broadcast channel is the scarce bucket — it's where the show is. Falls
        back to a fresh post if the tracked message has been deleted."""
        cid = self._mp_chan(self._mp_cfg(), "broadcast")
        prev = self._loop_msgs.get("mp:board", {}).get(cid)
        if prev is not None:
            try:
                await prev.edit(content=None if embed is not None else text, embed=embed)
                return
            except Exception:  # noqa: BLE001 — message gone / no perms → repost
                self._loop_msgs.get("mp:board", {}).pop(cid, None)
        msg = await self._link_say(text, embed=embed)
        if msg is not None:
            self._track_msg("mp:board", cid, msg)

    # -- the envelope path ---------------------------------------------------- #
    def _link_envelope(self, message: discord.Message) -> bool:
        """True when this message belongs to the protocol — handled here and
        invisible to everything downstream.

        Runs FIRST, ahead of the Chat capture and the bot filter: bot_network is
        plumbing, not a venue. Nothing posted there may reach the Chat tab, the
        command resolver, or the activity log.
        """
        cfg = self._mp_cfg()
        if not self._mp_live(cfg):
            return False
        cid = self._mp_chan(cfg, "bot_network")
        if not cid or str(getattr(message.channel, "id", "")) != cid:
            return False
        env = mp.decode(message.content or "")
        # Remember WHO has said hello here. Any number of bots can share this
        # channel, and a bot that advertises is running DiscoFlate — which is
        # the difference between "exists in this server" and "will answer an
        # invite", and is otherwise invisible in a member search.
        if env is not None and env.get("t") == mp.T_HELLO and env.get("from"):
            if not hasattr(self, "_mp_hellos"):
                self._mp_hellos = {}
            self._mp_hellos[str(env["from"])] = {
                "name": str(env.get("bot") or env.get("name") or ""),
                "player": str(env.get("player") or "")}
        s = self.link_session(cfg)
        if env is not None and s is not None:
            out = s.feed(env, time.time(), capacity=self.engine.capacity)
            asyncio.create_task(self._link_apply(out))
        return True     # even a human chatting in bot_network stays out of the game

    async def _mp_curtain(self, black: bool) -> None:
        """Drop or raise the curtain on THIS install's picture.

        `set_blackout` keeps compositing overlays over the black, so the gauges
        and labels stay up while the room itself is hidden — which is what makes
        a pre-show possible at all. Quiet when no virtual camera is running:
        an Android guest simply has no picture to hide.
        """
        if self.engine.camera_cb is None:
            return
        try:
            await self.engine.camera_cb("black" if black else "reveal")
        except Exception as e:  # noqa: BLE001 — never lose a match to a camera
            self.engine._log("error", f"multiplayer: camera {'black' if black else 'reveal'} failed: {e}")

    async def _link_curtain_check(self) -> None:
        """The curtain falls the moment a match is agreed, on BOTH installs, and
        rises only when the host says so.

        Keyed on the MATCH, not on whether the picture is currently black. The
        difference matters: this runs after every envelope, so a flag meaning
        "is it down" would see the host's deliberate mid-match reveal and
        helpfully put it straight back — the curtain would slam shut the moment
        the intro finished. Once per match is the whole contract.
        """
        s = self.link
        live = s is not None and s.state in (mp.S_LINKED, mp.S_READY,
                                             mp.S_MATCH, mp.S_SETTLING)
        sid = (s.link.sid if (s is not None and live) else "")
        if live and sid and self._mp_curtain_sid != sid:
            self._mp_curtain_sid = sid
            await self._mp_curtain(True)
            self._link_notes.append("picture hidden until the host reveals it")
        elif not live and self._mp_curtain_sid:
            # The match is over. Give the operator their picture back rather
            # than leaving them black with nothing running to un-black them.
            self._mp_curtain_sid = ""
            await self._mp_curtain(False)

    async def _link_begin_check(self) -> None:
        """The match has begun — standby ends on BOTH installs, once.

        Keyed on the match id for the same reason the curtain is: this runs
        after every envelope, so "are we standing by" would re-fire the guest's
        acknowledgement all match long.

        What happens next is the whole host/guest split. The HOST's session
        starts for real — its activation message posts, its [!command] tokens
        fire, its events release. The GUEST's never does: it has no game of its
        own to run, it takes instructions. It says so once, on broadcast, so
        the room knows who it is watching, and then it is quiet.
        """
        s = self.link
        began = s is not None and s.state in (mp.S_MATCH, mp.S_SETTLING)
        sid = (s.link.sid if (s is not None and began) else "")
        if not (began and sid and self._mp_begun_sid != sid):
            if not began and self._mp_begun_sid:
                self._mp_begun_sid = ""
            return
        self._mp_begun_sid = sid
        self.engine.set_mp_standby(False)
        if s.is_host:
            if self.mp_activate_cb is not None:
                try:
                    await self.mp_activate_cb()
                except Exception as e:  # noqa: BLE001 — a match outlives this
                    self.engine._log("error", f"multiplayer: activation failed: {e}")
            return
        who = s.player or "the guest"
        await self._link_say(f"🎮 **{who}** is in — playing as guest. "
                             f"The host's bot is running this match.")

    async def _link_apply(self, out) -> None:
        """Perform one Out. The protocol decides; this performs — and in this
        order: envelopes first (an ack or abort must not wait behind a pump),
        then the safe stop, then rows, then the resume file."""
        try:
            for line in out.send:
                await self._link_send(line)
            for note in out.notes:
                self._link_notes.append(note)
                self.engine._log("bot", f"multiplayer: {note}")
            for line in out.say:
                await self._link_say(line)
            if out.board is not None:
                await self._link_board(out.board)
            if out.stop:
                await self.engine.abort(reason="multiplayer: safe stop")
                await self.rounds_stop()   # the match is over; nothing left to run
                if self._start_task is not None:
                    self._start_task.cancel()
                    self._start_task = None
            for item in out.rows:
                await self._link_row(item)
            if out.cal is not None:
                self._mp_set_calibration(out.cal)
            if out.dirty:
                self._link_persist()
            await self._link_curtain_check()
            await self._link_begin_check()
        except Exception as e:  # noqa: BLE001 — a match must never die silently
            self.engine._log("error", f"multiplayer: applying an envelope failed: {e}")

    async def _mp_notify_local(self, row: dict, ctx) -> None:
        """An overlay line on THIS install, for a fire that arrived over the
        wire. Overlay only, never chat."""
        if self.engine.notify_cb is None:
            return
        pct = self.engine.render(str(row.get("fill_pct", "")), dict(ctx or {}))
        try:
            pct = f"{float(pct):g}"
        except (TypeError, ValueError):
            pass
        s = self.link
        who = (s.peer_player or s.link.peer_name or "the host") if s else "the host"
        mode = str(row.get("fire_mode") or "add")
        line = (f"💨 +{pct}% from {who}" if mode == "add"
                else f"💨 {who} set you to {pct}%")
        try:
            await self.engine.notify_cb(line, self._mp_ctx())
        except Exception as e:  # noqa: BLE001 — a missed line can't kill a match
            self.engine._log("error", f"multiplayer: local notify failed: {e}")

    async def _link_row(self, item: dict) -> None:
        """Run one gated `do` row locally, then report what ACTUALLY happened.

        A `message` row posts in THIS bot's voice through multiplayer's own
        send; everything else goes to the engine's action-block runner, which is
        why anything added to the action system later travels the wire for free.
        """
        s = self.link
        row = dict(item.get("row") or {})
        typ = str(row.get("type") or "")
        did, ok, why = "", True, ""
        try:
            if typ == "message":
                text = self.engine.render(str(row.get("message") or row.get("text") or ""))
                style = str(row.get("style") or "")
                msg = await self._link_say(
                    text, embed=self._status_embed("broadcast", text) if style == "embed" else None)
                ok = msg is not None
                did, why = ("posted ✓", "") if ok else ("", "refused: post failed")
            else:
                # The router has to be live for an ARRIVING row as well: an
                # `mp_action` from the other bot is meaningless to the engine
                # on its own, and that is exactly what a `tell` sends.
                self.engine.mp_row_cb = self._mp_row_router
                try:
                    ctx = await self.engine._run_action_block([row], name="multiplayer")
                finally:
                    self.engine.mp_row_cb = None
                did = str((ctx or {}).get("fired_desc") or "") or f"{typ} ✓"
                # Time landing on THIS pump with no local command behind it.
                # Say so on this machine's own overlay — and only there: the
                # host owns every chat notification for both of us, so a post
                # from here would be the same news twice in the same channel.
                if typ == "fire" and int(item.get("re") or 0):
                    await self._mp_notify_local(row, ctx)
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"refused: {e}"
            self.engine._log("error", f"multiplayer: {typ} row failed: {e}")
        re_seq = int(item.get("re") or 0)
        if s is not None and re_seq:
            # re 0 = a row WE originated (the referee pushing our own pump).
            # There is nobody waiting on an ack for it.
            await self._link_apply(s.ack(time.time(), re=re_seq, did=did,
                                         ok=ok, why=why,
                                         capacity=self.engine.capacity))

    # -- preflights (all local — no API calls) -------------------------------- #
    async def link_preflight(self, cfg=None) -> list:
        """The five checks. Every failure NAMES which check failed: "Dave's bot
        is pointed at #general, you're pointed at #gameshow" is a fixable
        problem; a silent dead end is not."""
        cfg = self._mp_cfg() if cfg is None else cfg
        s, checks = self.link, []
        net, cast = self._mp_chan(cfg, "bot_network"), self._mp_chan(cfg, "broadcast")

        def chan_check(cid, label, needs):
            if not cid:
                return {"ok": False, "check": label, "why": f"no {label} channel picked"}
            p = self.chat_perms(cid)
            if not p.get("known"):
                return {"ok": None, "check": label,
                        "why": f"the bot can't see {label} yet — is it invited to that server?"}
            lack = [n for n in needs if n in (p.get("missing") or [])]
            if lack:
                return {"ok": False, "check": label,
                        "why": f"missing in {label}: {', '.join(lack)}"}
            return {"ok": True, "check": label, "why": ""}

        checks.append(chan_check(net, "bot_network",
                                 ["View Channel", "Send Messages", "Read Message History"]))
        checks.append(chan_check(cast, "broadcast",
                                 ["View Channel", "Send Messages", "Embed Links"]))

        # 3 + 4: do the two bots agree on both channels? Answerable as soon as
        # the peer says hello, which is when it is still cheap to fix.
        peer_ch = dict(getattr(s, "peer_channels", None) or {}) if s else {}
        faults = mp.check_channels({"net": net, "cast": cast}, peer_ch) if peer_ch else []
        for label in ("bot_network", "broadcast"):
            name = f"peer's {label}"
            if not peer_ch:
                checks.append({"ok": None, "check": name, "why": "no peer seen yet"})
                continue
            hit = next((f for f in faults if label in f), "")
            checks.append({"ok": not hit, "check": name, "why": hit})

        # 5. Who YOU are. Blank is a silent failure, not a loud one: the Ready
        #    buttons get scoped to nobody (so anyone can press them) and the
        #    referee can't tell a racer from the audience, so a row aimed at
        #    one of them lands on nobody.
        ids = cfg.get("cooldown_exempt_user_ids") or []
        names = cfg.get("cooldown_exempt_names") or []
        me_id = str(ids[0]).strip() if ids else ""
        me_nm = (str(names[0]).strip() if names and str(names[0]).strip()
                 else str(cfg.get("operator_name") or "").strip())
        where = " — set it on the Scenes tab under Limits & announcements"
        if not me_id.isdigit():
            # Fatal: without it the Ready buttons scope to nobody and `!pump`
            # can't tell either racer apart from the audience.
            checks.append({"ok": False, "check": "your identity",
                           "why": "your Discord user ID isn't set" + where})
        elif not me_nm:
            # Not fatal — the match just talks about "you" and "your opponent"
            # instead of naming anyone. Worth saying, not worth refusing over.
            checks.append({"ok": None, "check": "your identity",
                           "why": "no player name, so messages won't name you" + where})
        else:
            checks.append({"ok": True, "check": "your identity", "why": ""})

        # 6. A voice broadcast channel is the only place video can happen, and
        #    it needs both owners actually in the guild (and, for cameras, in
        #    the channel). A text channel means nobody is on camera by Discord's
        #    rules rather than ours — not an error, so not a failure here.
        if cast and self._chan_kind(cast) == "voice":
            try:
                ch = self._client.get_channel(int(cast)) if self._client else None
            except (TypeError, ValueError):
                ch = None
            guild = getattr(ch, "guild", None)
            ids = cfg.get("cooldown_exempt_user_ids") or []
            mine = str(ids[0]).strip() if ids else ""
            theirs = str(getattr(s.link, "peer_owner", "") or "") if s else ""
            missing = []
            if guild is not None:
                for uid, who in ((mine, "you"),
                                 (theirs, (s.link.peer_name if s else "") or "the peer")):
                    if not uid.isdigit():
                        continue          # nobody named yet — not a failure
                    if not await self._is_member(guild, int(uid)):
                        missing.append(f"{who} isn't in that server")
            checks.append({"ok": not missing, "check": "voice channel",
                           "why": "; ".join(missing)})
        return checks

    # -- the referee ---------------------------------------------------------- #
    def _mp_game(self) -> str:
        """What the guest is being invited to, in words.

        The loaded multiplayer scene IS the game — its Rounds are what gets
        played — so the scene's name is the honest answer. There is no separate
        "game mode" to pick: that was a leftover from when Race was the only
        thing there was.
        """
        sc = config_store.live_scene(self._mp_cfg())
        named = str(sc.get("name") or "").strip()
        if str(sc.get("mode") or "") == "multi" and named:
            return named
        return "a multiplayer match"

    def _mp_input(self) -> str:
        """Versus or Gameshow — whose hands are on it. A property of the SCENE,
        because it is the scene's own production; the config value is the
        fallback for a match running without one."""
        sc = config_store.live_scene(self._mp_cfg())
        if str(sc.get("mode") or "") == "multi" and sc.get("input"):
            return "audience" if str(sc["input"]) == "audience" else "operators"
        return ""

    async def _mp_production(self) -> None:
        """Bring up the HOST's in-match layout, on the host.

        The guest brings up nothing of its own. It holds the same shipped
        scene, so the host can call any overlay or group in it by id and have
        it play over there — but WHEN that happens is the host's call, not a
        layout the guest decides for itself.

        Both players are on camera, so each carries only its own gauge and its
        own pump timer. The peer's numbers arrive on the heartbeat and drive
        the game's maths, not a second widget.
        """
        s = self.link
        if s is None or self.engine.overlay_cb is None:
            return
        want = str((self.engine.golive() or {}).get("after_group") or "").strip()
        if not want:
            self._link_notes.append(
                "no in-match scene group set for this scene — text only")
            return
        try:
            await self.engine._run_action_block(
                [{"type": "scene_group", "group": want}], name="multiplayer:production")
        except Exception as e:  # noqa: BLE001
            self.engine._log("error", f"multiplayer: laying out {want} failed: {e}")

    async def _mp_play_intro(self) -> None:
        """Play the host's scene intro, stage by stage.

        Reuses `intro_stages()` — the SAME pre-show data Go Live uses, so a
        scene has one intro rather than a solo one and a multiplayer one — but
        plays it through multiplayer's own runner rather than solo's go-live
        state machine, which gates the camera and holds commands on rules that
        don't apply here.
        """
        stages = self.engine.intro_stages()
        if not stages:
            return
        self.engine.mp_row_cb = self._mp_row_router
        try:
            for st in stages:
                if self.link is None or self.link.state != mp.S_MATCH:
                    return                      # aborted mid pre-show
                rows = []
                if st.get("group"):
                    rows.append({"type": "scene_group", "group": st["group"],
                                 "fade_in": 0.3})
                rows.extend(st.get("actions") or [])
                if rows:
                    await self.engine._run_action_block(
                        rows, name="multiplayer:intro", extra_ctx=self._mp_ctx())
                if st.get("seconds"):
                    await asyncio.sleep(float(st["seconds"]))
                if st.get("group"):
                    await self.engine._run_action_block(
                        [{"type": "scene_group_kill", "group": st["group"],
                          "fade_out": 0.3}], name="multiplayer:intro")
        finally:
            self.engine.mp_row_cb = None

    async def start_match(self) -> dict:
        """The whole opening, in order, on the host's clock:

            intro(s)  →  raise both curtains  →  the game

        ONE entry point. Two of them is how an operator ends up mid-pre-show
        with the picture already live, or with two things running at once.
        """
        s = self.link
        if s is None or not s.is_host:
            return {"ok": False, "error": "only the host starts the match"}
        if s.state not in (mp.S_LINKED, mp.S_READY):
            return {"ok": False, "error": f"can't start while {s.state}"}
        if self._start_task is not None and not self._start_task.done():
            return {"ok": False, "error": "the match is already starting"}
        self._start_task = asyncio.create_task(self._run_start())
        return {"ok": True, "status": self.link_status()}

    async def _run_start(self) -> None:
        s = self.link
        try:
            # MATCH first: the pre-show's own rows have to be able to cross the
            # wire, and nothing may cross outside a match.
            await self._link_apply(s.begin(time.time(), phase="intro", rnd=0))
            await self._mp_play_intro()
            if s.state != mp.S_MATCH:
                return                          # aborted during the pre-show
            # Lay the production out for the guest's camera answer, THEN reveal.
            await self._mp_production()
            await self._link_apply(s.send_do(time.time(),
                                             {"type": "camera", "op": "reveal"}))
            await self._mp_curtain(False)
            self._link_notes.append("curtains up")

            # A game IS its Rounds. With none defined there is nothing to play,
            # and saying so beats sitting revealed and silent.
            await self._rounds_run()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            self.engine._log("error", f"multiplayer: start failed: {e}")
            self._link_notes.append(f"start failed: {e}")
        finally:
            self._start_task = None

    def mp_action(self, name: str) -> dict:
        """One saved Multiplayer Action by name. Global — never per-scene, and
        nothing to do with solo's templates."""
        want = str(name or "").strip().lower()
        for a in (self._mp_cfg().get("mp_actions") or []):
            if str(a.get("name") or "").strip().lower() == want:
                return a
        return {}

    def _mp_ctx(self, chosen: str = "", extra=None) -> dict:
        """The match, as render values. Every one is `[multi_*]`-prefixed so a
        block can never confuse the other player's capacity with this install's
        own `[capacity]`.

        Read by three callers: a multiplayer block's context, a loop's
        until-condition, and — because an overlay LABEL has to keep resolving
        on screen — `engine.render` itself, ~4x/sec. So it stays cheap and
        returns nothing at all outside a match, rather than seeding solo
        renders with two dozen empty tokens.
        """
        s = self.link
        if s is None or s.state in (mp.S_IDLE, mp.S_ADVERTISED):
            return dict(extra or {})
        s.capacity = float(self.engine.capacity)
        return mp_games.placeholders(s, chosen=chosen, extra=extra)

    def _mp_render_row(self, row: dict, xc: dict) -> dict:
        """Resolve a row's placeholders BEFORE it crosses the wire.

        The guest has no idea who `[multi_chosen]` is or what the host's dice
        rolled — its context is its own. An unresolved placeholder would arrive
        as literal text and the row would do nothing, silently. Nested blocks
        are left alone: those run in whatever context the receiver has.
        """
        out = {}
        for k, v in (row or {}).items():
            if isinstance(v, str) and "[" in v:
                out[k] = self.engine.render(v, xc)
            else:
                out[k] = v
        return out

    async def _mp_spin_row(self, a: dict, xc: dict) -> bool:
        """The roulette. Picks a racer and publishes who — it fires nothing
        itself, because what happens to the chosen racer is the job of the rows
        after it, and that is what makes the block reusable."""
        s = self.link
        weights = a.get("weights") if isinstance(a.get("weights"), dict) else {}
        chosen, odds = mp_games.spin([s.link.me, s.link.peer], weights)
        ctx = self._mp_ctx(chosen=chosen)
        xc.update(ctx)
        names = {s.link.me: ctx.get("multi_me_name") or "you",
                 s.link.peer: ctx.get("multi_peer_name") or "them"}
        xc["multi_odds"] = " · ".join(f"{names.get(k, k)} {v * 100:.0f}%"
                                      for k, v in odds.items())
        even = len({round(v, 4) for v in odds.values()}) <= 1
        say = str(a.get("message") or "").strip()
        if say:
            await self._link_say(self.engine.render(say, xc))
        if not even and a.get("announce_odds", True):
            # A wheel the audience can't see is indistinguishable from a rigged
            # one, so uneven odds are stated rather than merely applied.
            await self._link_say(f"🎡 Odds this spin — {xc['multi_odds']}")
        self._link_notes.append(f"spin → {xc.get('multi_chosen_name') or chosen}")
        return True

    async def _mp_roll_row(self, a: dict, xc: dict) -> bool:
        """Roll, publish, fire nothing. A separate fire row spends the number as
        a PERCENT — seconds are refused over the wire, and splitting the two
        lets the same roll drive a message or an overlay instead."""
        total, faces = mp_games.roll_dice(a.get("dice"), a.get("sides"), a.get("luck"))
        d = max(1, int(mp._num(a.get("dice")) or mp_games.ROLL_DICE))
        sd = max(2, int(mp._num(a.get("sides")) or mp_games.ROLL_SIDES))
        xc.update({"multi_roll": str(total), "multi_roll_dice": f"{d}d{sd}",
                   "multi_roll_faces": "+".join(str(f) for f in faces),
                   "multi_roll_max": str(d * sd)})
        say = str(a.get("message") or "").strip()
        if say:
            await self._link_say(self.engine.render(say, xc))
        self._link_notes.append(f"rolled {d}d{sd} → {total}")
        return True

    def _mp_card(self, title: str, body: str):
        """An embed for a multiplayer prompt, independent of `rich_output`."""
        try:
            e = discord.Embed(description=(body or "")[:4096],
                              color=self._EMBED_COLORS.get("command", 0x5865F2))
            if title:
                e.title = title[:256]
            return e
        except Exception:  # noqa: BLE001 — never lose a prompt to a bad embed
            return None

    async def _mp_choice_row(self, a: dict, xc: dict) -> bool:
        """A Player Choice. BLOCKS the block until the player answers or the
        deadline passes — which is the entire point of "Double or Nothing":
        nothing may fire until they've decided.

        It blocks simply by being awaited. The engine awaits the multiplayer
        row hook, so a slow row is a slow block with no parking, no resume
        token and no second code path.
        """
        s = self.link
        opts = mp_games.choice_options(a)
        if len(opts) < 2:
            self._link_notes.append("choice needs at least two options — skipped")
            return True
        ids = mp_games.resolve_who(
            str(a.get("multi_who") or "chosen"), me=s.link.me, peer=s.link.peer,
            chosen=str(xc.get("multi_chosen") or ""),
            caps={s.link.me: float(self.engine.capacity),
                  s.link.peer: float(s.peer_cap or 0)},
            role=s.link.role)
        if not ids:
            self._link_notes.append("choice targeted nobody — skipped")
            return True
        who_id = ids[0]
        ctx = self._mp_ctx(chosen=str(xc.get("multi_chosen") or ""))
        who_name = (ctx.get("multi_me_name") if who_id == s.link.me
                    else ctx.get("multi_peer_name")) or "the racer"
        uid = s.link.owner if who_id == s.link.me else s.link.peer_owner

        secs = mp_games.choice_deadline(a.get("seconds"))
        view = MpChoiceView(self, opts, uid, who_name, secs)
        self._register_view(view)
        title = self.engine.render(str(a.get("title") or "").strip(), xc)
        body = self.engine.render(str(a.get("message") or "").strip(), xc)             or f"**{who_name}** — your call."
        # ALWAYS a card, whatever `rich_output` says. That toggle is about
        # status and report posts; a timed decision with buttons is the one
        # thing the doc insists on presenting as an embed, and a player who
        # misses it because the prompt looked like chatter loses their turn.
        view.message = await self._link_say(body, embed=self._mp_card(title, body),
                                            view=view)

        try:
            await asyncio.wait_for(view.done.wait(), timeout=secs + 5)
        except asyncio.TimeoutError:
            pass
        picked = view.picked
        if picked is None:
            # Nobody answered. A named default is what keeps a walked-away
            # player from stalling the match; without one the safe answer is
            # the LAST option, which is the un-brave one by convention.
            picked = str(a.get("default") or opts[-1]["value"]).strip().lower()
            tmo = str(a.get("timeout_message") or "").strip()
            await self._link_say(self.engine.render(tmo, {**xc, "multi_choice": picked})
                                 if tmo else
                                 f"⏳ No answer from {who_name} — taking **{picked}**.")
        hit = next((o for o in opts if o["value"] == picked), opts[-1])
        xc.update({"multi_choice": hit["value"], "multi_choice_label": hit["label"],
                   "multi_choice_by": who_name, "multi_choice_who": who_id})
        self._link_notes.append(f"{who_name} chose {hit['label']}")
        if hit["actions"]:
            sub = await self.engine._run_action_block(
                hit["actions"], name=f"multiplayer:choice:{hit['value']}",
                extra_ctx=xc)
            if isinstance(sub, dict):
                xc.update(sub)
        return True

    async def _mp_cards_row(self, a: dict, xc: dict) -> bool:
        """Blackjack, two seats, one dealer. BLOCKS until both stand or bust.

        The dealer makes no choices — it hits to 17, which is a rule, not a
        decision. That is what lets it be a shared threat both players face at
        once rather than a third competitor.
        """
        s = self.link
        ctx = self._mp_ctx()
        me_nm = ctx.get("multi_me_name") or "You"
        peer_nm = ctx.get("multi_peer_name") or "Opponent"
        hands = {"me": [mp_games.card_draw(), mp_games.card_draw()],
                 "peer": [mp_games.card_draw(), mp_games.card_draw()]}
        dealer = [mp_games.card_draw(), mp_games.card_draw()]
        secs = mp_games.choice_deadline(a.get("seconds"))
        view = MpCardsView(self, {"me": s.link.owner, "peer": s.link.peer_owner},
                           {"me": me_nm, "peer": peer_nm}, hands, dealer[0], secs)
        self._register_view(view)
        view.message = await self._link_say(
            view.table(), embed=self._mp_card("🃏 Blackjack", view.table()), view=view)
        try:
            await asyncio.wait_for(view.done.wait(), timeout=secs + 5)
        except asyncio.TimeoutError:
            pass                       # a walked-away hand simply stands as-is

        dealer = mp_games.dealer_play(dealer)
        r = mp_games.cards_outcome(view.hands["me"], view.hands["peer"], dealer,
                                   s.link.me, s.link.peer)
        names = {s.link.me: me_nm, s.link.peer: peer_nm}
        if r["both"]:
            head = "🏛 **The house takes both.**"
        elif r["push"]:
            head = "🤝 **Push** — level against the dealer, nobody pays."
        else:
            head = f"🃏 **{names[r['loser']]}** pays."
        await self._link_say(
            f"{head}\n{view.table(reveal=dealer)}",
            embed=self._mp_card("🃏 Blackjack", f"{head}\n{view.table(reveal=dealer)}"))
        xc.update({
            "multi_cards_loser": r["loser"],
            "multi_cards_winner": ("" if r["both"] or r["push"] or not r["loser"]
                                   else (s.link.peer if r["loser"] == s.link.me
                                         else s.link.me)),
            "multi_cards_loser_name": names.get(r["loser"], ""),
            "multi_cards_dealer": f"{r['dealer']:g}",
            "multi_cards_my_score": f"{r['scores'][s.link.me]:g}",
            "multi_cards_peer_score": f"{r['scores'][s.link.peer]:g}",
            "multi_cards_margin": f"{abs(r['scores'][s.link.me] - r['scores'][s.link.peer]):g}",
            "multi_cards_both": "1" if r["both"] else "",
        })
        # `both` is a real outcome, so a block can fire at everyone when the
        # house cleans up without inventing a second row type for it.
        if r["both"]:
            xc["multi_cards_who"] = "both"
        elif r["loser"]:
            xc["multi_cards_who"] = ("me" if r["loser"] == s.link.me else "peer")
        else:
            xc["multi_cards_who"] = ""
        return True

    async def _mp_duel_row(self, a: dict, xc: dict) -> bool:
        """Both racers pick at once, hidden, then it reveals. BLOCKS until both
        have answered or the deadline passes."""
        s = self.link
        opts = mp_games.duel_options(a)
        if len(opts) < 2:
            self._link_notes.append("a duel needs at least two moves — skipped")
            return True
        ctx = self._mp_ctx()
        me_nm = ctx.get("multi_me_name") or "You"
        peer_nm = ctx.get("multi_peer_name") or "Opponent"
        secs = mp_games.choice_deadline(a.get("seconds"))
        view = MpDuelView(self, opts,
                          {"me": s.link.owner, "peer": s.link.peer_owner},
                          {"me": me_nm, "peer": peer_nm}, secs)
        self._register_view(view)
        title = self.engine.render(str(a.get("title") or "").strip(), xc) or "Pick one"
        body = self.engine.render(str(a.get("message") or "").strip(), xc) \
            or f"**{me_nm}** vs **{peer_nm}** — both pick. Nobody sees the other until both are in."
        view.message = await self._link_say(body, embed=self._mp_card(title, body),
                                            view=view)
        try:
            await asyncio.wait_for(view.done.wait(), timeout=secs + 5)
        except asyncio.TimeoutError:
            pass

        mine, theirs = view.picks.get("me", ""), view.picks.get("peer", "")
        side = mp_games.duel_winner(mine, theirs, a.get("options"))
        lbl = {o["value"]: o["label"] for o in opts}
        win = s.link.me if side == "me" else (s.link.peer if side == "peer" else "")
        lose = "" if not win else (s.link.peer if win == s.link.me else s.link.me)
        names = {s.link.me: me_nm, s.link.peer: peer_nm}
        xc.update({
            "multi_duel_me": lbl.get(mine, "") or "—",
            "multi_duel_peer": lbl.get(theirs, "") or "—",
            "multi_duel_winner": win, "multi_duel_loser": lose,
            "multi_duel_winner_name": names.get(win, "") if win else "",
            "multi_duel_loser_name": names.get(lose, "") if lose else "",
            "multi_duel_draw": "1" if not win else "",
            # `chosen` is whoever this round singled out, however it did the
            # singling — so a row written for the wheel works here unchanged.
            "multi_chosen": lose or "",
            "multi_chosen_name": names.get(lose, "") if lose else "",
        })
        self._link_notes.append(
            f"duel: {xc['multi_duel_me']} vs {xc['multi_duel_peer']} → "
            + (f"{names.get(win)} wins" if win else "draw"))
        return True

    async def _mp_run_row(self, a: dict, xc: dict) -> bool:
        """Run another Multiplayer Action from inside this one.

        This is what lets a Round be composed rather than being one loop: four
        passes of the roulette, a video, four more, another video. Depth-guarded
        — an Action that runs itself would otherwise spiral, and the block
        runner has no way to notice.
        """
        name = self.engine.render(str(a.get("action") or "").strip(), xc)
        if not name:
            return True
        depth = int(xc.get("_mp_depth") or 0)
        if depth >= mp_games.RUN_DEPTH:
            self._link_notes.append(f"'{name}' nested too deep — stopped")
            return True
        blk = self.mp_action(name)
        rows = blk.get("actions") or []
        if not rows:
            self._link_notes.append(f"no Multiplayer Action called {name!r}")
            return True
        told = {f"multi_told_{k}": v for k, v in (a.get("told") or {}).items()
                if isinstance(k, str)}
        sub = await self.engine._run_action_block(
            rows, name=f"multiplayer:{name}",
            extra_ctx={**xc, **told, "_mp_depth": depth + 1})
        if isinstance(sub, dict):
            # results flow back out, so a later row can read what it produced
            xc.update({k: v for k, v in sub.items() if k != "_mp_depth"})
        return True

    async def _mp_tell_row(self, a: dict, xc: dict) -> bool:
        """Say something to the other BOT, over bot_network.

        It carries the NAME of an Action for them to run — not the thing to do.
        They play their OWN copy, with their own overlays and their own scene,
        which is why this exists: an overlay id from your scene means something
        different over there, or nothing. You don't reach into their picture,
        you tell them what just happened and let them show it their way.

        Values ride along and land in their block as `[multi_told_*]`, so "the
        wheel picked Dave" can be said in their words on their stream.
        """
        s = self.link
        name = self.engine.render(str(a.get("action") or "").strip(), xc)
        if not name:
            self._link_notes.append("nothing to tell them — no Action named")
            return True
        vals = {}
        for k, v in (a.get("values") or {}).items():
            key = str(k).strip().lower().replace(" ", "_")
            if key:
                vals[key] = self.engine.render(str(v), xc)
        await self._link_apply(s.send_do(time.time(),
                                         {"type": mp_games.T_RUN, "action": name,
                                          "told": vals}))
        # Name the BOT, not just the player: several pairs can share one
        # bot_network channel, so "told them" stops being unambiguous.
        who = s.link.peer_name or s.peer_player or s.link.peer or "the other bot"
        self._link_notes.append(f"told {who} to run {name!r}")
        return True

    async def _mp_row_router(self, a: dict, xc: dict) -> bool:
        """The engine's multiplayer hook. True = handled here, don't run locally.

        Two jobs: run the rows only multiplayer knows, and send a row that
        belongs to the OTHER install across the wire instead of firing this
        one's pump.
        """
        s = self.link
        if s is None or s.state not in (mp.S_MATCH, mp.S_SETTLING):
            return False
        typ = str((a or {}).get("type") or "").lower()
        if typ == mp_games.T_SPIN:
            return await self._mp_spin_row(a, xc)
        if typ == mp_games.T_ROLL:
            return await self._mp_roll_row(a, xc)
        if typ == mp_games.T_CHOICE:
            return await self._mp_choice_row(a, xc)
        if typ == mp_games.T_CARDS:
            return await self._mp_cards_row(a, xc)
        if typ == mp_games.T_DUEL:
            return await self._mp_duel_row(a, xc)
        if typ == mp_games.T_RUN:
            return await self._mp_run_row(a, xc)
        if typ == mp_games.T_TELL:
            return await self._mp_tell_row(a, xc)

        who = str((a or {}).get("multi_who") or "").strip().lower()
        if typ == "message" and who not in ("peer", "both"):
            # A match speaks in ONE channel — its own, named in config. Left to
            # the engine this would go out through solo's `_announce` and its
            # broadcast targets, which is precisely the plumbing multiplayer
            # does not reuse.
            text = self.engine.render(str(a.get("message") or a.get("text") or ""), xc)
            if text.strip():
                title = self.engine.render(str(a.get("title") or ""), xc)
                embed = (self._mp_card(title, text)
                         if str(a.get("style") or "") == "embed" else None)
                await self._link_say(text, embed=embed)
            return True
        if not who:
            return False              # nobody in particular — the engine runs it
        ids = mp_games.resolve_who(
            who, me=s.link.me, peer=s.link.peer,
            chosen=str(xc.get("multi_chosen") or ""),
            # "the loser" means whoever lost the LAST contest, whichever kind
            # it was. Without this a fire aimed at the loser only ever saw a
            # duel, and a blackjack round would quietly hit nobody.
            winner=str(xc.get("multi_duel_winner")
                       or xc.get("multi_cards_winner") or ""),
            loser=str(xc.get("multi_duel_loser")
                      or xc.get("multi_cards_loser") or ""),
            caps={s.link.me: float(self.engine.capacity),
                  s.link.peer: float(s.peer_cap or 0)},
            role=s.link.role)
        if not ids:
            # Refuse rather than guess. Inflating the wrong person because a
            # target didn't resolve is the one outcome worth a dropped row.
            self._link_notes.append(f"row targeted '{who}' — nobody matched, skipped")
            return True
        if s.link.peer in ids:
            if typ not in mp_games.CROSSABLE:
                # Hiding the control is not enforcement: an older config, or a
                # hand-edited one, can still carry a target on a row that has
                # no business crossing.
                self._link_notes.append(
                    f"a {typ} row can't act on the other machine — kept here")
            else:
                row = {k: v for k, v in (a or {}).items() if k != "multi_who"}
                await self._link_apply(s.send_do(time.time(),
                                                 self._mp_render_row(row, xc)))
                if s.link.me not in ids:
                    return True
        return s.link.me not in ids   # ours too? let the engine run it as well

    async def mp_run_action(self, name: str, extra=None) -> dict:  # noqa: D401
        """Run one Multiplayer Action. The HOST runs the block; rows aimed at
        the guest leave as `do` envelopes. The block itself is ordinary action
        rows, so `repeat`, `if` and `wait` are the ones already written and
        already correct — multiplayer owns its transport, not its own runner."""
        s = self.link
        if s is None or not s.is_host:
            return {"ok": False, "error": "only the host runs multiplayer actions"}
        if s.state not in (mp.S_MATCH, mp.S_SETTLING):
            return {"ok": False, "error": f"no match running ({s.state})"}
        blk = self.mp_action(name)
        rows = blk.get("actions") or []
        if not rows:
            # An Action you haven't filled in, or one that was deleted out from
            # under a round. Neither is worth stopping a match for: skip it and
            # let the round carry on, the same as an empty round.
            why = ("is empty" if blk else "doesn't exist (any more)")
            self._link_notes.append(f"Action {name!r} {why} — skipped")
            return {"ok": True, "skipped": f"Action {name!r} {why}"}
        self.engine.mp_row_cb = self._mp_row_router
        try:
            await self.engine._run_action_block(
                rows, name=f"multiplayer:{blk.get('name') or name}",
                extra_ctx=self._mp_ctx(extra=extra))
        finally:
            # ROUTING is only ever live for the duration of a multiplayer
            # block — solo's own blocks must never be routed anywhere. The
            # read-only CONTEXT stays on (see link_session): an overlay label
            # has to keep resolving between blocks, not just inside one.
            self.engine.mp_row_cb = None
        return {"ok": True, "status": self.link_status()}

    # -- Rounds: bands that loop their action --------------------------------- #
    def _mp_caps(self) -> dict:
        s = self.link
        return {s.link.me: float(self.engine.capacity),
                s.link.peer: float(s.peer_cap or 0)}

    async def rounds_start(self) -> dict:
        """Host: run the round sequence. Each round opens with its own intro,
        then loops its Action until the band is cleared, then hands over."""
        s = self.link
        if s is None or not s.is_host:
            return {"ok": False, "error": "only the host runs rounds"}
        if s.state not in (mp.S_MATCH, mp.S_SETTLING):
            return {"ok": False, "error": f"no match running ({s.state})"}
        rounds = mp_games.rounds_in_order(self._mp_cfg().get("mp_rounds") or [])
        if not rounds:
            return {"ok": False, "error": "no rounds defined"}
        if self._rounds_task is not None and not self._rounds_task.done():
            return {"ok": False, "error": "rounds are already running"}
        self._rounds_task = asyncio.create_task(self._rounds_run())
        return {"ok": True, "status": self.link_status()}

    async def rounds_stop(self) -> None:
        if self._rounds_task is not None:
            self._rounds_task.cancel()
            self._rounds_task = None

    async def _rounds_run(self) -> None:
        s = self.link
        try:
            # Walk the rounds in the order they are listed. Never by looking
            # up "which band is capacity in": that read one install's meter
            # while the clear-test reads both, so a round the OTHER racer
            # cleared left the driver pointed at the same one forever.
            rounds = mp_games.rounds_in_order(self._mp_cfg().get("mp_rounds") or [])
            if not rounds:
                self._link_notes.append("no rounds defined — nothing to play")
                await self._link_say("⚠ **No rounds are set up**, so there's "
                                     "nothing to play. Build them on the "
                                     "Triggers tab.")
                return
            for rnd in rounds:
                if s is None or s.state != mp.S_MATCH:
                    return
                if await self._mp_end_check():
                    return
                name = str(rnd.get("name") or "")
                until = str(rnd.get("until") or "leader")
                if (until != "count"
                        and mp_games.round_cleared(rnd, self._mp_caps(), until)):
                    # Somebody is already past this target — running it would be
                    # asking for something that has already happened. A COUNT
                    # round is never "already past": N passes is N passes.
                    self._link_notes.append(f"{name}: already past, skipped")
                    continue
                if not await self._run_round(rnd):
                    return           # the reason is already logged and said
            # OVERTIME. The rounds ran out with nobody at the ceiling, which
            # at these stakes is the minority case but never zero.
            if await self._run_sudden():
                return
            self._link_notes.append("every round is done")
            await self._link_say("🏁 **That's every round.**")
            return
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            self.engine._log("error", f"rounds stopped: {e}")
            self._link_notes.append(f"rounds stopped: {e}")
        finally:
            self._rounds_task = None

    async def _spread_open(self, rnd: dict):
        """Take both bets before the round plays. Returns the live view, or
        None when this round has no bet on it."""
        bet = rnd.get("spread_bet") or {}
        s = self.link
        if not bet.get("enabled") or s is None or s.state != mp.S_MATCH:
            return None
        caps = self._mp_caps()
        gap = mp_games.spread_now(caps, s.link.me, s.link.peer)
        ctx = self._mp_ctx()
        names = {"host": ctx.get("multi_host_name") or "host",
                 "guest": ctx.get("multi_guest_name") or "guest"}
        uids = ({"host": s.link.owner, "guest": s.link.peer_owner} if s.is_host
                else {"host": s.link.peer_owner, "guest": s.link.owner})
        secs = max(10.0, float(mp_games._num(bet.get("seconds")) or 45))
        view = MpSpreadView(self, uids, names, gap, secs)
        self._register_view(view)
        # The gap is ANNOUNCED. A bet placed without knowing where you stand is
        # a guess; knowing it makes the number a calculation — and tells you
        # whether you want to take a hit this round or avoid one.
        body = (f"**{names['host']} {ctx.get('multi_host_capacity', '0')}%** · "
                f"**{names['guest']} {ctx.get('multi_guest_capacity', '0')}%**\n"
                f"You are **{gap:g}% apart** right now.\n"
                f"Where will the gap be when this round ends? "
                f"Closest **without going over** takes it.")
        view.message = await self._link_say(
            body, embed=self._mp_card("🎯 Spread bet", body), view=view)
        try:
            await asyncio.wait_for(view.done.wait(), timeout=secs + 5)
        except asyncio.TimeoutError:
            pass
        for c in view.children:
            c.disabled = True
        try:
            if view.message is not None:
                await view.message.edit(view=view)
        except Exception:  # noqa: BLE001
            pass
        return view

    async def _spread_settle(self, rnd: dict, view) -> None:
        """Resolve the bet AFTER the round. The spread is measured FIRST and
        the payout applied second — paying first would move the very number
        the bet was placed on."""
        s = self.link
        if view is None or s is None or s.state != mp.S_MATCH:
            return
        bet = rnd.get("spread_bet") or {}
        stake = float(mp_games._num(bet.get("stake")) or 0)
        caps = self._mp_caps()
        actual = mp_games.spread_now(caps, s.link.me, s.link.peer)   # measured first
        seat_of = {"host": (s.link.me if s.is_host else s.link.peer),
                   "guest": (s.link.peer if s.is_host else s.link.me)}
        bets = {seat_of[k]: v for k, v in view.bets.items()}
        ctx = self._mp_ctx()
        r = mp_games.spread_outcome(bets, actual, s.link.me, s.link.peer)
        name_of = {s.link.me: ctx.get("multi_me_name") or "you",
                   s.link.peer: ctx.get("multi_peer_name") or "them"}
        shown = " · ".join(
            f"**{name_of[i]}** bet {bets.get(i, 0):g}%" for i in (s.link.me, s.link.peer))
        if r["push"]:
            head = f"🤝 Both bet {r['bets'][s.link.me]:g}% — push, nobody pays."
        elif r["both"]:
            head = f"💥 The gap came in at **{actual:g}%** — you both went over."
        else:
            head = (f"🎯 The gap came in at **{actual:g}%** — "
                    f"**{name_of[r['loser']]}** pays.")
        await self._link_say(f"{head}\n{shown}")
        if stake <= 0 or r["push"]:
            return
        # The loser pays the stake; the WINNER PAYS HALF. Both meters always
        # climb, so the ceiling stays reachable and the gap only moves by half
        # a stake — which is what keeps the next bet reasonable-about.
        scale = float(mp_games._num(ctx.get("multi_scale")) or 1)
        if r["both"]:
            pay = {s.link.me: stake, s.link.peer: stake}
        else:
            won = s.link.peer if r["loser"] == s.link.me else s.link.me
            pay = {r["loser"]: stake, won: stake / 2.0}
        for who, amt in pay.items():
            row = {"type": "fire", "fire_mode": "add",
                   "fill_pct": round(amt * scale, 2), "block_during": False,
                   "multi_who": "me" if who == s.link.me else "peer"}
            self.engine.mp_row_cb = self._mp_row_router
            try:
                await self.engine._run_action_block(
                    [row], name="multiplayer:spread", extra_ctx=dict(ctx))
            finally:
                self.engine.mp_row_cb = None

    async def _run_sudden(self) -> bool:
        """Sudden Death — overtime, run only if the rounds ended undecided.

        Deliberately NOT "turn both pumps on and wait". That decides nothing:
        whoever is closer to the ceiling reaches it first, so the loser is
        already fixed the moment overtime begins. This keeps playing a GAME
        that can land on either player, so the one behind can still catch up.

        It is a round with NO clear condition — it simply loops. Every pass
        puts percent on somebody, so the ceiling always arrives; the pass cap
        is only there because a block that can draw every time (rock-paper-
        scissors can) would otherwise be a hung match rather than a long one.

        Returns True if it ran, so the caller doesn't also announce a tidy
        finish to a match that just went to overtime.
        """
        s = self.link
        if s is None or s.state != mp.S_MATCH or s.conceded:
            return False
        rows = (self._mp_cfg().get("mp_sudden") or {}).get("actions") or []
        if not rows:
            return False                      # not set up; end the normal way
        self._link_notes.append("sudden death")
        await self._run_round({"name": "Sudden Death", "until": "count",
                               "count": mp_games.ROUND_CAP,
                               "actions": rows, "_i": len(
                                   mp_games.rounds_in_order(
                                       self._mp_cfg().get("mp_rounds") or []))})
        return True

    async def _run_round(self, rnd: dict) -> bool:
        """One band: open it, then loop its Action until it clears.

        Returns False when the band could NOT be cleared — a missing Action, a
        run that errored, or the pass limit. The caller must stop rather than
        re-enter: the same round would otherwise repeat forever.

        The round supplies `[multi_round_target]`; the Action reads it. That is
        what keeps an Action unbound from whatever ran it — the same roulette
        round works under any band, and under no band at all.
        """
        s = self.link
        name = str(rnd.get("name") or f"Round {rnd.get('_i', 0) + 1}")
        until = str(rnd.get("until") or "leader")
        s.round = int(rnd.get("_i", 0)) + 1
        s.round_target = float(rnd.get("max") or 0)
        self._link_notes.append(f"{name}: up to {s.round_target:g}%")

        # The round's own pre-show — different words, a sound cue, a scene
        # group. Same shape as a Go Live intro stage, and it runs ONCE.
        intro = rnd.get("intro") or []
        if intro:
            self.engine.mp_row_cb = self._mp_row_router
            self.engine.mp_ctx_cb = self._mp_ctx
            try:
                await self.engine._run_action_block(
                    intro, name=f"multiplayer:round:{name}:intro",
                    extra_ctx=self._mp_ctx(extra={"multi_round_name": name}))
            finally:
                self.engine.mp_row_cb = None
                self.engine.mp_ctx_cb = None

        view = await self._spread_open(rnd)
        body = rnd.get("actions") if isinstance(rnd.get("actions"), list) else []
        if not body:
            # Nothing to run is not a failure — it is a round you haven't
            # filled in yet. Skip it and carry on to the next; stopping the
            # whole match over a blank you left for later is the worse answer,
            # and announcing it puts your unfinished homework on the stream.
            self._link_notes.append(f"{name} is empty — skipped")
            return True

        # The round's BLOCK is what repeats. One mechanism: with a count of 1
        # it is a plain sequence, with 4 it runs four times, and with a target
        # it runs until somebody reaches it. A second "loop this one Action"
        # path was just a worse way of saying the same thing.
        cap = mp_games.round_passes(rnd)
        cleared = False
        for i in range(cap):
            if s.state != mp.S_MATCH:
                return False
            # EVERY PASS, not just between rounds. A round of six duels can
            # carry somebody past the lose-at capacity on the second one, and a
            # match that kept playing until the round happened to finish would
            # be ignoring the only thing that ends it.
            if await self._mp_end_check():
                return False
            if mp_games.round_cleared(rnd, self._mp_caps(), until, passes=i):
                cleared = True
                break
            self.engine.mp_row_cb = self._mp_row_router
            try:
                await self.engine._run_action_block(
                    body, name=f"multiplayer:round:{name}",
                    extra_ctx=self._mp_ctx(extra={"multi_round_name": name,
                                                  "multi_round_pass": str(i + 1)}))
            except Exception as e:  # noqa: BLE001
                self._link_notes.append(f"{name} stopped: {e}")
                await self._link_say(f"⚠ **{name}** stopped — {e}")
                return False
            finally:
                self.engine.mp_row_cb = None
        if not cleared:
            # The cap is a backstop, not a rule. Say so out loud rather than
            # moving on as though the band was cleared fairly.
            self._link_notes.append(f"{name} hit its pass limit ({cap})")
            await self._spread_settle(rnd, view)
            await self._link_say(f"⏭ **{name}** ran out of passes.")
            return False
        await self._spread_settle(rnd, view)
        done = str(rnd.get("done_message") or "").strip()
        if done:
            await self._link_say(self.engine.render(done, self._mp_ctx(
                extra={"multi_round_name": name})))
        return True

    # -- panel actions -------------------------------------------------------- #
    def link_status(self) -> dict:
        """One shape for the panel: ⚠ / ◌ / ✓ and everything behind them."""
        cfg = self._mp_cfg()
        s = self.link
        st = s.status(time.time()) if s is not None else {}
        return {"mode": str(cfg.get("mode") or "solo"),
                "connected": bool(self._client and self._client.user),
                "bot_network": self._mp_chan(cfg, "bot_network"),
                "broadcast": self._mp_chan(cfg, "broadcast"),
                "resume": dict(self._link_resume or {}),
                "notes": list(self._link_notes)[-12:],
                # This rig's own seconds-to-100%. The invite popup shows it
                # beside the host's and lets the guest correct it before
                # accepting — every target in the match is paced from it.
                "calibration": self._mp_calibration(),
                **st}

    async def link_offer(self, **kw) -> dict:
        """Host: invite the peer. Refuses with a reason rather than sending an
        invite that can only end in a decline."""
        s = self.link_session()
        if s is None:
            return {"ok": False, "error": "bot isn't connected"}
        faults = [c for c in await self.link_preflight() if c.get("ok") is False]
        if faults:
            return {"ok": False, "error": faults[0]["why"]}
        # The race's terms live in config, so the panel and the referee can
        # never disagree about what was offered. Explicit arguments still win,
        # which is what makes a one-off invite possible.
        cfg = self._mp_cfg()
        m = self._mp(cfg)
        # You need BOTH names to invite. Naming only the bot makes it possible
        # to start a match against the wrong person's install.
        if not str(m.get("peer_bot_name") or "").strip():
            return {"ok": False, "error": "name your opponent's bot first"}
        if not str(m.get("peer_player_name") or "").strip():
            return {"ok": False, "error": "name your opponent (the player) first"}
        want = str(m.get("peer_player_name") or "").strip().lower()
        seen = str(s.peer_player or "").strip().lower()
        if seen and want and seen != want:
            return {"ok": False,
                    "error": f"that bot belongs to {s.peer_player!r}, not "
                             f"{m.get('peer_player_name')!r} — check who you're inviting"}
        # The finish line is the TOP OF THE LAST BAND — that is where the game
        # actually ends, and it is what pace compensation and the guest's cost
        # estimate both need. There is no separate "target" to set.
        bands = mp_games.rounds_in_order(cfg.get("mp_rounds") or [])
        finish = max((b["max"] for b in bands), default=0)
        out = s.offer(time.time(), game=str(kw.get("game") or self._mp_game()),
                      base_target=kw.get("target") or finish,
                      rounds=len(bands) or None,
                      input=str(kw.get("input") or self._mp_input() or "operators"),
                      scene=str(cfg.get("chat_scene") or ""),
                      cap=self._mp_cap(),
                      split=bool(kw.get("split")),
                      venue=self._mp_venue(cfg))
        await self._link_apply(out)
        return {"ok": bool(out.send), "error": "" if out.send else "; ".join(out.notes),
                "status": self.link_status()}

    async def link_respond(self, accept: bool, why: str = "", video=None,
                           cal=None, split=False) -> dict:
        s = self.link
        if s is None:
            return {"ok": False, "error": "bot isn't connected"}
        out = s.respond(time.time(), bool(accept), why, video=video,
                        cal=cal, split=bool(split))
        await self._link_apply(out)
        # An accept that never linked was REFUSED (no camera, and soon others).
        # Saying ok:True there leaves the panel showing a match that isn't one.
        if accept and s.link.state == mp.S_INVITED:
            return {"ok": False, "error": "; ".join(out.notes) or "not accepted",
                    "status": self.link_status()}
        return {"ok": True, "status": self.link_status()}

    async def post_ready(self, seconds: float = 600.0) -> dict:
        """Host: put the Ready embed in the venue and wait for both presses.

        Anything a human does during a match happens in the broadcast channel —
        this is the first of those, and the panel's last act before the show
        drives itself.
        """
        s = self.link
        if s is None or not s.is_host:
            return {"ok": False, "error": "only the host posts the ready check"}
        if s.state not in (mp.S_LINKED, mp.S_READY):
            return {"ok": False, "error": f"not linked yet ({s.state})"}
        if self._ready_view is not None and not self._ready_view.done.is_set():
            return {"ok": False, "error": "a ready check is already up"}
        ctx = self._mp_ctx()
        view = MpReadyView(self, host_uid=s.link.owner, guest_uid=s.link.peer_owner,
                           host_name=ctx.get("multi_me_name") or "Host",
                           guest_name=ctx.get("multi_peer_name") or "Guest",
                           timeout=seconds)
        self._ready_view = view
        self._register_view(view)
        body = (f"**{s.game or 'The match'}** is set up. Both players press your "
                f"own button when you're ready.\n_Cameras stay dark until then._")
        view.message = await self._link_say(body, embed=self._mp_card("Ready?", body),
                                            view=view)
        asyncio.create_task(self._ready_wait(view, seconds))
        return {"ok": True, "status": self.link_status()}

    async def _ready_wait(self, view, seconds: float) -> None:
        """Both pressed → arm and let the host's intro run. Nobody pressed →
        say so rather than leaving an embed up that does nothing."""
        try:
            await asyncio.wait_for(view.done.wait(), timeout=seconds + 5)
        except asyncio.TimeoutError:
            pass
        s = self.link
        if s is None or s.state not in (mp.S_LINKED, mp.S_READY):
            return
        if not all(view.ready.values()):
            missing = [view.names[k] for k, v in view.ready.items() if not v]
            self._link_notes.append(f"ready check timed out — {', '.join(missing)}")
            await self._link_say(f"⏳ Never heard from {', '.join(missing)}.")
            return
        self._link_notes.append("both players ready")
        await self._link_apply(s.arm(time.time()))
        await self._link_say("✅ **Both ready.**")
        # From here the show drives itself — that was the hard constraint from
        # the start: a match runs beginning to end with both panels closed.
        await self.start_match()

    async def link_standby(self) -> dict:
        """Going LIVE in multiplayer: say hello on bot_network and WAIT.

        This is the whole separation — the install is discoverable and gated,
        but no session has started. It advertises so a host's invite picker can
        find it; it does nothing else until an invite is accepted.
        """
        s = self.link
        if s is None:
            return {"ok": False, "error": "bot isn't connected"}
        await self._link_apply(s.start_advertising(time.time()))
        self._link_notes.append("standing by — waiting for a match")
        return {"ok": True, "status": self.link_status()}

    async def link_offair(self, why: str = "went off air") -> dict:
        """LIVE off. Tell the peer rather than letting them time out."""
        s = self.link
        if s is None:
            return {"ok": True}
        if s.link.state in mp.LIVE:
            # Walking away is conceding by other means. Same End Condition,
            # same block — the match does not get a different ending because
            # somebody reached for the switch instead of the button.
            await self._link_apply(s.concede(time.time(), why))
            await self._mp_run_end()
        else:
            s.stop_advertising()
        return {"ok": True, "status": self.link_status()}

    async def link_arm(self) -> dict:
        s = self.link
        if s is None:
            return {"ok": False, "error": "bot isn't connected"}
        await self._link_apply(s.arm(time.time()))
        return {"ok": True, "status": self.link_status()}

    async def link_concede(self, why: str = "conceded", who: str = "") -> dict:
        """Give up — from the panel button, from !concede, or by hitting the
        lose-at capacity. All three land here, and all three run the SAME End
        Condition block, because they are the same event: somebody lost.
        """
        s = self.link
        if s is None or s.state not in mp.LIVE:
            return {"ok": False, "error": "no match to concede"}
        await self._link_apply(s.concede(time.time(), why, who=who))
        await self._mp_run_end()
        return {"ok": True, "status": self.link_status()}

    async def _mp_end_check(self) -> bool:
        """Has anyone hit the lose-at capacity? Getting there first IS
        conceding, so it ends the match the same way and runs the same block.
        """
        s = self.link
        if s is None or s.state != mp.S_MATCH:
            return False
        if s.conceded:
            await self._mp_run_end()
            return True
        top = mp_games.end_capacity(self._mp_cfg().get("mp_end") or {})
        if top <= 0:
            return False
        who = mp_games.end_loser(self._mp_caps(), top)
        if not who:
            return False
        await self._link_apply(s.concede(time.time(), f"reached {top:g}%",
                                         who=who))
        await self._mp_run_end()
        return True

    async def _mp_run_end(self) -> None:
        """The End Condition: its action block, then the match is over.

        Always last, never one of the rounds — a round is a band you clear,
        this is how the whole thing stops. It runs on the install that is
        driving; the block's own rows cross to the other machine the same way
        any round's do.
        """
        s = self.link
        if s is None or self._mp_ended_sid == s.link.sid:
            return
        self._mp_ended_sid = s.link.sid
        await self.rounds_stop()          # nothing else may run over the outro
        end = self._mp_cfg().get("mp_end") or {}
        rows = end.get("actions") if isinstance(end.get("actions"), list) else []
        if rows:
            self.engine.mp_row_cb = self._mp_row_router
            self.engine.mp_ctx_cb = self._mp_ctx
            try:
                await self.engine._run_action_block(
                    rows, name="multiplayer:end", extra_ctx=self._mp_ctx())
            except Exception as e:  # noqa: BLE001 — the match still has to end
                self.engine._log("error", f"multiplayer: end condition failed: {e}")
            finally:
                self.engine.mp_row_cb = None
                self.engine.mp_ctx_cb = None
        else:
            await self._link_say(f"🏳 **{self._mp_loser_name()}** conceded — "
                                 f"that's the match.")
        await self._link_apply(s.abort(time.time(), s.conceded_why or "conceded"))

    def _mp_loser_name(self) -> str:
        s = self.link
        if s is None or not s.conceded:
            return "nobody"
        return (s.player if s.conceded == s.link.me
                else (s.peer_player or s.link.peer_name or "the other player"))

    async def link_abort(self, why: str = "stopped from the panel") -> dict:
        s = self.link
        if s is None:
            return {"ok": False, "error": "no session"}
        await self._link_apply(s.abort(time.time(), why))
        return {"ok": True, "status": self.link_status()}

    def _mp_save_blocked(self) -> None:
        """A block that only lives in memory is not a block — it would be gone
        the next time the bot reconnects."""
        s = self.link
        if s is None:
            return
        blk = {"blocked": list(s.blocked)}
        self.engine.set_config(config_store.update({"multiplayer": blk}))

    async def link_block(self, bot_id: str = "") -> dict:
        s = self.link
        if s is None:
            return {"ok": False, "error": "bot isn't connected"}
        out = s.block(time.time(), bot_id=bot_id)
        # Persist BEFORE the envelopes go out. Applying first posts an abort,
        # the bot sees its own message, the envelope path rebuilds the session
        # from config — and the block, which is only in memory at that moment,
        # is read straight back out again.
        self._mp_save_blocked()
        await self._link_apply(out)
        return {"ok": True, "status": self.link_status()}

    async def link_unblock(self, bot_id: str) -> dict:
        s = self.link
        if s is None:
            return {"ok": False, "error": "bot isn't connected"}
        out = s.unblock(bot_id)
        self._mp_save_blocked()
        await self._link_apply(out)
        return {"ok": True, "status": self.link_status()}

    # -- resume ---------------------------------------------------------------- #
    def _match_path(self) -> str:
        return os.path.join(config_store.DATA_DIR, "match.json")

    def _link_persist(self) -> None:
        """data/match.json. Guest resume is nearly free — persist the cursor,
        read the tail on boot, replay by seq, and dedup does the rest."""
        s, path = self.link, self._match_path()
        try:
            if s is None or s.state in (mp.S_IDLE, mp.S_ADVERTISED):
                if os.path.exists(path):
                    os.remove(path)
                return
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(s.snapshot(), fh)
        except OSError as e:
            self.engine._log("error", f"multiplayer: couldn't save match state: {e}")

    def _link_find_resume(self) -> None:
        """Called once the bot is up. Never resumes on its own — it only offers,
        because rejoining a match silently is how a pump comes back on in a room
        nobody is watching."""
        try:
            with open(self._match_path(), "r", encoding="utf-8") as fh:
                snap = json.load(fh)
            if isinstance(snap, dict) and snap.get("sid"):
                self._link_resume = snap
                self._link_notes.append(
                    f"found an unfinished match with {snap.get('peer_name') or 'a peer'}"
                    + (f", round {snap.get('round')}" if snap.get("round") else ""))
        except (OSError, ValueError):
            self._link_resume = None

    async def link_resume(self, confirm: bool) -> dict:
        """Operator's answer to "Rejoin match with Dave, round 4 of 7?"."""
        snap, self._link_resume = self._link_resume, None
        s = self.link_session()
        if not confirm or not snap or s is None:
            try:
                os.remove(self._match_path())
            except OSError:
                pass
            return {"ok": True, "status": self.link_status()}
        await self._link_apply(s.restore(snap, time.time()))
        await self._link_catchup()
        return {"ok": True, "status": self.link_status()}

    async def _link_catchup(self) -> None:
        """Read the tail of bot_network and replay it. This is the single best
        property of using a channel as the wire: the log is still there, so
        reconnection costs nothing — no acks, no retry queue, no liveness
        dance. Dedup on (from, sid, seq) makes every replayed envelope inert."""
        s = self.link
        cid = self._mp_chan(self._mp_cfg(), "bot_network")
        ch = await self._channel(cid) if cid else None
        if s is None or ch is None:
            return
        try:
            tail = [m async for m in ch.history(limit=100)]
        except Exception as e:  # noqa: BLE001
            self.engine._log("error", f"multiplayer: couldn't read the tail: {e}")
            return
        for m in sorted(tail, key=lambda x: int(x.id)):
            env = mp.decode(m.content or "")
            if env is None or env["t"] in mp.NO_DEDUP:
                # hello and beat are the two types dedup deliberately doesn't
                # cover, which makes them the two a replay must skip: an old
                # hello in the history is not the peer coming back right now,
                # and reading it as one would abort the match we just rejoined.
                continue
            await self._link_apply(s.feed(env, time.time(),
                                          capacity=self.engine.capacity))

    # -- the match clock ------------------------------------------------------- #
    async def _link_loop(self) -> None:
        """Re-advertises, heartbeats, pushes telemetry, and enforces every
        timeout. One iteration failing must never end the loop — a clock that
        dies quietly is exactly the hung match this design is built to avoid."""
        # A quarter-second beat. Every timeout in tick_clock is absolute and
        # every push rate-limits itself, so a faster loop costs nothing — but a
        # 1-second one would make the 500ms dead-heat window meaningless, and
        # that window is what deletes every tie-break rule.
        while True:
            try:
                await asyncio.sleep(0.25)
                cfg = self._mp_cfg()
                if not self._mp_live(cfg):
                    continue
                s = self.link_session(cfg)
                if s is None:
                    continue
                now = time.time()
                # The panel's own meter reads off the session, so keep it live:
                # the host never pushes `tele`, so nothing else would update it.
                s.capacity = float(self.engine.capacity)
                # LIVE CAPACITY WATCH, on this install's own clock — four times
                # a second, on BOTH machines. The lose-at line is the first
                # thing watched here and anything else that has to react to a
                # meter crossing a number belongs here too: a round's pass loop
                # only comes round between actions, which can be a 15-second
                # wait, and a pump does not stop climbing while it waits.
                if s.state in mp.LIVE and not s.conceded:
                    ended = s.check_end(now)
                    if ended:
                        await self._link_apply(ended)
                        await self._mp_run_end()
                        continue
                if not s.advertising and self._mp_chan(cfg, "bot_network") \
                        and s.state == mp.S_IDLE and not self._link_resume:
                    await self._link_apply(s.start_advertising(now))
                    continue
                await self._link_apply(s.tick_clock(now))
                if s.state == mp.S_MATCH and not s.is_host:
                    # Interval telemetry is for METERS. Idle costs nothing:
                    # capacity doesn't move when no pump is running.
                    await self._link_apply(s.push_tele(
                        now, self.engine.capacity, firing=bool(self.engine._fires)))
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                self.engine._log("error", f"multiplayer clock: {e}")

    def _mp_guest_muted(self, cfg) -> bool:
        """During a match the GUEST resolves nothing — the host is the only side
        that adjudicates. Two referees is worse than none."""
        s = self.link
        return bool(self._mp_live(cfg) and s is not None
                    and s.state in mp.LIVE and not s.is_host)

    # ══ end Multiplayer ═════════════════════════════════════════════════════ #

    async def _handle(self, client: discord.Client, message: discord.Message) -> None:
        # The protocol path runs FIRST and returns early — ahead of the Chat
        # capture and ahead of the bot filter below, which would otherwise drop
        # every envelope on the floor (they all come from a bot).
        if self._link_envelope(message):
            return
        self._chat_capture(message)   # Chat tab log — before ANY filtering
        if message.author.bot or (client.user and message.author.id == client.user.id):
            return
        cfg = self.get_config()
        if self._mp_guest_muted(cfg):
            return
        prefix = cfg.get("command_prefix", "!")
        content = (message.content or "").strip()
        # Owner Commands: the OWNER typing "#name" posts that macro attributed
        # to them ("**Owner:** …"). Everyone else's #… is ignored quietly.
        if content.startswith("#") and prefix != "#":
            nm = content[1:].split(" ", 1)[0].lower()
            oc = self.engine.find_owner_command(nm)
            if oc is not None:
                if not cfg.get("listener_enabled") or not self._allowed(cfg, message):
                    return
                who = message.author.display_name
                if not self.engine.is_owner(message.author.id, who):
                    return
                body = self.engine.render((oc.get("message") or "").strip(),
                                          {"user": who, "mention": message.author.mention})
                if body:
                    await self.owner_say(message.channel, body)
                return
        if not content.startswith(prefix):
            return
        cmd = content[len(prefix):].split(" ", 1)[0].lower()
        # !concede — the other way to give up, for a player whose hands are on
        # a pump rather than on the panel. Scoped to the TWO PLAYERS: the
        # audience does not get to end somebody else's match.
        if cmd == "concede":
            s_ = self.link
            if s_ is None or s_.state not in mp.LIVE:
                return
            who_id = str(message.author.id)
            players = {str(s_.link.owner or ""), str(s_.link.peer_owner or "")}
            if who_id not in players or not who_id:
                return
            mine = who_id == str(s_.link.owner or "")
            await self.link_concede("conceded in chat",
                                    who=s_.link.me if mine else s_.link.peer)
            return
        bn = self.engine.builtin_names()               # {capacity,help,…} → names
        action = next((k for k, v in bn.items() if v == cmd), None)
        custom = self.engine.find_command(cmd)
        if custom is not None and not custom.get("enabled", True):
            custom = None  # disabled command → ignore quietly
        if action is None and custom is None:
            return
        if not self._allowed(cfg, message):
            return
        if not cfg.get("listener_enabled"):
            # Activation off = say nothing. The command isn't going to run, so
            # there is nothing to report; the operator already knows they
            # turned it off. This used to reply "🔇 Activation is currently
            # off." to every command.
            return

        who = message.author.display_name

        # Anti-spam buffer: quietly ignore one PERSON spamming the SAME command
        # back-to-back. Keyed per-user, so different people are never buffered
        # against each other.
        bufkey = custom.get("name", "").lower() if custom is not None else action
        if not self.engine.buffer_ok(f"buf:{bufkey}:{message.author.id}"):
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
            txt = self.engine.render(tmpl)
            await _reply(message, txt, embed=self._cfg_embed(cfg, "pumptimer", txt, "⏱️ Pump timer"))
            return

        if action == "capacity":
            rd, rs = self.engine.range_dice(self.engine.range_for(self.engine.capacity))
            default = "📊 Capacity **[capacity]%**\n[capacity_bar]\nRolling **[dice]** · [announce]"
            tmpl = cfg.get("capacity_message") or default
            txt = self.engine.render(tmpl, {"dice": f"{rd}d{rs}", "sides": rs}) or "📊"
            await _reply(message, txt, embed=self._cfg_embed(cfg, "capacity", txt, "📊 Capacity")
                         or self._status_embed("capacity", txt))
            return

        if custom is not None:
            if custom.get("owner_only") and not self.engine.is_owner(message.author.id, who):
                return  # owner-only command → silently ignore for everyone else
            res = await self.engine.run_custom_collected(custom, who, uid=str(message.author.id))
            if res.get("silent"):
                return  # gated out (wrong range) → ignore quietly
            if not res.get("ok"):
                # cooldown/out-of-uses/paused messages go through as-is; other errors get a ⚠️
                if res.get("cooldown"):
                    await _reply(message, res["error"],
                                 embed=self._cfg_embed(cfg, "cooldown", res["error"], "⏳ Cooldown"))
                elif res.get("paused"):
                    await _reply(message, res["error"],
                                 embed=self._cfg_embed(cfg, "pause", res["error"], "⏸️ Session paused"))
                elif res.get("used_up"):
                    await _reply(message, res["error"])
                else:
                    await _reply(message, f"⚠️ {res.get('error', 'could not run')}")
                return
            if res.get("game"):
                # Minigame: post the public Play button (locked to the author). The
                # game itself runs ephemerally; the result is broadcast at the end.
                # the minigame ACTION's own row when there is one, else the command
                gcmd = {**(res.get("game_cmd") or custom),
                        "__resume": res.get("resume_token")}
                glabel = self.engine.game_display_name(gcmd)
                intro = (res.get("reply") or "").strip() or f"🎮 **{who}** started **{custom.get('name')}** — press Play!"
                view = minigames.make_play_view(self, gcmd, who, str(message.author.id))
                try:
                    if custom.get("game_intro_embed"):
                        ttl = self.engine.render(
                            (custom.get("game_intro_title") or "").strip() or f"🎮 {glabel}",
                            {"user": who, "mention": message.author.mention})
                        emb = discord.Embed(title=ttl[:256], description=intro[:4096], color=0x9B59B6)
                        view.message = await message.channel.send(embed=emb, view=view)
                    else:
                        itxt = self._hdr(cfg, glabel, who) + intro
                        iemb = _auto_embed(itxt) or self._out_embed(cfg, glabel, who, intro)
                        view.message = await (message.channel.send(embed=iemb, view=view) if iemb
                                              else message.channel.send(itxt, view=view))
                except Exception as e:  # noqa: BLE001
                    await _reply(message, f"⚠️ couldn't start the game: {e}")
                return
            # ONE POST PER COMMAND. The reply, everything the action block said
            # while it ran (res["posts"]) and any event-activation lines are the
            # same event as far as a reader is concerned — they used to arrive
            # as three or four separate messages seconds apart.
            posts = [p for p in (res.get("posts") or []) if str(p or "").strip()]
            evt_lines, evt_special = [], []
            for post in (res.get("events_posted") or []):
                # a replace_key / image post has to stay its own message: the
                # next round deletes it by that key
                if isinstance(post, dict):
                    evt_special.append(post)
                elif str(post or "").strip():
                    evt_lines.append(str(post))
            line = "\n".join([x for x in [res["reply"], *posts, *evt_lines] if str(x or "").strip()])
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
                body = (line + tail).strip()
                # An action-driven command usually has no `reply` of its own —
                # the block does the talking. Posting the header anyway put a
                # bare "**[inflate · Stan]**" in chat with nothing under it.
                if body:
                    await _reply(message, self._hdr(cfg, label, who) + body,
                                 as_reply=bool(custom.get("mention")),
                                 embed=self._out_embed(cfg, label, who, body))
                    echo_real = "\n".join([x for x in [res.get("reply"), *posts, *evt_lines]
                                           if str(x or "").strip()])
                    echo_anon = "\n".join([x for x in [res.get("reply_anon"), *posts, *evt_lines]
                                           if str(x or "").strip()])
                    await self._echo(message, echo_anon, tail, echo_real, label=label)
            # A clean_previous loop's first round arrives as a dict carrying its
            # replace_key so subsequent rounds can delete it — those can't be
            # folded in, so they still post on their own.
            for post in evt_special:
                await self.broadcast(post.get("text", ""), post.get("image"),
                                     replace_key=post.get("replace_key"))
            return
        # (no trailing builtin here: the old chat dice-roll is a custom command now)

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
        for t in self._broadcast_targets(cfg):
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
    # Nothing to say = nothing posted. A blank reply used to go out as an empty
    # message with just the bot's name on it.
    if embed is None and not str(text or "").strip():
        return
    embed = _auto_embed(text, embed)
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
