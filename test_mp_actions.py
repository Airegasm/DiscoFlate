"""Headless proof of Multiplayer Actions — the reusable block.

A Multiplayer Action is plain action rows. It is never bound to whatever
invoked it: a row says WHO it acts on (`multi_who`) instead of naming a player,
and reads the match out of `[multi_*]` placeholders instead of being handed
one. So the same "roulette round" drops into any multiplayer game.

This proves the two halves separately — the pure pieces (spin, dice, targeting,
placeholders) here, and the routed block end-to-end through two BotManagers.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mp_games as g
import multiplayer as mp

P, F = 0, []


def ok(cond, label):
    global P
    if cond:
        P += 1
    else:
        F.append(label)


ME, PEER = "100", "200"


def sess(cap=0.0, peer_cap=0.0, targets=None, role=mp.ROLE_HOST):
    s = mp.Session(mp.Link(ME, name="Curtis-bot", owner="5001"),
                   peer_name="Dave-bot", net="n", cast="c",
                   calibration=60, player="Curtis")
    s.link.peer = PEER
    s.link.peer_name = "Dave-bot"
    s.link.peer_owner = "5002"
    s.peer_player = "Dave"
    s.link.bind(PEER, "act1", role, state=mp.S_MATCH)
    s.capacity, s.peer_cap = cap, peer_cap
    s.targets = targets or {ME: 150.0, PEER: 75.0}
    return s


# ---- the spin --------------------------------------------------------------- #
ok(g.spin([], None)[0] == "", "a spin with nobody to pick returns nobody")
ok(g.spin([ME], None)[0] == ME, "one racer is always the pick")
a, odds = g.spin([ME, PEER], None, pick=0.0)
ok(a == ME and odds == {ME: 0.5, PEER: 0.5}, "even by default")
ok(g.spin([ME, PEER], None, pick=0.999)[0] == PEER, "…and lands on the other half")
_, w = g.spin([ME, PEER], {ME: 3}, pick=0.0)
ok(abs(w[ME] - 0.75) < 1e-9 and abs(w[PEER] - 0.25) < 1e-9,
   "a weight tilts the wheel, and the odds come back so they can be announced")
ok(g.spin([ME, PEER], {ME: 3}, pick=0.8)[0] == PEER,
   "…a tilted wheel still lands on the long odds sometimes")
ok(g.spin([ME, PEER], {ME: 0, PEER: -5}, pick=0.4)[0] == ME,
   "zero and negative weights fall back to even rather than to a crash")
hits = [g.spin([ME, PEER], None)[0] for _ in range(400)]
ok(0.35 < hits.count(ME) / 400 < 0.65, "an unrigged spin is actually about even")

# ---- the dice --------------------------------------------------------------- #
totals = [g.roll_dice()[0] for _ in range(500)]
ok(min(totals) >= 1 and max(totals) <= 7, "1d7 stays in 1–7")
ok(len(set(totals)) >= 6, "…and actually varies")
ok(g.roll_dice(2, 10, rolls=[7, 3]) == (10, [7, 3]), "NdN sums its faces")
ok(g.roll_dice(1, 7, luck=100)[0] == 7, "+100 luck forces the best roll")
ok(g.roll_dice(1, 7, luck=-100)[0] == 1, "-100 luck forces the worst")
ok(g.roll_dice(1, 7, rolls=[99]) == (7, [7]), "an injected face is clamped to the die")

# ---- targeting -------------------------------------------------------------- #
def who(w, **kw):
    return g.resolve_who(w, me=ME, peer=PEER, **kw)


ok(who("") == [], "a row with no target is nobody's — it runs where it is")
ok(who("me") == [ME] and who("peer") == [PEER], "me / peer are install-relative")
ok(who("both") == [ME, PEER], "both is a normal answer, not two rows")
ok(who("chosen", chosen=PEER) == [PEER], "chosen follows the spin")
ok(who("other", chosen=PEER) == [ME], "…and other is whoever it missed")
ok(who("chosen") == [] and who("other") == [],
   "before a spin there is no chosen, so those rows hit nobody")
ok(who("host", role=mp.ROLE_HOST) == [ME] and who("guest", role=mp.ROLE_HOST) == [PEER],
   "host/guest resolve from this install's role")
ok(who("host", role=mp.ROLE_GUEST) == [PEER] and who("guest", role=mp.ROLE_GUEST) == [ME],
   "…and the other install computes the same two people")
caps = {ME: 80.0, PEER: 20.0}
ok(who("leader", caps=caps) == [ME] and who("trailer", caps=caps) == [PEER],
   "leader / trailer read the meters")
ok(who("leader", caps={ME: 50, PEER: 50}) == [],
   "dead level names nobody rather than picking arbitrarily")
ok(who("nonsense") == [], "an unknown target resolves to nobody, never to a default")

# ---- what one install may ask the other to do ------------------------------- #
C = g.CROSSABLE
for t in ("fire", "roll", "stop_devices", "camera", "mp_action"):
    ok(t in C, f"{t} crosses — it acts on the OTHER pump or its picture")
# Overlays cross because the shipped scene is the same file on both machines,
# so an overlay id means the same thing on either side.
for t in ("overlay", "overlay_kill", "update_overlay_text",
          "scene_group", "scene_group_kill"):
    ok(t in C, f"{t} crosses — the guest holds the same shipped scene")
# Nothing that would make the guest's bot SPEAK or run a game of its own. The
# host narrates for both; a post from the guest is the same news twice.
for t in ("message", "notify", "broadcast", "command", "poll", "competition",
          "minigame", "bonus_round", "award", "end_session", "if", "repeat",
          "goto", "var", "wait"):
    ok(t not in C, f"{t} does NOT cross — the guest neither speaks nor plays")

# ---- the placeholders, all [multi_*] ---------------------------------------- #
s = sess(cap=60.0, peer_cap=30.0)
ph = g.placeholders(s, chosen=PEER)
ok(all(k.startswith("multi_") for k in ph),
   "every multiplayer placeholder carries the multi_ prefix")
ok(ph["multi_me_name"] == "Curtis" and ph["multi_peer_name"] == "Dave",
   "players are named by PLAYER, not by bot")
ok(ph["multi_chosen"] == PEER and ph["multi_chosen_name"] == "Dave", "the spin's pick")
ok(ph["multi_other"] == ME and ph["multi_other_name"] == "Curtis", "and who it missed")
ok(ph["multi_my_pct"] == "60" and ph["multi_peer_pct"] == "30", "both meters")
ok(ph["multi_my_target"] == "150" and ph["multi_peer_target"] == "75",
   "each racer's own compensated line")
ok(ph["multi_leader_name"] == "Curtis" and ph["multi_trailer_name"] == "Dave",
   "leader and trailer by raw capacity")
ok(ph["multi_my_frac"] == "40" and ph["multi_peer_frac"] == "40",
   "…but the FRACTION of your own line is what says who's really winning")
ok(ph["multi_host_name"] == "Curtis" and ph["multi_guest_name"] == "Dave",
   "host/guest names for a block that cares which seat someone is in")

level = g.placeholders(sess(cap=50.0, peer_cap=50.0), None)
ok(level["multi_leader"] == "" and level["multi_leader_name"] == "nobody",
   "dead level has no leader to name")
nospin = g.placeholders(sess(), None)
ok(nospin["multi_chosen"] == "" and nospin["multi_chosen_name"] == "",
   "before a spin, chosen renders empty rather than to somebody")

# the guest computes the same two seats from its own side
gs = sess(role=mp.ROLE_GUEST)
gph = g.placeholders(gs)
ok(gph["multi_host_name"] == "Dave" and gph["multi_guest_name"] == "Curtis",
   "the guest agrees about who is in which seat")

# ---- rounds: how a round ENDS --------------------------------------------- #
CNT = {"name": "Four", "until": "count", "count": 4, "actions": [{"type": "mp_action"}]}
PCT = {"name": "ToTop", "until": "leader", "max": 25, "actions": [{"type": "mp_action"}]}
BOTH = {**PCT, "name": "Together", "until": "both"}

ok([r["name"] for r in g.rounds_in_order([CNT, PCT])] == ["Four", "ToTop"],
   "rounds run in the order they are LISTED — a count round has no band to sort on")
ok(g.rounds_in_order([{"name": "Empty", "until": "count", "count": 2}]) == [],
   "a round with nothing to run is dropped")
ok(g.rounds_in_order([{"name": "NoTarget", "until": "leader",
                       "actions": [{"type": "mp_action"}]}]) == [],
   "…and so is a % round with no target: it could never end")
ok(len(g.rounds_in_order([{**CNT, "max": 0}])) == 1,
   "…but a COUNT round needs no target at all")

ok([g.round_cleared(CNT, {}, "count", passes=i) for i in range(6)]
   == [False, False, False, False, True, True],
   "Count runs exactly N times, whatever the meters say")
ok(g.round_count({"count": 0}) == 1 and g.round_count({}) == 1,
   "…and never zero times: a round that runs zero times is a typo")
ok(g.round_count({"count": 9999}) == g.ROUND_CAP, "…nor more than the backstop")

ok(not g.round_cleared(PCT, {"a": 24, "b": 0}, "leader"), "First to %: not yet")
ok(g.round_cleared(PCT, {"a": 25, "b": 0}, "leader"),
   "…the FIRST racer there ends it")
ok(not g.round_cleared(BOTH, {"a": 25, "b": 0}, "both"),
   "Both Reach %: one is not enough")
ok(g.round_cleared(BOTH, {"a": 30, "b": 25}, "both"), "…it waits for the slower one")
ok(not g.round_cleared(PCT, {}, "leader"),
   "no racers yet is NOT cleared — nothing has happened")
ok(g.round_cleared({}, {"a": 0}), "no round left is trivially cleared")

ok(g.round_passes({}) == g.ROUND_CAP, "a % round with no give-up gets the backstop")
ok(g.round_passes({"max_passes": 5}) == 5, "…and an author's limit is honoured")
ok(g.round_passes({"max_passes": 99999}) == g.ROUND_CAP,
   "…but never above it: a target nobody can reach still has to end")

# ---- the spread bet --------------------------------------------------------- #
# Predict THE GAP at the round's end. Closest without going over.
A, B = "a", "b"
def sp(ba, bb, act): return g.spread_outcome({A: ba, B: bb}, act, A, B)

ok(sp(10, 14, 15)["loser"] == A, "14 is closer to 15 than 10 — the further bet pays")
ok(sp(16, 14, 15)["loser"] == A, "…and a bet OVER the actual is out, however close")
ok(sp(14, 16, 15)["loser"] == B, "…whichever seat goes over")
r = sp(20, 30, 15)
ok(r["both"] and not r["loser"],
   "BOTH over → the round beat them both and they both pay. A round that "
   "resolves to nothing is the outcome worth avoiding")
ok(sp(15, 15, 15)["push"], "the same bet is a push — nobody pays")
ok(sp(15, 10, 15)["loser"] == B, "an EXACT bet wins, it does not count as over")
ok(sp(0, 0, 0)["push"], "a round that ends level, with both saying so, is a push")

# a bet nobody placed is 0 — a real bet, usually a losing one, and never a way
# to stall the round or dodge the wager by walking away
ok(sp(None, 12, 14)["loser"] == A, "no answer bets ZERO and usually loses")
ok(sp(None, None, 0)["push"], "…but two non-answers on a level round still push")
ok(sp(None, 30, 14)["loser"] == B,
   "…and a wild bet still loses to it, because 0 is at least not over")

ok(g.spread_clamp(9999) == 999 and g.spread_clamp(-5) == 0,
   "a bet is clamped to 0-999, so a typo cannot win by being absurd")
ok(g.spread_now({A: 34, B: 22}, A, B) == 12
   and g.spread_now({A: 22, B: 34}, A, B) == 12,
   "the gap announced at the open is absolute — it is a distance, not a lead")

# ---- cards: two seats, one dealer, both hands face up ----------------------- #
ok(g.hand_total([11, 10]) == 21, "an ace plays high when it fits")
ok(g.hand_total([11, 10, 5]) == 16, "…and softens to 1 rather than busting")
ok(g.hand_total([11, 11]) == 12, "two aces cannot both be 11")
ok(g.hand_total([11, 11, 9]) == 21, "…and soften one at a time, only as far as needed")
ok(g.hand_total([10, 10, 10]) == 30, "a bust is reported as its real total, not clamped")
ok(g.card_draw(0.0) == 2 and g.card_draw(0.999) == 11, "the deck is injectable for tests")

# the dealer has no choices — that is the whole point of a dealer
ok(g.dealer_play([6], draws=[5, 7]) == [6, 5, 7], "the dealer hits below 17")
ok(g.dealer_play([10, 7]) == [10, 7], "…and stands on 17, every time")
ok(g.hand_total(g.dealer_play([2], draws=[2, 2, 2, 2, 2, 2, 2, 2, 2, 2])) >= 17,
   "…and always finishes, whatever it is dealt")

# a hand is worth NOTHING if the dealer held it off — that is what makes the
# house a shared threat instead of scenery
ok(g.hand_score([10, 10], [10, 8]) == 20, "a winning hand is worth its total")
ok(g.hand_score([10, 9], [10, 10]) == 0, "a 19 that lost to 20 is worth nothing…")
ok(g.hand_score([10, 10, 5], [10, 6]) == 0, "…and so is a bust")
ok(g.hand_score([10, 8], [10, 8]) == 0,
   "a TIE with the dealer is worth nothing: the house wins pushes at this table, "
   "which is what stops both players simply standing on 17 forever")
ok(g.hand_score([10, 9], [10, 10, 5]) == 19, "a dealer bust pays everyone still in")

H, G = "H", "G"
r = g.cards_outcome([10, 10], [10, 7], [10, 8], H, G)
ok(r["loser"] == G and not r["both"], "the lower surviving hand pays")
r = g.cards_outcome([10, 7], [10, 10], [10, 8], H, G)
ok(r["loser"] == H, "…whichever seat it is in")
r = g.cards_outcome([10, 10], [10, 10], [10, 8], H, G)
ok(r["push"] and not r["loser"], "equal surviving hands push — nobody pays")
r = g.cards_outcome([10, 10, 10], [10, 9], [10, 10], H, G)
ok(r["both"] and not r["loser"],
   "one busts, the other loses to the dealer → the HOUSE cleaned up, and they "
   "both pay. A round where nothing happens is the one outcome worth avoiding")
r = g.cards_outcome([10, 10, 5], [9, 9, 9], [10, 8], H, G)
ok(r["both"], "…both busting is the same answer")
r = g.cards_outcome([10, 9], [10, 8], [10, 10, 4], H, G)
ok(r["loser"] == G and r["dealer"] == 24,
   "when the dealer busts, both survive and it is decided between the players")

# ---- simon: memory that escalates itself ------------------------------------ #
R, Y, GR, BL = g.SIMON_PADS
ok(g.simon_sequence(4, picks=[0, 1, 2, 3]) == [R, Y, GR, BL], "a sequence is injectable")
ok(len(g.simon_sequence(99)) == g.SIMON_MAX, "…and capped: nobody recalls 99 pads")
ok(len(g.simon_sequence(0)) == 1, "…and never empty, which would be a free round")

ok(g.simon_score([R, Y, BL], [R, Y, GR, BL]) == 2,
   "scored as a correct PREFIX — you got two before the mistake")
ok(g.simon_score([R, Y, GR], [R, Y, GR]) == 3, "…all of it when it is all right")
ok(g.simon_score([], [R, Y]) == 0, "…and nothing for not answering")
ok(g.simon_score([R, Y, GR, BL], [R, Y]) == 2,
   "…and never more than the sequence, however many extra pads are mashed")

r = g.simon_outcome([R, Y], [R], [R, Y, GR], A, B)
ok(r["loser"] == B, "further through the sequence wins")
ok(g.simon_outcome([R], [R], [R, Y], A, B)["push"], "level is a push")
r = g.simon_outcome([Y], [GR], [R, Y], A, B)
ok(r["both"] and not r["loser"],
   "neither remembered a single pad → they BOTH pay, same as both busting")
ok(g.simon_outcome([R, Y], [R], [R, Y, GR], A, B)["length"] == 3,
   "…and it reports how long the sequence was, so a stake can scale on it")

# ---- tic tac toe: the draws are the point ----------------------------------- #
# Solved games always draw. That is the mechanism, not the flaw: a draw hits
# BOTH players, so perfect play still walks you into the ceiling, and the only
# escape is to try to win — which means leaving perfect play.
M = {A: "x", B: "o"}
ok(g.ttt_winner(g.ttt_new()) == "", "an empty board has no winner")
ok(g.ttt_winner(["x", "x", "x", "", "o", "o", "", "", ""]) == "x", "a row wins")
ok(g.ttt_winner(["x", "o", "", "x", "o", "", "", "o", ""]) == "o", "…a column wins")
ok(g.ttt_winner(["x", "o", "o", "", "x", "", "", "", "x"]) == "x", "…a diagonal wins")
ok(g.ttt_winner(["", "", "", "", "", "", "", "", ""]) == "", "…and blanks never line up")

r = g.ttt_outcome(["x", "x", "x", "", "o", "o", "", "", ""], M)
ok(r["winner"] == A and r["loser"] == B and r["over"], "a win names both seats")
full = ["x", "o", "x", "x", "o", "o", "o", "x", "x"]
ok(g.ttt_full(full) and g.ttt_outcome(full, M)["draw"],
   "a full board with no line is a DRAW — which is what costs them both")
r = g.ttt_outcome(full, M)
ok(not r["winner"] and not r["loser"],
   "…and a draw names nobody, so a fire aimed at the loser hits nobody and the "
   "block has to pay them both deliberately")
ok(not g.ttt_outcome(["x", "", "", "", "o", "", "", "", ""], M)["over"],
   "a game in progress is not over")

# ---- the end condition ------------------------------------------------------ #
# Always last, never one of the rounds. Three things trigger it — a concession,
# reaching the lose-at capacity, or somebody going off air — and all three are
# the SAME event: somebody lost.
ok(g.end_capacity({"max_capacity": 150}) == 150, "a lose-at capacity is honoured")
ok(g.end_capacity({}) == 0 and g.end_capacity({"max_capacity": 0}) == 0,
   "unset means NO capacity trigger — conceding is then the only way out")
ok(g.end_capacity({"max_capacity": 9999}) == 999,
   "…clamped to 999 above, so a typo can't set a line nobody reaches")
ok(g.end_capacity({"max_capacity": -5}) == 0,
   "…and a negative reads as OFF rather than clamping UP to 1, which would "
   "end every match the instant it started")

ok(g.end_loser({ME: 149, PEER: 10}, 150) == "", "nobody has lost yet")
ok(g.end_loser({ME: 150, PEER: 10}, 150) == ME,
   "reaching the line LOSES — it is a lose condition, not a finish line")
ok(g.end_loser({ME: 151, PEER: 150}, 150) == ME,
   "both past it → the HIGHER meter lost, so a shared tick still names someone")
ok(g.end_loser({ME: 150, PEER: 150}, 150) == "",
   "dead level names nobody: deciding a match on dictionary order is worse "
   "than not deciding it")
ok(g.end_loser({ME: 999, PEER: 0}, 0) == "",
   "with no capacity trigger set, no capacity can lose it")

# the placeholders a block reads once it has fired
s2 = sess(cap=90.0, peer_cap=10.0)
ok(g.placeholders(s2)["multi_loser"] == "" and g.placeholders(s2)["multi_winner"] == "",
   "before anyone loses, both render EMPTY so a block can test them")
s2.conceded, s2.conceded_why = PEER, "reached 150%"
ph2 = g.placeholders(s2)
ok(ph2["multi_loser"] == "Dave" and ph2["multi_winner"] == "Curtis",
   "once it fires they name the players")
ok(ph2["multi_loser_seat"] == "guest", "…and which seat lost")
ok(ph2["multi_end_why"] == "reached 150%", "…and why, in words")
s2.conceded = ME
ok(g.placeholders(s2)["multi_loser_seat"] == "host",
   "…the host losing says host — it reads the seat, not a fixed answer")

print(f"{P} passed, {len(F)} failed")
for f in F:
    print("  FAIL:", f)
sys.exit(1 if F else 0)
