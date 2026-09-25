"""DiscoFlate — the multiplayer game pieces.

The spin, the dice, the duel, the targeting and the round bands. Pure, exactly
like the rail underneath them: these *decide*, the adapter *performs*. Nothing
here imports discord or engine, so a whole match can be played out in a test
with no pump, no token and no second person.

There is no hard-coded game. A game is **Rounds** made of **Actions** made of
action rows — so a new one is authored, not written, and adds **nothing to the
wire**.

See MULTIPLAYER.md.
"""

import random

import multiplayer as mp
from multiplayer import ROLE_HOST, _num

# ---- multiplayer actions: rows, targeting, placeholders --------------------- #
#
# A Multiplayer Action is a plain action BLOCK. It is never bound to whatever
# invoked it — the same "roulette round" drops into any game — because a row
# says WHO it acts on (`multi_who`) rather than naming a player, and reads the
# match out of `[multi_*]` placeholders rather than being handed one.

T_SPIN = "mp_spin"
T_ROLL = "mp_roll"
T_CHOICE = "mp_choice"
T_DUEL = "mp_duel"
T_CARDS = "mp_cards"
T_SIMON = "mp_simon"
T_TTT = "mp_ttt"
T_RUN = "mp_action"          # run a named Multiplayer Action inline
T_TELL = "mp_tell"           # ask the OTHER bot to run one of its own
RUN_DEPTH = 4                # an Action that runs itself must not spiral

# A Player Choice BLOCKS the block it sits in — that is the whole point of
# "Double or Nothing": nothing may fire until the player has answered. So it
# must always be able to end on its own. A choice with no deadline would hang
# the match on someone who walked away from their keyboard, which is the exact
# failure every other timeout in this design exists to prevent.
CHOICE_SECONDS = 30.0
CHOICE_MAX = 180.0

# Multiplayer's dice roll only ROLLS. Solo's `roll` row fires its total as
# seconds, and seconds are refused over the wire — they are a different amount
# of inflation on every rig. So the roll publishes a number and a separate fire
# row spends it as a PERCENT, which is also why the two are separate rows in
# the first place: the same roll can drive a message, an overlay, or nothing.
ROLL_DICE, ROLL_SIDES = 1, 7

# Which MACHINE a row acts on. Blank = this one, and nothing crosses the wire.
#
# `me`, `host` and `guest` still resolve, for a config that already used them,
# but the panel no longer offers them: only the host runs a block, so `host`
# was always this machine and `guest` was always the other one.
WHO = ("chosen", "other", "both", "host", "guest", "me", "peer",
       "leader", "trailer", "winner", "loser")

# What one install may ask the other to do.
#
# OVERLAYS cross because the shipped scene is the SAME FILE on both machines —
# it regenerates from defaults exactly like the solo one — so an overlay id is
# a shared vocabulary, not a reference to something only the host has. The
# handshake settles which vocabulary is in play (Versus or Gameshow) and the
# guest calls overlays out of that scene and nothing else.
#
# What still cannot cross is anything that would make the guest's bot SPEAK or
# run a game of its own: no commands, no messages, no rounds. The host narrates
# for both, so a post from the guest would be the same news twice.
CROSSABLE = frozenset({
    "fire", "roll", "stop_devices", "camera", "mp_action",
    "overlay", "overlay_kill", "update_overlay_text",
    "scene_group", "scene_group_kill",
})


def spin(targets, weights=None, pick=None):
    """The roulette. Returns (chosen, odds) where odds maps id -> probability.

    Even by default. `weights` tilts it — the hook the open "what feeds the
    weighting" question will eventually use — and the caller is expected to say
    the odds out loud whenever they aren't even, because a wheel the audience
    can't see is indistinguishable from a rigged one.

    `pick` is a 0..1 float for tests; production passes None and gets random.
    """
    ids = [str(t) for t in (targets or []) if str(t or "")]
    if not ids:
        return "", {}
    w = {}
    for i in ids:
        v = _num((weights or {}).get(i))
        w[i] = float(v) if (v is not None and v > 0) else 1.0
    total = sum(w.values()) or float(len(ids))
    odds = {i: w[i] / total for i in ids}
    r = random.random() if pick is None else max(0.0, min(0.999999, float(pick)))
    acc = 0.0
    for i in ids:
        acc += odds[i]
        if r < acc:
            return i, odds
    return ids[-1], odds


def roll_dice(dice=ROLL_DICE, sides=ROLL_SIDES, luck=0, rolls=None):
    """NdN with an optional luck nudge. `rolls` injects results for tests.

    Returns (total, faces). Luck is a percentage chance of forcing the best
    (positive) or worst (negative) possible total — the same shape solo's dice
    use, so an operator who knows one knows the other.
    """
    d = max(1, int(_num(dice) or ROLL_DICE))
    sd = max(2, int(_num(sides) or ROLL_SIDES))
    if rolls is not None:
        faces = [max(1, min(sd, int(x))) for x in rolls][:d]
        while len(faces) < d:
            faces.append(1)
    else:
        faces = [random.randint(1, sd) for _ in range(d)]
    total = sum(faces)
    lk = float(_num(luck) or 0)
    if lk > 0 and random.random() * 100 < lk:
        total, faces = d * sd, [sd] * d
    elif lk < 0 and random.random() * 100 < min(100.0, -lk):
        total, faces = d, [1] * d
    return total, faces


def choice_deadline(seconds) -> float:
    """A choice's deadline, clamped. Blank or nonsense gets the default rather
    than forever; nothing gets more than CHOICE_MAX."""
    v = _num(seconds)
    if v is None or v <= 0:
        return CHOICE_SECONDS
    return min(float(v), CHOICE_MAX)


def choice_options(row) -> list:
    """The buttons, normalised. A choice needs at least two — one button is not
    a decision — and every option carries the value later rows read out of
    `[multi_choice]`."""
    out = []
    for i, o in enumerate(row.get("options") or []):
        if not isinstance(o, dict):
            continue
        label = str(o.get("label") or "").strip()
        if not label:
            continue
        out.append({"label": label[:80],
                    "value": str(o.get("value") or label).strip().lower(),
                    "style": str(o.get("style") or "secondary").lower(),
                    "actions": o.get("actions") if isinstance(o.get("actions"), list) else []})
    return out[:5]              # Discord allows five buttons on a row


def duel_options(row) -> list:
    """The moves, normalised. Each carries what it BEATS, so the game is data
    rather than code — rock/paper/scissors and anything else shaped like it use
    the same row."""
    out = []
    for o in (row.get("options") or []):
        if not isinstance(o, dict):
            continue
        label = str(o.get("label") or "").strip()
        if not label:
            continue
        val = str(o.get("value") or label).strip().lower()
        beats = [str(b).strip().lower() for b in (o.get("beats") or []) if str(b).strip()]
        out.append({"label": label[:80], "value": val, "beats": beats,
                    "style": str(o.get("style") or "secondary").lower()})
    return out[:5]              # five buttons on a row


def duel_winner(mine, theirs, options):
    """Who won: "me", "peer", or "" for a draw.

    A walkover counts: if only one of them answered, they win. The alternative
    is a round that stalls on somebody who walked away, and every other timeout
    in this design exists to stop exactly that.
    """
    a = str(mine or "").strip().lower()
    b = str(theirs or "").strip().lower()
    if not a and not b:
        return ""                       # nobody played — nothing to decide
    if a and not b:
        return "me"
    if b and not a:
        return "peer"
    if a == b:
        return ""
    beats = {o["value"]: set(o["beats"]) for o in duel_options({"options": options})}
    if b in beats.get(a, ()):
        return "me"
    if a in beats.get(b, ()):
        return "peer"
    return ""                           # unrelated moves are a draw, not a crash


def resolve_who(word, *, me, peer, chosen="", caps=None, role="",
                winner="", loser=""):
    """`multi_who` -> the install ids it means. Always a list: `both` is a
    normal answer, and an unknown word resolves to nobody rather than
    defaulting to somebody — silently inflating the wrong person is the one
    outcome worth refusing."""
    w = str(word or "").strip().lower()
    if not w:
        return []
    caps = caps or {}
    if w == "both":
        return [me, peer]
    if w == "me":
        return [me]
    if w == "peer":
        return [peer]
    if w == "chosen":
        return [str(chosen)] if chosen else []
    if w == "winner":
        return [str(winner)] if winner else []
    if w == "loser":
        # A draw resolves to NOBODY, so a row aimed at the loser of a drawn
        # round does nothing rather than punishing someone arbitrarily.
        return [str(loser)] if loser else []
    if w == "other":
        if not chosen:
            return []
        return [peer if str(chosen) == me else me]
    if w == "host":
        return [me if role == ROLE_HOST else peer]
    if w == "guest":
        return [peer if role == ROLE_HOST else me]
    if w in ("leader", "trailer"):
        a, b = float(caps.get(me) or 0), float(caps.get(peer) or 0)
        if a == b:
            return []                 # level: no leader to name, so name nobody
        ahead = me if a > b else peer
        return [ahead] if w == "leader" else [peer if ahead == me else me]
    return []


def placeholders(session, chosen="", extra=None) -> dict:
    """Every `[multi_*]` value, as the render context wants them.

    All multiplayer placeholders carry the `multi_` prefix so a block can never
    confuse "the other player's capacity" with this install's own `[capacity]`.
    The action system's own publications ([result], [secs], [dice]) are
    untouched and keep working — they belong to the rows, not to multiplayer.
    """
    s = session
    me, peer = s.link.me, s.link.peer
    caps = {me: float(s.capacity or 0), peer: float(s.peer_cap or 0)}
    names = {me: s.player or "you", peer: s.peer_player or s.link.peer_name or "your opponent"}
    # The same line for both — percent is rig-independent, so there is only
    # ever one number here. Falls back to the lose-at capacity, which is what
    # actually ends the match.
    tgts = {}
    for k in (me, peer):
        v = _num((s.targets or {}).get(k))
        tgts[k] = float(v) if v is not None else float(s.end_max or 0)

    lead = ""
    if caps[me] != caps[peer]:
        lead = me if caps[me] > caps[peer] else peer
    trail = ("" if not lead else (peer if lead == me else me))
    other = "" if not chosen else (peer if str(chosen) == me else me)

    def pct(v):
        return f"{float(v):g}"

    out = {
        "multi_me": me, "multi_me_name": names[me],
        "multi_peer": peer, "multi_peer_name": names[peer],
        "multi_host_name": names[me] if s.is_host else names[peer],
        "multi_guest_name": names[peer] if s.is_host else names[me],
        "multi_my_pct": pct(caps[me]), "multi_peer_pct": pct(caps[peer]),
        "multi_my_target": pct(tgts[me]), "multi_peer_target": pct(tgts[peer]),
        "multi_leader": lead, "multi_leader_name": names.get(lead, "nobody"),
        "multi_leader_pct": pct(caps.get(lead, 0)) if lead else "0",
        "multi_trailer": trail, "multi_trailer_name": names.get(trail, "nobody"),
        "multi_trailer_pct": pct(caps.get(trail, 0)) if trail else "0",
        "multi_chosen": str(chosen or ""),
        "multi_chosen_name": names.get(str(chosen), "") if chosen else "",
        "multi_other": other, "multi_other_name": names.get(other, "") if other else "",
        "multi_game": s.game or "",
        "multi_round": str(s.round or 0),
        "multi_round_target": f"{float(_num(getattr(s, 'round_target', 0)) or 0):g}",
        "multi_role": s.link.role or "",
        "multi_sid": s.link.sid or "",
    }
    # SEAT-relative telemetry. The guest reports its own meter and its pump's
    # remaining seconds on the heartbeat, so a host-side overlay can show the
    # other rig without asking for anything. The host never reports back — it
    # narrates, so the guest has no use for the number.
    gcap = caps[peer] if s.is_host else caps[me]
    hcap = caps[me] if s.is_host else caps[peer]
    gpump = float(s.peer_pump or 0) if s.is_host else float(s.pump_left or 0)
    hpump = float(s.pump_left or 0) if s.is_host else float(s.peer_pump or 0)
    # WHO LOST, once the End Condition has a reason. Conceding, being first to
    # the lose-at capacity and going off air are the same event as far as this
    # is concerned: somebody gave up. Empty until one of them happens, so a
    # block can test it.
    lost = str(getattr(s, "conceded", "") or "")
    won = ""
    if lost:
        won = peer if lost == me else me
    out["multi_loser"] = names.get(lost, "") if lost else ""
    out["multi_winner"] = names.get(won, "") if won else ""
    out["multi_loser_seat"] = ("" if not lost else
                               ("host" if (lost == me) == s.is_host else "guest"))
    out["multi_end_why"] = str(getattr(s, "conceded_why", "") or "")

    # THE CEILING, AND THE SCALE IT IMPLIES.
    #
    # Stakes are authored against a 100% match and multiplied by this, so one
    # scene plays the same game at any ceiling: set 200 and every stake
    # doubles, set 80 and they shrink. It is the same idea as pace
    # compensation — express the number relative to the thing that matters
    # instead of in absolute units that don't travel.
    #
    # Snapshotted by construction: end_max is agreed at invite time and does
    # not move again, so a dial turned mid-match cannot re-price a deal both
    # players already agreed to. No ceiling set = ×1, never ×0.
    top = float(_num(getattr(s, "end_max", 0)) or 0)
    out["multi_max"] = f"{top:g}"
    out["multi_scale"] = f"{(top / 100.0) if top > 0 else 1.0:g}"

    out["multi_guest_capacity"] = pct(gcap)
    out["multi_host_capacity"] = pct(hcap)
    out["multi_guest_pump_timer"] = f"{gpump:.0f}"
    out["multi_host_pump_timer"] = f"{hpump:.0f}"

    # Whichever racer is nearest their OWN line — the only honest "who's
    # winning" when the two lines differ on purpose.
    fr = {k: (caps[k] / tgts[k]) if tgts[k] > 0 else 0.0 for k in (me, peer)}
    out["multi_my_frac"] = f"{fr[me] * 100:.0f}"
    out["multi_peer_frac"] = f"{fr[peer] * 100:.0f}"
    out.update(extra or {})
    return out


# ---- rounds: bands that loop -------------------------------------------- #
#
# A round is a band with an action block, the way a capacity range is a band
# with one. The difference is that a round LOOPS its action until the band is
# cleared, then hands over to the next round.

ROUND_CAP = 200          # passes per round, so a band nobody can clear still ends

# ---- the spread bet --------------------------------------------------------- #
#
# At a round's start both players predict THE GAP between them when it ends.
# Closest without going over takes it.
#
# What makes it more than a guess: once you have named a number, you want the
# round to land on it. Bet wide and you want to lose a hand; bet tight and you
# want it close. So throwing a duel on purpose can be correct play, and a luck
# round acquires a decision it did not have.
#
# The loser pays the stake and the WINNER PAYS HALF. That is a rubber band:
# both meters always climb, so the ceiling stays reachable, and the gap only
# moves by half a stake — which makes the spread predictable enough to reason
# about, which in turn makes the next bet a calculation instead of a guess.

SPREAD_MAX = 999


def spread_clamp(v) -> float:
    n = _num(v)
    return 0.0 if n is None else float(min(SPREAD_MAX, max(0, n)))


def spread_outcome(bets: dict, actual, a="a", b="b") -> dict:
    """Closest to `actual` WITHOUT going over.

    A bet nobody placed is 0 — which is a real bet ("you two will finish
    level"), usually a losing one, and never a way to stall the round or dodge
    the wager by walking away.

    Both over → the round beat them both, and they both pay. Same shape as
    both busting at blackjack, and for the same reason: a round that resolves
    to nothing is the outcome worth avoiding.
    """
    act = spread_clamp(actual)
    ba, bb = spread_clamp(bets.get(a)), spread_clamp(bets.get(b))
    out = {"actual": act, "bets": {a: ba, b: bb},
           "loser": "", "both": False, "push": False}
    over_a, over_b = ba > act, bb > act
    if over_a and over_b:
        out["both"] = True
    elif over_a:
        out["loser"] = a
    elif over_b:
        out["loser"] = b
    elif ba == bb:
        out["push"] = True
    else:
        out["loser"] = b if (act - ba) < (act - bb) else a
    return out


def spread_now(caps, a="a", b="b") -> float:
    """The gap right now — announced when the betting opens, because a bet you
    place without knowing where you stand is not a decision."""
    return abs(float(_num((caps or {}).get(a)) or 0)
               - float(_num((caps or {}).get(b)) or 0))


# ---- cards: blackjack, two seats, one dealer -------------------------------- #
#
# A REAL table, not a solitaire game with a second player bolted on. Both hands
# are face-up — which is how a shoe game actually deals — and only the dealer
# holds a hole card. Both players act at once against the same upcard.
#
# Face-up is also what makes it hard. Blackjack against a dealer is SOLVED:
# basic strategy gives one right answer for your total against their upcard.
# But when you can see the other player sitting on 20 and you are holding 17,
# the solved answer is "stand" and standing loses. Second place pays, so you
# have to hit into a bad spot. No strategy card covers that.

CARD_DECK = (2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 11)   # 11 = ace
DEALER_STANDS = 17


def card_draw(pick=None) -> int:
    """One card from an infinite shoe. `pick` (0..1) makes it testable."""
    if pick is None:
        return random.choice(CARD_DECK)
    i = int(max(0.0, min(0.999999, float(pick))) * len(CARD_DECK))
    return CARD_DECK[i]


def hand_total(cards) -> int:
    """Best total: aces soften from 11 to 1 only as far as needed."""
    t = sum(int(c) for c in (cards or []))
    aces = list(cards or []).count(11)
    while t > 21 and aces:
        t -= 10
        aces -= 1
    return t


def hand_text(cards) -> str:
    return ", ".join("A" if c == 11 else str(c) for c in (cards or []))


def dealer_play(cards, draws=None) -> list:
    """The dealer's rule, and it is only a rule — the house makes no choices.
    Hits to 17, then stops. `draws` injects cards for a test."""
    hand = list(cards or [])
    feed = list(draws or [])
    while hand_total(hand) < DEALER_STANDS and len(hand) < 12:
        hand.append(feed.pop(0) if feed else card_draw())
    return hand


def hand_score(cards, dealer) -> int:
    """What this hand is WORTH in the round: its total if it beat the dealer,
    zero if it busted or the dealer held it off.

    Zero rather than the raw total on purpose. A 20 that lost to a dealer 21 is
    worth exactly as much as a bust — nothing — which is what makes the dealer
    a shared threat rather than scenery.
    """
    mine, theirs = hand_total(cards), hand_total(dealer)
    if mine > 21:
        return 0
    if theirs <= 21 and theirs >= mine:
        return 0
    return mine


def cards_outcome(a_cards, b_cards, dealer, a="a", b="b") -> dict:
    """Who pays. Both scored against the dealer, then compared to each other.

    - one higher score       → the other pays
    - BOTH zero              → the house took both; they BOTH pay
    - equal and not zero     → push, nobody pays
    """
    sa, sb = hand_score(a_cards, dealer), hand_score(b_cards, dealer)
    out = {"scores": {a: sa, b: sb}, "loser": "", "both": False, "push": False,
           "dealer": hand_total(dealer)}
    if sa == 0 and sb == 0:
        out["both"] = True                 # the house cleaned up
    elif sa == sb:
        out["push"] = True
    else:
        out["loser"] = b if sa > sb else a
    return out


# ---- simon: memory, and it escalates itself --------------------------------- #
#
# A sequence is shown, hidden, and both players reproduce it. Each pass is one
# longer than the last, so the round needs no stake tuning to end — memory
# fails on its own, and it fails sooner the more inflated you are.
#
# Scored as a correct PREFIX, not right-or-wrong. Getting five of seven is a
# real result and should beat four of seven; all-or-nothing would throw away
# most of what happened and turn a memory game into a coin flip.

SIMON_PADS = ("🔴", "🟡", "🟢", "🔵")
SIMON_MAX = 12


def simon_sequence(length, picks=None) -> list:
    n = int(min(SIMON_MAX, max(1, int(_num(length) or 1))))
    feed = list(picks or [])
    out = []
    for i in range(n):
        if i < len(feed):
            out.append(SIMON_PADS[int(feed[i]) % len(SIMON_PADS)])
        else:
            out.append(random.choice(SIMON_PADS))
    return out


def simon_score(answer, target) -> int:
    """How far they got before the first mistake."""
    n = 0
    for got, want in zip(list(answer or []), list(target or [])):
        if got != want:
            break
        n += 1
    return n


def simon_outcome(a_ans, b_ans, target, a="a", b="b") -> dict:
    """Further through the sequence wins. Level is a push; both at zero means
    neither remembered a single pad, and they both pay."""
    sa, sb = simon_score(a_ans, target), simon_score(b_ans, target)
    out = {"scores": {a: sa, b: sb}, "length": len(list(target or [])),
           "loser": "", "both": False, "push": False}
    if sa == 0 and sb == 0:
        out["both"] = True
    elif sa == sb:
        out["push"] = True
    else:
        out["loser"] = b if sa > sb else a
    return out


# ---- tic tac toe: the draws are the point ----------------------------------- #
#
# Sudden death only. Tic tac toe is SOLVED — perfect play always draws — which
# looks fatal for a decider and is actually the mechanism: a draw hits BOTH
# players, so perfect play still walks you into the ceiling. The only way out
# of the shared damage is to try to win, which means leaving perfect play,
# which is how you lose. The game is not the board, it is who cracks first.

TTT_LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8),      # rows
             (0, 3, 6), (1, 4, 7), (2, 5, 8),      # columns
             (0, 4, 8), (2, 4, 6))                 # diagonals


def ttt_new() -> list:
    return [""] * 9


def ttt_winner(board) -> str:
    b = list(board or []) + [""] * 9
    for i, j, k in TTT_LINES:
        if b[i] and b[i] == b[j] == b[k]:
            return b[i]
    return ""


def ttt_full(board) -> bool:
    return all(str(c).strip() for c in (list(board or []) + [""] * 9)[:9])


def ttt_outcome(board, marks: dict) -> dict:
    """`marks` maps player id -> "x"/"o". A draw names nobody and costs both."""
    w = ttt_winner(board)
    out = {"winner": "", "loser": "", "draw": False, "over": False}
    if w:
        for who, mark in (marks or {}).items():
            if mark == w:
                out["winner"] = who
            else:
                out["loser"] = who
        out["over"] = True
    elif ttt_full(board):
        out["draw"] = out["over"] = True
    return out


# ---- the end condition ---------------------------------------------------- #
#
# Always last, never one of the rounds. A match has to be able to END for a
# reason other than "the list ran out", and there are exactly two: somebody
# conceded, or somebody hit the ceiling. Hitting it LOSES.

END_MIN, END_MAX = 1, 999


def end_capacity(spec) -> float:
    """The lose-at capacity, clamped. 0 (or unset) = no capacity trigger."""
    v = _num((spec or {}).get("max_capacity"))
    if v is None or float(v) <= 0:
        return 0.0
    return float(min(END_MAX, max(END_MIN, float(v))))


def end_loser(caps, max_capacity) -> str:
    """Who has lost by reaching the ceiling, or "".

    The HIGHEST meter at or past the line, so a tie-on-the-same-tick still
    names one person rather than none. Dead level names nobody: with both at
    the line there is no honest answer, and picking arbitrarily would decide a
    match on dictionary order.
    """
    top = end_capacity({"max_capacity": max_capacity})
    if top <= 0:
        return ""
    over = {k: float(_num(v) or 0) for k, v in (caps or {}).items()
            if float(_num(v) or 0) >= top}
    if not over:
        return ""
    best = max(over.values())
    who = [k for k, v in over.items() if v == best]
    return who[0] if len(who) == 1 else ""


def rounds_in_order(rounds) -> list:
    """Rounds in the order they RUN — which is the order they are listed in.

    Ordering used to be by capacity band, which stopped making sense the moment
    a round could end on a count instead: "four spins" has no band to sort on.
    A list you can reorder says what it means.

    A `%` round with no target could never finish, so it is dropped; a `count`
    round needs no target at all.
    """
    out = []
    for i, r in enumerate(rounds or []):
        if not isinstance(r, dict):
            continue
        if not (r.get("actions") or r.get("action")):
            continue                       # nothing to run is not a round
        if str(r.get("until") or "") != "count":
            mx = _num(r.get("max"))
            if mx is None or float(mx) <= 0:
                continue
        out.append({**r, "max": float(_num(r.get("max")) or 0), "_i": i})
    return out



def round_count(rnd) -> int:
    """How many passes a `count` round runs. At least one — a round that runs
    zero times is not a round, it is a typo."""
    v = _num((rnd or {}).get("count"))
    return max(1, min(ROUND_CAP, int(v))) if (v is not None and v >= 1) else 1


def round_cleared(rnd, caps, until="leader", passes=0) -> bool:
    """Is this round finished?

    `count`  — after N passes, whatever the meters say. The common case: "four
               spins, then a video". Nothing to do with capacity at all.
    `leader` — the FIRST racer to reach the target ends it.
    `both`   — everyone has to get there, for a round they finish together.
    """
    if not rnd:
        return True
    u = str(until or "leader")
    if u == "count":
        return int(passes) >= round_count(rnd)
    top = float(_num(rnd.get("max")) or 0)
    vals = [float(_num(v) or 0) for v in (caps or {}).values()]
    if not vals:
        return False
    if u == "leader":
        return max(vals) >= top
    return min(vals) >= top


def round_passes(rnd) -> int:
    v = _num((rnd or {}).get("max_passes"))
    n = int(v) if (v is not None and v > 0) else ROUND_CAP
    return max(1, min(ROUND_CAP, n))


