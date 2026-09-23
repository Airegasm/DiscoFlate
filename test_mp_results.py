"""Results have to SHOW — on the overlay, not just in chat.

MultiRoulette and MultiRPS both end by putting percent on somebody's pump. The
number that fires and the number the stream shows are two different code paths
(an action row vs. a composited card), so they can drift apart silently: the
card says +5%, the pump takes +8%, and the only person who can tell is the one
being inflated. These tests pin them to the same source.

The draw is the other half. Rock-paper-scissors ties routinely, and a tie must
show a DRAW card and fire nobody — not an award card with an empty name in it.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_store
import engine as eng

P, F = 0, []


def ok(cond, label):
    global P
    if cond:
        P += 1
    else:
        F.append(label)


def shipped():
    cfg = {"scenes": [], "mp_actions": [], "mp_rounds": [], "templates_removed": []}
    config_store.seed_versus(cfg)
    return cfg


SHIP = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "default_config.json"), encoding="utf-8"))
bot = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "discord_bot.py"), encoding="utf-8").read()
CFG = shipped()
SCENE = CFG["scenes"][0]
ACTS = {a["name"]: a for a in CFG["mp_actions"]}
CARDS = {o["id"]: o for o in SCENE["overlays"]}


def walk(rows):
    for r in rows or []:
        yield r
        for key in ("actions", "else_actions"):
            yield from walk(r.get(key))


# ---- the cards an action calls have to exist ------------------------------- #
# A typo'd overlay id is a QUIET no-op in production ("overlay … not in any
# scene"), so nothing at all appears and no error is raised. Only a test catches
# it.
for nm, a in ACTS.items():
    for r in walk(a["actions"]):
        if r.get("type") in ("overlay", "update_overlay_text"):
            ok(r.get("overlay") in CARDS,
               f"{nm} calls overlay {r.get('overlay')!r}, and the scene has it")

ok({"bvResult", "bvDuel", "bvDraw"} <= set(CARDS), "every result card ships")

# A `notify` row resolves the overlay INSIDE the linked scene. The versus scene
# was born copying the solo scene's notify id, which lives in a scene multi mode
# never loads — so every notify row was a silent no-op with nothing logged.
nid = SCENE.get("notify_overlay")
ok(nid in CARDS, "the versus scene's notify overlay is one of ITS OWN overlays")
ok(CARDS.get(nid, {}).get("layer") == "notify",
   "…and is slotted as the notify layer")
ok(nid not in {"dfdefnotify"}, "…not the solo scene's, which would never resolve")
ok(any(r.get("type") == "notify" for a in ACTS.values() for r in walk(a["actions"])),
   "…and something actually posts to it")
ok(all(CARDS[c]["group"] == "Result" for c in ("bvResult", "bvDuel", "bvDraw")),
   "…and they are grouped together, so one edit restyles all three")
ok(CARDS["bvDuel"]["text"] != CARDS["bvResult"]["text"],
   "the duel gets its OWN card rather than retexting the spin's")

# ---- the scene has a MAIN, and its intro groups are flagged ---------------- #
GROUPS = {}
for o in SCENE["overlays"]:
    GROUPS.setdefault(o.get("group") or "", []).append(o["id"])
gl = SCENE.get("golive") or {}
ok("Main" in GROUPS, "the versus scene ships a Main — the in-match picture")
ok(nid in GROUPS["Main"], "…the notify card lives in Main, so it is up during play")
# Main is the ONE in-match layout. Camera is required of both players, so the
# other player is a video tile and this screen carries only its own gauge.
ok(gl.get("after_group") == "Main",
   "…and Go Live switches to it: one layout, nothing to choose at match time")

# ---- the intro is CARD ART plus the two things art cannot know ------------- #
# The backdrop already says "DiscoFlate VS" and "ONE-ON-ONE PUMP BATTLE". The
# overlays supply only who is playing and when it starts — anything else would
# be re-typing what is already painted on.
ing = {o["id"]: o for o in SCENE["overlays"] if o.get("group") == "Intro"}
ok(set(ing) == {"bvIntroBg", "bvIntroHost", "bvIntroGuest", "bvIntroBegins"},
   "four pieces: the art, both names, and the countdown")
ok(ing["bvIntroBg"]["kind"] == "media", "the backdrop is a media slot")
ok((ing["bvIntroBg"]["x"], ing["bvIntroBg"]["y"], ing["bvIntroBg"]["w"]) == (0.0, 0.0, 1.0),
   "…filling the frame, so no edge of it shows")
ok(ing["bvIntroBg"]["z"] < ing["bvIntroHost"]["z"], "…and BEHIND the names")
ok(ing["bvIntroHost"]["text"] == "[multi_host_name]"
   and ing["bvIntroGuest"]["text"] == "[multi_guest_name]",
   "the names are the SEATS, so left is always the host")
ok(ing["bvIntroHost"]["align"] == "right" and ing["bvIntroGuest"]["align"] == "left",
   "…and they align INWARD, so a long name grows away from the VS rather than "
   "over it")
ok(ing["bvIntroHost"]["x"] + ing["bvIntroHost"]["w"] < ing["bvIntroGuest"]["x"],
   "…and the two blocks cannot overlap")
ok("[intro_timer]" in ing["bvIntroBegins"]["text"], "the countdown counts")
ok(ing["bvIntroBegins"]["y"] > ing["bvIntroHost"]["y"],
   "…and sits below the names, above the art's own strapline")
ok(all(o["mode"] == "hold" for o in ing.values()),
   "every piece HOLDS — an intro that timed its own pieces out would empty the "
   "screen while the countdown was still running")
stg = (SCENE.get("golive") or {}).get("stages") or []
ok(stg and stg[0]["seconds"] == 20, "a 20 second countdown")

intro = [g for g in (SCENE.get("intro_groups") or [])]
ok(intro == ["Intro"], "the intro group is FLAGGED as intro")
ok(all(str(st.get("group")) in intro for st in (gl.get("stages") or [])),
   "…and every Go Live stage names one of them — an unflagged stage group is "
   "offered by nothing and plays during normal gameplay")
ok("Main" not in intro, "Main is not an intro group")

# ---- the scene has its OWN Limits & announcements --------------------------- #
# A scene with no gameplay block falls back to the TOP LEVEL, which is solo's.
# So a versus match announced itself with solo's words, reported one player,
# and offered solo's commands — silently, because a fallback is not an error.
GP = SCENE.get("gameplay") or {}
ok(GP, "the versus scene carries a gameplay block of its own")

live = {**CFG, "chat_scene": SCENE["name"]}
R = config_store.resolved(live)
ok(R.get("commands") == [], "no !commands: a match is driven by its Rounds")
ok(not R.get("owner_commands") and not R.get("chat_buttons"),
   "…and nothing else is typed at it either")
ok(R.get("cooldown_seconds") == 0, "…so there is nothing to cool down")

# Every report covers BOTH players. One number is half a scoreboard in a game
# whose whole subject is the two of you.
for key in ("capacity_message", "pumptimer_message", "pump_message",
            "listener_message_on", "listener_message_off"):
    txt = str(R.get(key) or "")
    ok("[multi_host_" in txt and "[multi_guest_" in txt,
       f"{key} reports BOTH players, not just this install")
auto = R.get("auto_report") or {}
ok(auto.get("enabled") and "[multi_guest_" in str(auto.get("message") or ""),
   "the scoreboard posts on a timer, and it has the guest in it too")
ok("[capacity]" not in str(R.get("capacity_message") or ""),
   "…and none of them quote the bare [capacity], which names only one rig")

# the solo scene is untouched by any of it
solo_live = {**CFG, "scenes": CFG["scenes"], "chat_scene": "nothing-loaded"}
ok(config_store.resolved(solo_live).get("capacity_message")
   != R.get("capacity_message"),
   "a scene that is NOT loaded does not lend its words to anyone else")

# ---- Session Overlays: both of them have to RESOLVE ------------------------ #
# The Scenes tab picks two overlays the session itself drives. A picker that
# names something this scene hasn't got is a quiet no-op — the pause covers
# nothing, and a pause in a MATCH stops both rigs, which is exactly when the
# room most needs telling.
pg = str(SCENE.get("pause_overlay") or "")
ok(pg in GROUPS or pg in CARDS,
   "the pause overlay names a group or overlay this scene actually has")
ok(len(GROUPS.get(pg) or []) >= 2,
   "…with something to cover the picture AND something that says why")

# ---- the whole kit hangs together ------------------------------------------ #
# Four rounds, then overtime, then the ending. Every reference in it resolves.
ACT_NAMES = {a["name"] for a in SHIP["mp_actions"]}
ok([r["name"] for r in SHIP["mp_rounds"]]
   == ["Round 1", "Round 2", "Round 3", "Round 4"], "four rounds ship, in order")

blocks = ([("round " + r["name"], r.get("actions"), r.get("intro")) for r in SHIP["mp_rounds"]]
          + [("sudden death", SHIP["mp_sudden"]["actions"], None),
             ("end condition", SHIP["mp_end"]["actions"], None)]
          + [("action " + a["name"], a["actions"], None) for a in SHIP["mp_actions"]])
for label, body, intro in blocks:
    for r in list(walk(body)) + list(walk(intro)):
        if r.get("type") == "mp_action":
            ok(r.get("action") in ACT_NAMES,
               f"{label} calls Action {r.get('action')!r}, which exists")
        if r.get("type") in ("overlay", "update_overlay_text"):
            ok(r.get("overlay") in CARDS, f"{label} calls overlay {r.get('overlay')!r}")
        if r.get("type") in ("scene_group", "scene_group_kill"):
            ok(r.get("group") in GROUPS, f"{label} plays group {r.get('group')!r}")
        if r.get("type") == "fire":
            ok("[multi_scale]" in str(r.get("fill_pct")),
               f"{label}'s stake is ceiling-relative")

# each round escalates in KIND, not just in number: luck, bluff, judgement,
# memory — then nerve in overtime
kinds = []
for r in SHIP["mp_rounds"]:
    called = [x.get("action") for x in walk(r["actions"]) if x.get("type") == "mp_action"]
    kinds += called
ok(kinds == ["MultiRoulette", "MultiRPS", "MultiBlackjack", "MultiSimon"],
   "the rounds run luck → bluff → judgement → memory, in that order")
sud = [x.get("action") for x in walk(SHIP["mp_sudden"]["actions"])
       if x.get("type") == "mp_action"]
ok(sud == ["MultiTicTacToe"], "…and overtime is nerve")

# the three games that can end with NOBODY beaten must still cost somebody,
# or a round resolves to nothing
for nm, flag in (("MultiBlackjack", "multi_cards_who"),
                 ("MultiSimon", "multi_simon_who"),
                 ("MultiTicTacToe", "multi_ttt_draw")):
    a = next(x for x in SHIP["mp_actions"] if x["name"] == nm)
    txt = json.dumps(a, ensure_ascii=False)
    ok(flag in txt, f"{nm} branches on its shared-loss case")
    ok('"multi_who": "both"' in txt,
       f"…and {nm} really does fire at BOTH when nobody won")

# ---- the spread bet is wired, and settled in the RIGHT ORDER --------------- #
for r in SHIP["mp_rounds"]:
    sb = r.get("spread_bet") or {}
    ok(sb.get("enabled") and sb.get("stake"),
       f"{r['name']} carries a spread bet with a stake on it")

op = bot[bot.index("async def _spread_open"):]
op = op[:op.index("async def _spread_settle")]
ok("spread_now" in op and "_link_say" in op,
   "the CURRENT gap is announced when betting opens — a bet placed without "
   "knowing where you stand is a guess, not a decision")
ok("send_modal" in bot, "the bet is a number box, not a menu of presets")

st = bot[bot.index("async def _spread_settle"):]
st = st[:st.index("async def _run_sudden")]
meas = st.index("spread_now(")
fire = st.index('"type": "fire"')
ok(meas < fire,
   "the spread is MEASURED before anybody is paid — paying first would move "
   "the very number the bet was placed on")
ok("stake / 2.0" in st,
   "the winner pays half: both meters climb so the ceiling stays reachable, "
   "and the gap moves by half a stake rather than a whole one")
ok("multi_scale" in st,
   "…and the payout scales to the ceiling like every other stake")

# ---- Sudden Death: overtime that is a CONTEST, not a countdown ------------- #
SUD = SHIP["mp_sudden"]
ok(SUD.get("actions"), "sudden death ships with something to play")
kinds = [r.get("type") for r in walk(SUD["actions"])]
ok("mp_action" in kinds,
   "…and it is a GAME. 'Both pumps on until someone pops' decides nothing — "
   "whoever is closer to the ceiling gets there first, so the loser is fixed "
   "before overtime even starts")
ok(not [r for r in walk(SUD["actions"]) if r.get("type") == "fire"
        and str(r.get("multi_who") or "") in ("both", "me", "peer")],
   "…and it never just fires at a fixed person")
ok("max_capacity" not in SUD and "count" not in SUD and "until" not in SUD,
   "it has NO clear condition — it loops, and the End Condition is the only "
   "way out")

drv = bot[bot.index("async def _run_sudden"):]
drv = drv[:drv.index("async def _run_round")]
ok("ROUND_CAP" in drv,
   "a pass cap survives anyway: a block that can draw every time (RPS can) "
   "would be a hung match rather than a long one")
ok("s.conceded" in drv and "S_MATCH" in drv,
   "…and it is skipped entirely if the match already resolved")
ok("_run_round(" in drv,
   "it reuses the round runner, so the per-pass ceiling check applies to "
   "overtime too rather than being a second implementation")

# ---- the End Condition can actually run ------------------------------------ #
# the SHIPPED block, not the schema default — content lives in default_config
END = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "default_config.json"), encoding="utf-8"))["mp_end"]
ok(END.get("actions"), "the End Condition ships a block — a match needs an outro")
top = END.get("max_capacity")
ok(1 <= top <= 999, f"…and a lose-at capacity inside 1–999 ({top})")
msg = next(r for r in walk(END["actions"]) if r.get("type") == "message")
ok(msg.get("style") == "embed", "the result posts as an EMBED, not a chat line")
ok(msg.get("title"), "…with a title, so it reads as the end of something")
for token in ("[multi_loser]", "[multi_winner]", "[multi_end_why]",
              "[multi_host_capacity]", "[multi_guest_capacity]"):
    ok(token in msg["message"], f"…and carries {token}")
ok("[capacity]" not in msg["message"],
   "…and never the bare [capacity], which names only one rig")
kinds = [r.get("type") for r in walk(END["actions"])]
ok("stop_devices" in kinds, "it stops both pumps first")
ok("message" in kinds, "…says who won in the venue")
ok("overlay" in kinds, "…and puts it on the stream")
for r in walk(END["actions"]):
    if r.get("type") == "overlay":
        ok(r.get("overlay") in CARDS,
           f"…on a card the scene has ({r.get('overlay')})")
txt = json.dumps(END["actions"], ensure_ascii=False)
ok("[multi_winner]" in txt and "[multi_loser]" in txt,
   "…naming both players from the placeholders the end sets")

# ---- a blank you haven't filled in is SKIPPED, never a failure ------------- #
# Unfinished work is the normal state of a show being built. An empty round, an
# Action you haven't written, a video you haven't made — none of them should
# stop a match, and none should announce your homework on the stream.
import re
i = bot.index('body = rnd.get("actions")')
blk = bot[i:i + 700]
ok("return True" in blk.split("if not body:")[1][:400],
   "an empty ROUND is skipped and the driver carries on to the next")
ok("_link_say" not in blk.split("if not body:")[1][:400],
   "…and says nothing in the venue about it")

# BOTH paths that can call an Action: the mp_action ROW inside a round, and
# mp_run_action driving one directly.
row_site = bot[bot.index("blk = self.mp_action(name)"):][:600]
ok("return True" in row_site,
   "an mp_action ROW for a missing Action is skipped, and the round carries on")
run_site = bot[bot.index("async def mp_run_action"):][:1200]
ok('"ok": True' in run_site and "skipped" in run_site,
   "…and so is running one directly — neither stops a match")

apps = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "app.py"), encoding="utf-8").read()
ok('("media", "audio")' in apps and "has no media file yet" in apps,
   "a media overlay with no file is skipped the way a missing id already is")

# ONE empty media slot ships: the intro backdrop, waiting for the operator's
# own card art. That is exactly what the graceful skip is for — with no file in
# it the intro still plays, names and countdown on black, and the match runs.
blank = [o["id"] for o in SCENE["overlays"]
         if o.get("kind") == "media" and not str(o.get("media") or "").strip()]
ok(blank == ["bvIntroBg"],
   "the only slot shipped empty is the intro backdrop, for your own art")
called = {r.get("overlay") for a in list(ACTS.values()) + [{"actions": END["actions"]}]
          for r in walk(a["actions"]) if r.get("type") == "overlay"}
for r in CFG.get("mp_rounds") or []:
    called |= {x.get("overlay") for x in walk(r.get("actions")) if x.get("type") == "overlay"}
ok(called <= set(CARDS),
   "every overlay any round or action calls still exists in the scene")

# ---- the lose-at capacity is tested EVERY PASS ----------------------------- #
# A round of six duels can carry somebody past it on the second one. Testing
# only between rounds would keep playing until the round happened to finish,
# ignoring the only thing that ends a match.
drv = bot[bot.index("async def _run_round"):]
drv = drv[:drv.index("async def ", 10)]
ok("_mp_end_check" in drv,
   "the pass loop tests the end condition, not just the round boundary")
ok(drv.index("_mp_end_check") < drv.index("round_cleared"),
   "…before the clear-test, so a match that is already over does not run "
   "one more pass first")

# ---- the announced number is the number that fires ------------------------- #
rps = ACTS["MultiRPS"]["actions"]
stake = next(r for r in walk(rps) if r.get("type") == "var")
fire = next(r for r in walk(rps) if r.get("type") == "fire")
ok(stake["variable"] == "duel_pct" and "[var:duel_pct]" in str(fire["fill_pct"]),
   "the RPS fire reads the stake variable instead of repeating a literal")
ok("[var:duel_pct]" in CARDS["bvDuel"]["text"],
   "…and the card reads the SAME variable, so the two cannot drift")
ok(fire.get("multi_who") == "loser", "…and it lands on the loser")

roul = ACTS["MultiRoulette"]["actions"]
rfire = next(r for r in walk(roul) if r.get("type") == "fire")
rnote = next(r for r in walk(roul) if r.get("type") == "notify")
ok("[multi_roll]" in str(rfire["fill_pct"]) and "[multi_roll]" in rnote["message"],
   "roulette's notification and its fire both read the roll")
ok("[multi_roll]" in CARDS["bvResult"]["text"], "…and so does its card")

# ---- a draw fires nobody ---------------------------------------------------- #
br = next(r for r in rps if r.get("type") == "if")
ok(not any(r.get("type") == "fire" for r in walk(br.get("else_actions"))),
   "the draw branch contains no fire at all")
ok(any(r.get("overlay") == "bvDraw" for r in walk(br.get("else_actions"))),
   "…it shows the draw card instead")


# ---- and now actually RUN it ------------------------------------------------ #
def rig():
    e = eng.Engine()
    e.set_config({"devices": [{"id": "d1", "label": "P1", "host": "h",
                               "calibration_seconds_to_100": 60}],
                  "active_device_id": "d1",
                  "scenes": CFG["scenes"], "chat_scene": SCENE["name"],
                  "mode": "multi",
                  "capacity_ranges": [{"min": 0, "max": 999}]})
    shown, notes, fires = [], [], []

    def overlay(spec):
        # mirrors app._bake: the card's own text, rendered with the row's ctx
        item = CARDS.get(spec.get("id")) or {}
        txt = str(spec.get("text") if spec.get("mode") == "update"
                  else item.get("text") or "")
        try:
            txt = e.render(txt, dict(spec.get("ctx") or {}))
        except Exception:  # noqa: BLE001
            pass
        shown.append({"id": spec.get("id"), "text": txt, "mode": spec.get("mode")})
        return {"ok": True}

    async def notify(line, ctx):
        notes.append(e.render(str(line), dict(ctx or {})))
        return {}

    async def router(row, xc):
        t = row.get("type")
        if t == "fire":
            fires.append({"who": row.get("multi_who"),
                          # _num_expr, exactly as the engine's fire row does:
                          # a stake is an EXPRESSION, and rendering it without
                          # evaluating would miss the ceiling scaling entirely
                          "pct": e._num_expr(row.get("fill_pct"), xc)})
            return True
        return bool(t and t.startswith("mp_"))   # the rail claims its own rows

    e.overlay_cb = overlay
    e.notify_cb = notify
    e.mp_row_cb = router
    return e, shown, notes, fires


def run(rows, ctx):
    e, shown, notes, fires = rig()
    asyncio.run(
        e._run_action_block(rows, "MultiRPS", "", "55", "Curtis", extra_ctx=dict(ctx)))
    return shown, notes, fires


# ---- stakes are authored per-100 and scale to the ceiling ------------------ #
# One scene plays the same game at any ceiling. Set 200 and every stake
# doubles; set 80 and they shrink. Same idea as pace compensation: express the
# number relative to the thing that matters, not in units that don't travel.
for nm, a in ACTS.items():
    for r in walk(a["actions"]):
        if r.get("type") == "fire":
            ok("[multi_scale]" in str(r.get("fill_pct")),
               f"{nm}'s stake is ceiling-relative, not a bare number")

ok(config_store.DEFAULTS["mp_end"]["max_capacity"] == 0,
   "the SCHEMA default is still off — a lose-at number is the author's call")
ok(SHIP["mp_end"]["max_capacity"] == 100,
   "…and the shipped kit is authored against 100, which is what makes the "
   "numbers in it readable")


def stake_at(rows, ctx):
    """What a pump is actually ASKED for, through the real engine."""
    e, _shown, _notes, fires = rig()
    asyncio.run(e._run_action_block(rows, "x", "", "55", "C", extra_ctx=dict(ctx)))
    return fires


ROUL = ACTS["MultiRoulette"]["actions"]


scales = {}
for ceiling in (100, 200, 50):
    ctx = {"multi_roll": "6", "multi_scale": f"{ceiling / 100:g}",
           "multi_chosen_name": "Dave", "multi_roll_dice": "6"}
    fires = stake_at(ROUL, ctx)
    scales[ceiling] = float(fires[0]["pct"])
ok(scales[100] == 6, "a 6 rolled at a 100% ceiling asks the pump for 6%")
ok(scales[200] == 12, "…the same roll at 200% asks for 12%")
ok(scales[50] == 3, "…and at 50% asks for 3%")

# a match with no ceiling set must not silently fire ZERO
ctx = {"multi_roll": "6", "multi_scale": "1", "multi_chosen_name": "D",
       "multi_roll_dice": "6"}
ok(float(stake_at(ROUL, ctx)[0]["pct"]) == 6,
   "with no ceiling set the scale is 1, never 0 — an unset dial must not "
   "quietly turn every stake into nothing")

WON = {"multi_duel_loser_name": "Dave", "multi_duel_me": "rock",
       "multi_duel_peer": "scissors", "multi_me_name": "Curtis",
       "multi_peer_name": "Dave", "multi_scale": "1"}
DRAW = {**WON, "multi_duel_loser_name": "", "multi_duel_peer": "rock"}

shown, notes, fires = run(rps, WON)
ok([s["id"] for s in shown] == ["bvDuel"], "a decided duel shows the duel card, alone")
ok("Dave" in shown[0]["text"], "…and the card NAMES the loser")
ok("+5%" in shown[0]["text"],
   "…and the stake variable renders into the card as a real number")
ok(len(fires) == 1 and fires[0] == {"who": "loser", "pct": 5.0},
   "…and the pump is asked for exactly what the card showed")
ok(any("Dave" in n and "5" in n for n in notes), "the notify overlay says it too")

shown, notes, fires = run(rps, DRAW)
ok([s["id"] for s in shown] == ["bvDraw"], "a draw shows the draw card")
ok(not fires, "…and fires NOBODY")
ok("[" not in shown[0]["text"], "…with no unrendered placeholder left showing")
ok(any("Draw" in n and "nobody" in n for n in notes),
   "…and says so on the notify overlay")

# roulette's own run
shown, notes, fires = run(roul, {"multi_chosen_name": "Dave", "multi_roll": "6",
                                 "multi_roll_dice": "6", "multi_scale": "1"})
ok([s["id"] for s in shown] == ["bvResult"], "roulette shows its spin card")
ok("Dave" in shown[0]["text"] and "+6%" in shown[0]["text"],
   "…naming who it landed on and for how much")
ok(len(fires) == 1 and fires[0] == {"who": "chosen", "pct": 6.0},
   "…and the pump takes that same number")
ok(any("Dave" in n and "6" in n for n in notes), "…and the notify overlay agrees")

print(f"{P} passed, {len(F)} failed")
for f in F:
    print("  FAIL:", f)
sys.exit(1 if F else 0)
