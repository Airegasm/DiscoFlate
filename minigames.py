"""
minigames.py — button/ephemeral minigames for DiscoFlate (discord.py 2.x).

A minigame command posts a public "Play" button (locked to whoever ran it).
Clicking it opens an EPHEMERAL game (only that player sees it). When the game
ends, the tiered result is broadcast to the channel and any pump fire is credited
to the player — reusing the engine's outcome machinery (game_payoff / _run_fires).

Two games:
  * game-pushluck — Pump for points at a rising bust chance, or Bank to lock in.
  * game-simon    — Repeat a growing emoji sequence shown for a few seconds.
"""
from __future__ import annotations

import asyncio
import random

import discord

SIMON_SYMBOLS = ["🔴", "🟢", "🔵", "🟡", "🟣", "🟠"]


def make_play_view(bot, cmd, who, uid):
    return PlayView(bot, cmd, who, uid)


class PlayView(discord.ui.View):
    """Public message with one Play button, locked to the command's author."""

    def __init__(self, bot, cmd, who, uid):
        super().__init__(timeout=180)
        self.bot = bot
        self.cmd = cmd
        self.who = who
        self.uid = uid
        try:
            self.starter_id = int(uid) if uid else None
        except (TypeError, ValueError):
            self.starter_id = None

    @discord.ui.button(label="▶ Play", style=discord.ButtonStyle.primary)
    async def play(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.starter_id is not None and interaction.user.id != self.starter_id:
            await interaction.response.send_message(
                "This isn't your game — run the command yourself to play.", ephemeral=True)
            return
        typ = (self.cmd.get("type") or "").lower()
        game = {"game-pushluck": PushLuckView, "game-simon": SimonView,
                "game-balloon": BalloonView, "game-rps": RpsView}.get(typ)
        if game is None:
            await interaction.response.send_message("Unknown game.", ephemeral=True)
            return
        await game(self.bot, self.cmd, self.who, self.uid).begin(interaction)
        button.disabled = True
        try:
            await interaction.message.edit(view=self)   # grey out the public Play button
        except Exception:
            pass

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True


class PushLuckView(discord.ui.View):
    def __init__(self, bot, cmd, who, uid):
        super().__init__(timeout=120)
        self.bot = bot
        self.cmd = cmd
        self.who = who
        self.uid = uid
        self.pumps = 0
        self.score = 0.0
        self.max_pumps = max(1, int(cmd.get("pl_max_pumps") or 8))
        try:
            self.points = float(cmd.get("pl_points") or 1)
        except (TypeError, ValueError):
            self.points = 1.0

    def _bust_pct(self):
        return self.bot.engine.pushluck_bust_pct(self.cmd, self.pumps)

    def _state(self):
        return (f"💨 **{self.cmd.get('name', 'Push Your Luck')}**\n"
                f"Pumps: **{self.pumps}** · Score: **{self.score:g}**\n"
                f"Next pump busts at **{self._bust_pct():.0f}%** — Pump for more, or Bank it?")

    async def begin(self, interaction: discord.Interaction):
        await interaction.response.send_message(self._state(), view=self, ephemeral=True)

    @discord.ui.button(label="💨 Pump", style=discord.ButtonStyle.danger)
    async def pump(self, interaction: discord.Interaction, button: discord.ui.Button):
        if random.random() * 100 < self._bust_pct():
            self.pumps += 1
            self.score = 0.0
            await self._finish(interaction, busted=True)
            return
        self.pumps += 1
        self.score += self.points
        if self.pumps >= self.max_pumps:
            await self._finish(interaction, busted=False, note="Max pumps — auto-banked!")
        else:
            await interaction.response.edit_message(content=self._state(), view=self)

    @discord.ui.button(label="🏦 Bank", style=discord.ButtonStyle.success)
    async def bank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, busted=False)

    async def _finish(self, interaction, busted, note=""):
        for c in self.children:
            c.disabled = True
        self.stop()
        head = ("💥 **BUST!** You lost it all." if busted
                else f"🏦 Banked **{self.score:g}**! {note}".strip())
        try:
            await interaction.response.edit_message(content=head, view=self)
        except Exception:
            pass
        await self.bot.game_payoff(self.cmd, self.score, self.who, self.uid)


class SimonView(discord.ui.View):
    def __init__(self, bot, cmd, who, uid):
        super().__init__(timeout=300)
        self.bot = bot
        self.cmd = cmd
        self.who = who
        self.uid = uid
        n = max(2, min(6, int(cmd.get("sm_symbols") or 4)))
        self.symbols = SIMON_SYMBOLS[:n]
        self.max_rounds = max(1, int(cmd.get("sm_max_rounds") or 8))
        try:
            self.reveal = max(1.0, float(cmd.get("sm_reveal") or 3))
        except (TypeError, ValueError):
            self.reveal = 3.0
        self.sequence = []
        self.entered = []
        self.score = 0
        self._interaction = None

    def _add(self):
        self.sequence.append(random.choice(self.symbols))

    def _reveal_text(self, prefix=""):
        return (f"{prefix}🧠 **{self.cmd.get('name', 'Simon')}** — memorize (round {len(self.sequence)}):\n\n"
                f"# {'  '.join(self.sequence)}\n\n*(hiding in {self.reveal:g}s…)*")

    async def begin(self, interaction: discord.Interaction):
        self._interaction = interaction
        self._add()
        await interaction.response.send_message(self._reveal_text(), ephemeral=True)
        asyncio.create_task(self._reveal_then_input())

    async def _reveal_then_input(self):
        try:
            await asyncio.sleep(self.reveal)
            self.entered = []
            self._build_buttons()
            await self._interaction.edit_original_response(
                content=f"🧠 **{self.cmd.get('name', 'Simon')}** — now repeat it! (round {len(self.sequence)})",
                view=self)
        except Exception:
            pass

    def _build_buttons(self):
        self.clear_items()
        for sym in self.symbols:
            self.add_item(SimonButton(sym))

    async def press(self, interaction: discord.Interaction, sym: str):
        idx = len(self.entered)
        if idx >= len(self.sequence) or self.sequence[idx] != sym:
            # wrong → game over; score = fully-completed rounds
            for c in self.children:
                c.disabled = True
            self.stop()
            await interaction.response.edit_message(
                content=f"❌ Wrong! You reached round **{self.score}**.", view=self)
            await self.bot.game_payoff(self.cmd, self.score, self.who, self.uid)
            return
        self.entered.append(sym)
        if len(self.entered) < len(self.sequence):
            await interaction.response.edit_message(
                content=f"🧠 **{self.cmd.get('name', 'Simon')}** — keep going… "
                        f"({len(self.entered)}/{len(self.sequence)})", view=self)
            return
        # round cleared
        self.score = len(self.sequence)
        if len(self.sequence) >= self.max_rounds:
            for c in self.children:
                c.disabled = True
            self.stop()
            await interaction.response.edit_message(
                content=f"🏆 Perfect! You cleared all **{self.score}** rounds!", view=self)
            await self.bot.game_payoff(self.cmd, self.score, self.who, self.uid)
            return
        # next round: reveal the longer sequence, then re-arm input
        self._add()
        self.clear_items()
        self._interaction = interaction
        await interaction.response.edit_message(
            content=self._reveal_text(prefix=f"✅ Round {self.score} done!\n"), view=self)
        asyncio.create_task(self._reveal_then_input())


class SimonButton(discord.ui.Button):
    def __init__(self, sym: str):
        super().__init__(label=sym, style=discord.ButtonStyle.secondary)
        self.sym = sym

    async def callback(self, interaction: discord.Interaction):
        await self.view.press(interaction, self.sym)


class BalloonView(discord.ui.View):
    """Don't Pop It — reveal safe cells for points, avoid the hidden balloons."""

    def __init__(self, bot, cmd, who, uid):
        super().__init__(timeout=180)
        self.bot = bot
        self.cmd = cmd
        self.who = who
        self.uid = uid
        self.cells = max(2, min(20, int(cmd.get("bl_cells") or 9)))
        pops = max(1, min(self.cells - 1, int(cmd.get("bl_pops") or 3)))
        try:
            self.points = float(cmd.get("bl_points") or 1)
        except (TypeError, ValueError):
            self.points = 1.0
        self.score = 0.0
        self.revealed = 0
        idxs = list(range(self.cells))
        random.shuffle(idxs)
        self.mines = set(idxs[:pops])
        self._build()

    def _cash_row(self):
        return min(4, (self.cells - 1) // 5 + 1)

    def _build(self):
        self.clear_items()
        for k in range(self.cells):
            self.add_item(BalloonButton(k, k // 5))
        self.add_item(CashOutButton(self._cash_row()))

    def _state(self):
        return (f"🎈 **{self.cmd.get('name', 'Balloons')}** — {len(self.mines)} balloons hidden.\n"
                f"Reveal a safe cell (+{self.points:g}) or 🏦 Cash Out.  Score: **{self.score:g}**")

    async def begin(self, interaction: discord.Interaction):
        await interaction.response.send_message(self._state(), view=self, ephemeral=True)

    async def reveal(self, interaction, k, btn):
        if k in self.mines:
            for c in self.children:
                c.disabled = True
            self.stop()
            await interaction.response.edit_message(content="💥 **POP!** You hit a balloon — lost it all.", view=self)
            await self.bot.game_payoff(self.cmd, 0, self.who, self.uid)
            return
        self.revealed += 1
        self.score += self.points
        btn.disabled = True
        btn.label = "✅"
        btn.style = discord.ButtonStyle.success
        if self.revealed >= (self.cells - len(self.mines)):
            for c in self.children:
                c.disabled = True
            self.stop()
            await interaction.response.edit_message(content=f"🏆 Cleared the whole board — score **{self.score:g}**!", view=self)
            await self.bot.game_payoff(self.cmd, self.score, self.who, self.uid)
            return
        await interaction.response.edit_message(content=self._state(), view=self)

    async def cash_out(self, interaction):
        for c in self.children:
            c.disabled = True
        self.stop()
        await interaction.response.edit_message(content=f"🏦 Cashed out with **{self.score:g}**!", view=self)
        await self.bot.game_payoff(self.cmd, self.score, self.who, self.uid)


class BalloonButton(discord.ui.Button):
    def __init__(self, k, row):
        super().__init__(label="🎈", style=discord.ButtonStyle.secondary, row=row)
        self.k = k

    async def callback(self, interaction):
        await self.view.reveal(interaction, self.k, self)


class CashOutButton(discord.ui.Button):
    def __init__(self, row):
        super().__init__(label="🏦 Cash Out", style=discord.ButtonStyle.success, row=row)

    async def callback(self, interaction):
        await self.view.cash_out(interaction)


class RpsView(discord.ui.View):
    """Rock-Paper-Scissors — first to `rps_wins` rounds against the bot."""
    EMOJI = {"rock": "✊", "paper": "✋", "scissors": "✌️"}
    BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

    def __init__(self, bot, cmd, who, uid):
        super().__init__(timeout=180)
        self.bot = bot
        self.cmd = cmd
        self.who = who
        self.uid = uid
        self.target = max(1, int(cmd.get("rps_wins") or 3))
        self.pw = 0
        self.bw = 0

    def _state(self, last=""):
        tail = ("\n" + last) if last else ""
        return (f"✊✋✌️ **{self.cmd.get('name', 'RPS')}** — first to **{self.target}**.\n"
                f"You **{self.pw}** – **{self.bw}** Bot{tail}\nPick your throw:")

    async def begin(self, interaction: discord.Interaction):
        await interaction.response.send_message(self._state(), view=self, ephemeral=True)

    async def throw(self, interaction, choice):
        bot_choice = random.choice(list(self.EMOJI))
        if choice == bot_choice:
            last = f"🤝 Both {self.EMOJI[choice]} — tie."
        elif self.BEATS[choice] == bot_choice:
            self.pw += 1
            last = f"✅ {self.EMOJI[choice]} beats {self.EMOJI[bot_choice]}!"
        else:
            self.bw += 1
            last = f"❌ {self.EMOJI[bot_choice]} beats {self.EMOJI[choice]}."
        if self.pw >= self.target or self.bw >= self.target:
            for c in self.children:
                c.disabled = True
            self.stop()
            won = self.pw >= self.target
            head = (f"🏆 You won the match **{self.pw}–{self.bw}**!" if won
                    else f"💀 The bot took it **{self.bw}–{self.pw}**.")
            await interaction.response.edit_message(content=head, view=self)
            await self.bot.game_payoff(self.cmd, self.pw, self.who, self.uid)  # score = player round wins
            return
        await interaction.response.edit_message(content=self._state(last), view=self)

    @discord.ui.button(label="✊", style=discord.ButtonStyle.secondary)
    async def rock(self, interaction, button):
        await self.throw(interaction, "rock")

    @discord.ui.button(label="✋", style=discord.ButtonStyle.secondary)
    async def paper(self, interaction, button):
        await self.throw(interaction, "paper")

    @discord.ui.button(label="✌️", style=discord.ButtonStyle.secondary)
    async def scissors(self, interaction, button):
        await self.throw(interaction, "scissors")
