"""The versus starter kit, and the compositor support it needs.

A scene is only as good as the pieces it points at. Every check here is a way
DiscoFlate Versus could arrive looking fine and render wrong: a gauge silently
showing your own capacity under your opponent's name, a production layout the
config names but the scene hasn't got, a band pointing at an Action that never
seeds — or a group called "Intro" resolving to the wrong half of the app.

The kit SEEDS once and is then yours to edit, so these read it from a fresh
config the way a real install would.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import camera
import config_store

P, F = 0, []


def ok(cond, label):
    global P
    if cond:
        P += 1
    else:
        F.append(label)


# The versus kit is SEEDED, not generated-always, so read it the way a real
# install would: from a fresh config that has just been handed the starter.
_fresh = {"scenes": [], "mp_actions": [], "mp_rounds": [], "templates_removed": []}
config_store.seed_versus(_fresh)
cfg = _fresh                      # what an install holds after first load
SC = {s["name"]: s for s in _fresh["scenes"]}
BV = SC.get(config_store.VERSUS_SCENE_NAME, {})
groups = set(BV.get("groups") or [])
ovs = BV.get("overlays") or []
by_group = {}
for o in ovs:
    by_group.setdefault(o.get("group"), []).append(o)

# ---- it ships, and it ships as a MULTIPLAYER scene ------------------------- #
ok(BV, f"{config_store.VERSUS_SCENE_NAME} is handed over on a fresh install")
ok(BV.get("mode") == "multi", "…tagged multi, so the solo picker never lists it")
ok(not BV.get("builtin"),
   "…and EDITABLE — a starter you take apart, not a fixture. It seeds once by "
   "name; delete it and it stays deleted.")
ok(not BV.get("multi_game_mode"),
   "…and names no separate 'game mode' — the scene's Rounds ARE the game")
ok(BV.get("input") in ("operators", "audience"),
   "…it says whose hands are on it: Versus or Gameshow")

# ---- ONE production layout, because both players are on camera ------------ #
# Camera is REQUIRED of both players now. That is what removed the second
# layout: the other player is a video tile, so this screen only ever carries
# its own gauge and its own pump timer. Their meter still crosses the wire — it
# drives the game's maths, not a widget.
vid = config_store.DEFAULTS["multiplayer"]["video"]
ok(vid.get("required") is True, "a match requires both players on camera")
ok("guest_cam_group" not in vid and "no_guest_cam_group" not in vid,
   "…so there is no camera-answer layout branch left to get wrong")

main = by_group.get("Main") or []
mg = [o for o in main if o["kind"] == "capacity_gauge"]
ok((BV.get("golive") or {}).get("after_group") == "Main",
   "the in-match layout is fixed — one layout, so nothing has to choose")
ok(len(mg) == 1, "it carries exactly ONE gauge")
ok(mg[0].get("source", "me") == "me",
   "…this install's own: the other player is on camera, not on a gauge")
ok(not [o for o in BV["overlays"] if o.get("source") == "peer"],
   "…and the scene draws NO remote widget at all")
ok(str(mg[0].get("orient", "")).startswith("v"), "…vertical, not horizontal")
ok(mg[0]["x"] < 0.05, "…hugging the edge")
ok(0.6 < mg[0]["w"] < 0.95,
   "…running most of the screen height (a vertical gauge's `w` is its LENGTH)")
ok(abs((mg[0]["y"] + mg[0]["w"] / 2) - 0.5) < 0.02, "…and centred vertically")
ok(any(o["kind"] == "pump_timer" for o in main),
   "…beside its own pump timer, which this install updates itself")
lbls = " ".join(o.get("text", "") for o in main if o["kind"] == "text")
# NOT the player's name. Your own camera tile already says who you are —
# Discord labels it and the face is yours — so a name here is the one thing on
# screen that tells a viewer nothing they don't have. The number is what
# Discord cannot show, so that is what the label carries.
ok("[multi_me_name]" not in lbls, "your own tile does not re-label you")
ok("[multi_my_pct]" in lbls and "[multi_my_target]" in lbls,
   "…it carries your meter against your own line, which Discord cannot show")

# ---- the pre-shows --------------------------------------------------------- #
stages = (BV.get("golive") or {}).get("stages") or []
# One stage for now. The second was a video slot with no file in it; a
# pre-show that plays nothing is worse than no pre-show, so it went rather
# than sitting there waiting to be filled.
ok(len(stages) >= 1, "Go Live opens with an intro")
ok(all(st["group"] in groups for st in stages),
   "…and every stage names a group the scene has")
ok((BV.get("golive") or {}).get("after_group") == "Main",
   "a FIXED after-group: with camera required of both players there is only "
   "one layout, so nothing is left to choose at match time")
rtext = " ".join(o.get("text", "") for o in by_group.get("Round Intro", []))
ok("[multi_round_name]" in rtext and "[multi_round_target]" in rtext,
   "the round intro says which round and what clears it")

ok(len({o["id"] for o in ovs}) == len(ovs), "overlay ids are unique")
ok(all((o.get("label") or "").strip() for o in ovs),
   "every overlay is NAMED — without one the list and the action dropdown fall "
   "back to the kind, so four videos all read as 'Media'")
ok(len({o["label"] for o in ovs}) == len(ovs),
   "…and no two share a name, or the dropdown is still a guess")
ok(all(0 <= o["x"] <= 1 and 0 <= o["y"] <= 1 for o in ovs),
   "every overlay is on screen (coords are 0–1 fractions)")

# a vertical gauge is a real thing in the compositor, not a rotated horizontal
# one — it fills UPWARD, which a rotation would only manage by accident
import camera as _cam
_c = _cam.VirtualCam.__new__(_cam.VirtualCam)
_c._bgr = lambda col, dflt=None: dflt or (80, 80, 80)
_c._ramp = lambda f: (0, 200, 0)
spr = _c._gauge_sprite(mg[0], 1920, 1080, 55.0)
hh, ww = spr.shape[:2]
ok(hh > ww * 4, "it renders as a tall bar, not a wide one")
ok(tuple(spr[hh - 3, ww // 2, :3]) != tuple(spr[2, ww // 2, :3]),
   "…and fills from the bottom up")
flat = dict(mg[0]); flat["orient"] = "h"
sp2 = _c._gauge_sprite(flat, 1920, 1080, 55.0)
ok(sp2.shape[1] > sp2.shape[0],
   "…while the same overlay without orient:v is wide — the field is what does it")

# ---- the compositor actually honours `source` ------------------------------ #
# The shipped versus scene no longer draws a peer gauge — both players are on
# camera. The compositor keeps the capability: it is how a scene COULD show the
# other meter, and removing it would break anyone who has built one.
cam = camera.VirtualCam.__new__(camera.VirtualCam)
cam._get_state = lambda: {"capacity": 80.0, "peer_capacity": 25.0}
seen = []
cam._gauge_sprite = lambda item, W, H, pct: seen.append(pct)
cam._solid_sprite = lambda *a: None
cam._text_sprite = lambda *a, **k: None

cam._render_item({"kind": "capacity_gauge"}, 1920, 1080)
ok(seen[-1] == 80.0, "a gauge with no source shows THIS install — the default "
                     "can't change under existing scenes")
cam._render_item({"kind": "capacity_gauge", "source": "me"}, 1920, 1080)
ok(seen[-1] == 80.0, "source 'me' is explicit and the same")
cam._render_item({"kind": "capacity_gauge", "source": "peer"}, 1920, 1080)
ok(seen[-1] == 25.0, "source 'peer' reads the OTHER install's meter")
cam._render_item({"kind": "capacity_gauge", "source": "PEER"}, 1920, 1080)
ok(seen[-1] == 25.0, "…case-insensitively")

cam._get_state = lambda: {"capacity": 80.0}          # no match running
cam._render_item({"kind": "capacity_gauge", "source": "peer"}, 1920, 1080)
ok(seen[-1] == 0.0,
   "outside a match a peer gauge reads ZERO — showing your own capacity under "
   "your opponent's name would be worse than showing nothing")

# ---- the loaded scene swaps with the mode ---------------------------------- #
# The live scene is what resolved() lays over the config, so a solo scene left
# loaded in multiplayer means solo's rules and none of the multiplayer overlays.
SCENES = [{"name": "AireGasm"},                       # untagged = solo
          {"name": "DiscoFlate Default"},
          {"name": config_store.VERSUS_SCENE_NAME, "mode": "multi"},
          {"name": "Roulette Night", "mode": "multi"}]


def pick(mode, remembered=None, scenes=None):
    return config_store.scene_for_mode(
        {"scenes": scenes if scenes is not None else SCENES,
         "scene_by_mode": remembered or {}}, mode)


ok(pick("multi") == config_store.VERSUS_SCENE_NAME,
   "with nothing remembered, multiplayer loads the shipped versus scene")
ok(pick("solo") == "DiscoFlate Default",
   "…and solo loads the shipped default")
ok(pick("multi", {"multi": "Roulette Night"}) == "Roulette Night",
   "what a mode had loaded is what it gets back")
ok(pick("solo", {"solo": "AireGasm"}) == "AireGasm", "…both ways")
ok(pick("multi", {"multi": "AireGasm"}) == config_store.VERSUS_SCENE_NAME,
   "a remembered scene belonging to the OTHER mode is ignored — that is the "
   "whole bug this exists to prevent")
ok(pick("solo", {"solo": "Deleted Scene"}) == "DiscoFlate Default",
   "a remembered scene that no longer exists falls back, it doesn't blank")
ok(pick("multi", {}, [{"name": "Only One", "mode": "multi"}]) == "Only One",
   "with no shipped default present, that mode's first scene is loaded")
ok(pick("multi", {}, [{"name": "Solo Only"}]) == "",
   "and a mode with NO scenes at all loads nothing rather than the wrong one")
ok(config_store.scene_mode({"name": "x"}) == "solo",
   "an untagged scene is solo — nothing written before multiplayer disappears")

# ---- the versus starter kit: handed over once, then yours ------------------ #
# All three together — scene, Actions, Rounds. A scene you can edit whose
# Actions you cannot would be the worst of both.
kit = {"scenes": [], "mp_actions": [], "mp_rounds": [], "templates_removed": []}
ok(config_store.seed_versus(kit) > 0, "the versus kit seeds on a fresh config")
ok([sc["name"] for sc in kit["scenes"]] == [config_store.VERSUS_SCENE_NAME],
   "…the scene")
ok({a["name"] for a in kit["mp_actions"]} == {"MultiRoulette", "MultiRPS"},
   "…the Actions it runs")
ok(len(kit["mp_rounds"]) >= 2, "…and the Rounds that sequence them")
ok(not kit["scenes"][0].get("builtin")
   and not any(a.get("builtin") for a in kit["mp_actions"])
   and not any(r.get("builtin") for r in kit["mp_rounds"]),
   "every piece is EDITABLE — none of it is flagged read-only")

ok(config_store.seed_versus(kit) == 0, "seeding twice never duplicates any of it")
kit["mp_actions"][0]["name"] = "My Roulette"
ok(config_store.seed_versus(kit) == 0,
   "…and renaming one does not make a second appear, because the id is what "
   "is tracked, not the name")

rm = {"scenes": [], "mp_actions": [], "mp_rounds": [],
      "templates_removed": [config_store.VERSUS_SEED_ID, "mp_multiroulette"]}
config_store.seed_versus(rm)
ok(not rm["scenes"], "a deleted scene stays deleted")
ok([a["name"] for a in rm["mp_actions"]] == ["MultiRPS"],
   "…and so does a deleted Action, while the rest still arrives")

ok(config_store.VERSUS_SCENE_NAME not in config_store.SHIPPED_SCENE_NAMES,
   "the versus scene is NOT in the generated-always list — that is what makes "
   "it editable at all")

MR = [a for a in cfg["mp_actions"] if a["name"] == "MultiRoulette"][0]
rows = MR["actions"]
kinds = [r["type"] for r in rows]
ok(kinds == ["mp_spin", "mp_roll", "overlay", "notify", "fire", "wait"],
   "it is the sequence: spin, roll, show the card, post the line, inflate, wait")
spin = rows[kinds.index("mp_spin")]
roll = rows[kinds.index("mp_roll")]
fire = rows[kinds.index("fire")]
ov = rows[kinds.index("overlay")]
ok(roll["dice"] == 1 and roll["sides"] == 8, "the dice are 1d8")
# Stakes are authored against a 100% match and scaled to the ceiling, so the
# same scene plays the same game whatever the lose-at number is set to.
ok("[multi_roll]" in str(fire["fill_pct"]), "the fire spends what the roll produced")
ok("[multi_scale]" in str(fire["fill_pct"]),
   "…scaled to the match's ceiling, not a bare number that only suits one")
ok(fire["fire_mode"] == "add",
   "…as a PERCENT — seconds are a different amount of inflation on every rig, "
   "and are refused over the wire anyway")
ok(fire["multi_who"] == "chosen", "…on whoever the wheel picked")
ok(fire.get("block_during") is False,
   "it does NOT block: the wheel spins again on its own clock and the numbers "
   "stack on what is already queued")
ok("[multi_chosen_name]" in spin.get("message", ""), "the spin names who it picked")
ok(rows[-1]["type"] == "wait" and rows[-1]["seconds"] > 0,
   "…and it ends with the gap between spins")

ok(ov["overlay"] in {o["id"] for o in ovs},
   "the result card names an overlay the shipped scene actually has")
card = [o for o in ovs if o["id"] == ov["overlay"]][0]
ok("[multi_chosen_name]" in card["text"] and "[multi_roll]" in card["text"],
   "…and that card shows who and how much")
ok(card.get("group") in groups, "…in a group the scene knows about")

# ---- the shipped MultiRPS --------------------------------------------------- #
RPS = [a for a in cfg["mp_actions"] if a["name"] == "MultiRPS"]
ok(len(RPS) == 1, "MultiRPS ships too")
RPS = RPS[0]
ok(not RPS.get("builtin"), "…editable, like the scene that uses it")
rk = [r["type"] for r in RPS["actions"]]
ok(rk == ["var", "mp_duel", "message", "if", "wait"],
   "it is: set the stake, both pick, reveal, then branch on whether anyone LOST")


def rps_walk(rows):
    """The fire lives inside the `if` now, so nothing here may index by row
    position — a draw and a decided duel run different branches."""
    for r in rows or []:
        yield r
        for k in ("actions", "else_actions"):
            yield from rps_walk(r.get(k))


duel = RPS["actions"][rk.index("mp_duel")]
moves = {o["value"]: set(o["beats"]) for o in duel["options"]}
ok(set(moves) == {"rock", "paper", "scissors"}, "the three moves")
ok(moves["rock"] == {"scissors"} and moves["paper"] == {"rock"}
   and moves["scissors"] == {"paper"}, "…and the cycle is right")
ok(duel["seconds"] > 0, "a duel always has a deadline")

hit = next(r for r in rps_walk(RPS["actions"]) if r["type"] == "fire")
ok(hit["multi_who"] == "loser", "the pump hits the LOSER")
ok(hit["fire_mode"] == "add",
   "…as a percent: seconds are a different amount of inflation per rig, and "
   "are refused over the wire")
ok("[var:duel_pct]" in str(hit["fill_pct"]),
   "…for whatever the stake variable says, so the card and the pump can never "
   "disagree about the number")
ok("[multi_scale]" in str(hit["fill_pct"]), "…and it scales to the ceiling too")
ok(hit.get("block_during") is False,
   "…and doesn't block, so the next round starts on its own clock")

reveal = RPS["actions"][rk.index("message")]["message"]
branch = RPS["actions"][rk.index("if")]
ok(not any(r["type"] == "fire" for r in rps_walk(branch.get("else_actions"))),
   "a DRAW fires nobody — the stake is only paid by someone who lost")
ok("[multi_duel_me]" in reveal and "[multi_duel_peer]" in reveal,
   "the reveal shows both moves")
ok("[if multi_duel_loser_name]" in reveal and "[if not multi_duel_loser_name]" in reveal,
   "…and reads correctly on a draw, using the action system's own conditional "
   "lines rather than a field invented for it")

# the whole point of a generic duel row: RPS is DATA
import mp_games as g
ok(g.duel_winner("rock", "scissors", duel["options"]) == "me", "rock beats scissors")
ok(g.duel_winner("rock", "paper", duel["options"]) == "peer", "paper beats rock")
ok(g.duel_winner("rock", "rock", duel["options"]) == "", "same move draws")
ok(g.duel_winner("rock", "", duel["options"]) == "me",
   "a walkover counts — a round must not stall on someone who walked away")
ok(g.duel_winner("", "", duel["options"]) == "",
   "…but nobody playing is a draw, not a win for nobody")
ok(g.resolve_who("loser", me="1", peer="2", loser="") == [],
   "a drawn round aims the hit at NOBODY rather than picking arbitrarily")

# ---- the versus game has rounds, and they hang together -------------------- #
ship = [r for r in cfg["mp_rounds"] if str(r.get("_tpl_id") or "").startswith("mp_vs_")]
ok(len(ship) >= 2, "the versus game comes with its bands")
ok(all(r.get("action") or r.get("actions") for r in ship),
   "every band has something to run — a loop, or a body")


def actions_named(rows):
    """Every Action name a block reaches, however deeply it nests."""
    out = set()
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if r.get("type") == "mp_action" and r.get("action"):
            out.add(r["action"])
        for key in ("actions", "else_actions", "win_actions", "miss_actions",
                    "post_actions", "intro"):
            out |= actions_named(r.get(key))
        for op in (r.get("options") or []):
            if isinstance(op, dict):
                out |= actions_named(op.get("actions"))
    return out


acts = {a["name"] for a in cfg["mp_actions"]}
named = {r["action"] for r in ship if r.get("action")}
for r in ship:
    named |= actions_named(r.get("actions"))
ok(named and named <= acts,
   "every Action a band reaches is one that seeds with it — a band pointing at "
   "something that never arrives is a game that cannot run")

ok(all(r.get("until") in ("count", "leader", "both") for r in ship),
   "every band says how it ENDS: a count, or a target somebody has to reach")
ok(all(r["count"] >= 1 for r in ship if r["until"] == "count"),
   "…and a count round runs at least once")
ok(all(r.get("max", 0) > 0 for r in ship if r["until"] != "count"),
   "…while a % round has a target it could actually reach")
ok(all(r.get("intro") for r in ship), "each round opens with its own card")
introgroups = {row.get("group") for r in ship for row in r["intro"]
               if row.get("type", "").startswith("scene_group")}
ok(introgroups and introgroups <= groups,
   "…playing a group the scene actually has")

r1 = [r for r in ship if r["name"] == "Round 1"][0]
seq = [b["type"] for b in r1["actions"]]
# It was four spins, a video, four more, a video. With the videos gone the two
# halves had nothing between them, so they are one loop of eight — two
# back-to-back repeats of four is just a confusing way to write that.
ok(seq == ["repeat"], "Round 1 is one loop")
ok(int(float(r1["actions"][0]["iterations"])) == 8, "…of eight spins")
ok(r1["until"] == "count" and r1["count"] == 1,
   "…run once, because the loop already says eight")
r2 = [r for r in ship if r["name"] == "Round 2"][0]
ok(r2["until"] == "count" and r2["count"] == 6,
   "Round 2 is one match, run six times — the round itself is the loop")
ok([b["type"] for b in r2["actions"]] == ["mp_action"], "…and it is just the call")

# ---- a group name in one scene never reaches another ----------------------- #
# Group names are scoped to the LOADED scene, which is right: two scenes may
# both call a group "Intro" and mean different looks. The hole was the fallback
# when nothing is loaded — it searched every scene, so a solo "Intro" could
# play during a multiplayer match.
mixed = {"mode": "multi", "scenes": [
    {"name": "Solo A", "groups": ["Intro", "Main"]},
    {"name": "Solo B", "groups": ["Intro", "Main"]},
    {"name": "Versus", "mode": "multi", "groups": ["Intro", "Round Intro"]},
]}
ok([sc["name"] for sc in config_store.scenes_in_mode(mixed)] == ["Versus"],
   "in multiplayer, only multiplayer scenes are searched")
mixed["mode"] = "solo"
ok([sc["name"] for sc in config_store.scenes_in_mode(mixed)] == ["Solo A", "Solo B"],
   "…and in solo, only solo ones — an 'Intro' never crosses the divide")
ok([sc["name"] for sc in config_store.scenes_in_mode(mixed, "multi")] == ["Versus"],
   "…and the mode can be asked for explicitly")
ok(config_store.scenes_in_mode({"scenes": [{"name": "X"}]}),
   "no mode set behaves as solo, so nothing written before this disappears")

# two scenes in the SAME mode may absolutely share a name — that is scoped,
# not a clash, and the resolver takes the LOADED one
ok(len([sc for sc in config_store.scenes_in_mode(mixed) if "Intro" in sc["groups"]]) == 2,
   "same-mode scenes sharing a group name is legal; the loaded scene decides")

print(f"{P} passed, {len(F)} failed")
for f in F:
    print("  FAIL:", f)
sys.exit(1 if F else 0)
