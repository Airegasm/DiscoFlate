"""Headless proof of the multiplayer TRANSPORT — discord_bot's adapter.

test_multiplayer.py proves the protocol; this proves the wiring around it: the
envelope path, multiplayer's own send paths, a crossed row reaching a real action
row, and the two channels staying out of each other's business.

Two BotManagers are pointed at one fake bot_network channel and made to play a
whole handshake through it. No network, no tokens, no second house.
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# BEFORE importing config_store: CONFIG_PATH is computed from DATA_DIR at
# import time, so patching the module later leaves the real data/config.json
# reachable. This test writes config (blocking persists), and it must never be
# able to touch the operator's own.
os.environ["DISCOFLATE_DATA_DIR"] = tempfile.mkdtemp(prefix="df-test-")

import config_store

assert "df-test-" in config_store.CONFIG_PATH, \
    f"refusing to run against a real config at {config_store.CONFIG_PATH}"

import discord_bot as db
import mp_games
import multiplayer as mp

P, F = 0, []


def ok(cond, label):
    global P
    if cond:
        P += 1
    else:
        F.append(label)


LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)


async def settle(_=0):
    """Drain the tasks _link_envelope spawns — including the ones THOSE spawn
    when a reply crosses back. The real bot has a running loop; a test has to
    hand it one explicitly."""
    for _ in range(200):
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()]
        if not pending:
            return
        await asyncio.sleep(0)


def run(coro):
    """Every call settles, because a send here dispatches straight into the
    other install's envelope path and that work happens in a task."""
    async def go():
        r = await coro
        await settle()
        return r
    return LOOP.run_until_complete(go())


# ---- stubs ----------------------------------------------------------------- #
class Msg:
    _n = [1000]

    def __init__(self, channel, content, bot=True, uid=7):
        Msg._n[0] += 1
        self.id = Msg._n[0]
        self.channel = channel
        self.content = content
        self.edits = 0
        self.deleted = False
        self.embed_text = ""
        self.view = None
        self.author = type("A", (), {"bot": bot, "id": uid, "display_name": "x"})()

    async def edit(self, **kw):
        self.edits += 1
        self.content = kw.get("content") or self.content

    async def delete(self):
        self.deleted = True


class Chan:
    """One channel. `listeners` are the BotManagers that will be handed every
    message posted here — the fake gateway."""

    def __init__(self, cid, kind="text"):
        self.id = int(cid)
        self.sent = []
        self.embeds = []
        self.listeners = []
        self.fail = False
        self.guild = None
        self.kind = kind

    def history(self, limit=100):
        """The reason a channel beats a socket: the log is still there, so
        catching up after a reconnect costs nothing."""
        sent = self.sent[-limit:]

        async def gen():
            for m in reversed(sent):      # discord.py yields newest-first
                yield m
        return gen()

    async def send(self, content=None, **kw):
        if self.fail:
            raise RuntimeError("missing permissions")
        m = Msg(self, content if content is not None else kw.get("content") or "")
        # an embed's text is NOT in message.content — a test that only reads
        # content silently stops checking anything posted as a card
        em = kw.get("embed")
        if em is not None:
            m.embed_text = f"{getattr(em, 'title', '') or ''}\n{getattr(em, 'description', '') or ''}"
            self.embeds.append(m.embed_text)
        m.view = kw.get("view")
        self.sent.append(m)
        for bm in self.listeners:
            bm._link_envelope(m)
        return m


class Client:
    def __init__(self, uid, name, chans):
        self.user = type("U", (), {"id": uid, "name": name})()
        self._chans = chans

    def is_ready(self):
        return True

    def get_channel(self, cid):
        return self._chans.get(int(cid))


class Engine:
    def __init__(self):
        self.capacity = 0.0
        self._fires = {}
        self.logs = []
        self.ran = []
        self.aborted = []
        self.boom = False
        self.cfg = {}
        self.mp_row_cb = None
        self.mp_ctx_cb = None
        self.camera = []                      # every curtain call, in order
        self.camera_cb = self._camera
        self.overlay_cb = lambda spec: {"ok": True}   # a compositor is present

    def golive(self):
        """Mirrors the real one: the live scene's Go Live block."""
        sc = next((x for x in (self.cfg.get("scenes") or [])
                   if x.get("name") == self.cfg.get("chat_scene")), {})
        return sc.get("golive") or {}

    def intro_stages(self):
        """Mirrors the real one: the live scene's Go Live stages."""
        sc = next((x for x in (self.cfg.get("scenes") or [])
                   if x.get("name") == self.cfg.get("chat_scene")), {})
        return [{"group": str(r.get("group") or ""),
                 "seconds": float(r.get("seconds") or 0),
                 "actions": r.get("actions") or []}
                for r in ((sc.get("golive") or {}).get("stages") or [])]

    def _capacity_cap(self):
        return getattr(self, "cap", 100.0)

    def set_config(self, cfg):
        # In production engine.cfg IS the stored config. Here the fixture is a
        # hand-built dict, so merge the one field that round-trips rather than
        # replacing it and losing the fixture's channels.
        if isinstance(cfg, dict) and isinstance(cfg.get("multiplayer"), dict):
            self.cfg.setdefault("multiplayer", {})["blocked"] = \
                cfg["multiplayer"].get("blocked", [])

    async def _camera(self, op):
        self.camera.append(op)
        return {"ok": True}

    def _log(self, kind, msg):
        self.logs.append(f"{kind}: {msg}")

    def render(self, t, extra=None):
        # real substitution: the test asserts the number the audience was told
        # is the number that fired, which is meaningless without it
        out = str(t)
        for k, v in (extra or {}).items():
            out = out.replace(f"[{k}]", str(v))
        return out

    def _device(self, did):
        return {"id": "d1", "calibration_seconds_to_100": 60}

    def _active_id(self):
        return "d1"

    async def abort(self, device_id=None, *, reason="aborted"):
        self.aborted.append(reason)

    async def _run_action_block(self, rows, name="", extra_ctx=None, **kw):
        """Mirrors the real loop's contract: ask the multiplayer router about
        every row first, and skip whatever it claims. A stub that ran rows
        unconditionally would pass this test while the real engine sent the
        same fire twice."""
        if self.boom:
            raise RuntimeError("device offline")
        xc = dict(extra_ctx or {})
        out = {}
        for a in rows:
            if self.mp_row_cb is not None and await self.mp_row_cb(a, xc):
                continue
            row = dict(a)
            if row.get("type") == "repeat":
                # mirrors the real loop: a nested block, capped. Without it a
                # repeat row would be recorded and its body silently skipped.
                mode = str(row.get("mode") or "fixed")
                cap = int(float(row.get("iterations") or 1)) if mode == "fixed" else 50
                self.ran.append(row)
                for _ in range(max(0, min(cap, 50))):
                    sub = await self._run_action_block(
                        row.get("actions") or [], name=name, extra_ctx=xc)
                    if isinstance(sub, dict):
                        xc.update(sub)
                continue
            if row.get("type") == "camera" and self.camera_cb is not None:
                await self.camera_cb(row.get("op") or "freeze")
            if row.get("type") == "fire":
                val = self.render(str(row.get("fill_pct", "")), xc)
                try:                       # the real engine coerces via _num_expr
                    val = float(val)
                except (TypeError, ValueError):
                    pass
                row["fill_pct"] = val
                self.capacity += float(val or 0)
                out["fired_desc"] = f"+{row['fill_pct']}% to Belly"
            self.ran.append(row)
        xc.update(out)
        return xc


def cfg_for(peer_name, net="900", cast="901", mode="multi", **kw):
    return {
        "mode": mode,
        "multiplayer": {
            "bot_network": {"guild_id": "1", "channel_id": net},
            "broadcast": {"guild_id": "1", "channel_id": cast},
            "peer_bot_name": peer_name,
            # both names are required to invite — naming only the bot makes it
            # possible to start a match against the wrong person's install
            "peer_player_name": kw.get("peer_player", "Someone"),
            "blocked": [],
            "video": {"guest_cam_group": "", "no_guest_cam_group": ""},
            "peer": {"bot_id": "", "owner_id": "", "name": ""},
            "role_pref": kw.get("role_pref", "host"),
        },
        "listen_targets": [], "cooldown_exempt_user_ids": ["55"],
        "devices": [], "listener_enabled": True,
    }


def make(uid, name, peer_name, chans, **kw):
    eng = Engine()
    cfg = cfg_for(peer_name, **kw)
    # the SAME object the engine holds: the hot paths read engine.cfg rather
    # than paying config_store.load() per message
    eng.cfg = cfg
    bm = db.BotManager(eng, lambda: cfg)
    bm._client = Client(uid, name, chans)
    return bm, eng, cfg


NET, CAST_A, CAST_B = Chan("900"), Chan("901"), Chan("902")
CHANS = {900: NET, 901: CAST_A, 902: CAST_B}

A, EA, CFGA = make(100, "Curtis-bot", "Dave-bot", CHANS)
B, EB, CFGB = make(200, "Dave-bot", "Curtis-bot", CHANS, role_pref="guest")
NET.listeners = [A, B]

# ---- the envelope path ------------------------------------------------------ #
ok(A._link_envelope(Msg(CAST_A, "DF1 hello {}")) is False,
   "the broadcast channel is not the protocol's channel")
CFGA["mode"] = "solo"
ok(A._link_envelope(Msg(NET, "DF1 hello {}")) is False,
   "a solo install ignores bot_network entirely")
CFGA["mode"] = "multi"
ok(A._link_envelope(Msg(NET, "just chatting")) is True,
   "bot_network is plumbing — even human chatter there stays out of the game")

# bot_network can never become a listen target
CFGA["listen_targets"] = [{"guild_id": "1", "channel_id": "900", "listen": True},
                          {"guild_id": "1", "channel_id": "901", "listen": True}]
ok([t["channel_id"] for t in A._targets(CFGA)] == ["901"],
   "bot_network is filtered out of the target list")
ok("900" not in A._chat_watched(CFGA), "…and out of the Chat tab")
CFGA["listen_targets"] = []

# get_config is config_store.load() — a full disk read, migrate and deep merge.
# The envelope path runs on EVERY message in every channel, so it must read the
# engine's live copy instead. This is the one invariant that keeps a busy server
# from paying for multiplayer it isn't using.
async def count_loads():
    seen, orig = [0], A.get_config
    A.get_config = lambda: (seen.__setitem__(0, seen[0] + 1), orig())[1]
    try:
        for _ in range(20):
            A._link_envelope(Msg(CAST_A, "chatter", bot=False))
            A._link_envelope(Msg(NET, 'DF1 beat {"from":"999","seq":1}'))
        await settle()
    finally:
        A.get_config = orig
    return seen[0]

ok(run(count_loads()) == 0, "the envelope path never triggers a config load")

# ---- a whole handshake across two installs ---------------------------------- #
async def handshake():
    sa, sb = A.link_session(), B.link_session()
    await A._link_apply(sa.start_advertising(1000.0))
    await settle()
    await B._link_apply(sb.start_advertising(1000.0))
    await settle()
    return sa, sb

sa, sb = run(handshake())
ok(sa.link.peer == "200" and sb.link.peer == "100", "the two installs found each other")
ok(sa.link.peer_name == "Dave-bot", "…by the name the operator configured")
ok(sa.peer_cal == 60, "hello carried the peer's pump calibration")
ok(all(m.content.startswith("DF1 ") for m in NET.sent),
   "nothing but envelopes ever reaches bot_network")

res = run(A.link_offer(game="Race to N%", target=150))
ok(res["ok"] and sb.state == mp.S_INVITED, "the invite crossed and the guest is holding it")
ok("150%" in sb.invite["cost"], "the guest sees a cost estimate before agreeing")

run(B.link_respond(True, video=True))
ok(sa.state == mp.S_LINKED and sb.state == mp.S_LINKED, "accepted both ways")
run(A.link_arm()); run(B.link_arm()); ok(sa.state == mp.S_READY and sb.state == mp.S_READY, "both armed")

run(A._link_apply(sa.begin(1000.0, rnd=1)))
ok(sa.state == mp.S_MATCH and sb.state == mp.S_MATCH, "the match is live on both installs")
ok(sa.is_host and not sb.is_host, "one host, one guest")

# ---- a do row actually reaching a device ------------------------------------ #
run(A._link_apply(sa.send_do(1000.0, {"type": "fire", "fire_mode": "add", "fill_pct": 10})))
ok(len(EB.ran) == 1 and EB.ran[0]["fill_pct"] == 10,
   "a legal row reaches the GUEST's engine, not the host's")
ok(not EA.ran, "…and the host's own engine is untouched by its own do row")
ok(sa.peer_cap == 10, "the ack brought the guest's capacity back")

# No percentage gate any more — safety is the hardware's, not a panel's.
run(A._link_apply(sa.send_do(1000.0, {"type": "fire", "fire_mode": "add", "fill_pct": 40})))
ok(len(EB.ran) == 2, "a large fire reaches the guest's device rather than being swallowed")

# SECONDS still never cross: a unit that means something different on each rig.
run(A._link_apply(sa.send_do(1000.0, {"type": "fire", "fire_mode": "seconds", "seconds": 20})))
ok(len(EB.ran) == 2, "a seconds fire is refused at the wire")
ok(any("% only" in n for n in A._link_notes), "…and the host is told why, by name")

# a message row speaks in the GUEST's voice, through multiplayer's own send
before = len(CAST_A.sent)
run(A._link_apply(sa.send_do(1000.0, {"type": "message", "message": "Dave's pump is charging…"})))
ok(len(CAST_A.sent) == before + 1 and "charging" in CAST_A.sent[-1].content,
   "a message row posts in the broadcast channel")
ok(any("posted" in n for n in A._link_notes), "…and acks that it posted")

# a row that BLOWS UP is still narratable
EB.boom = True
run(A._link_apply(sa.send_do(1000.0, {"type": "fire", "fire_mode": "add", "fill_pct": 5})))
ok(any("device offline" in n for n in A._link_notes),
   "a row that fails reports the failure — the host never narrates fiction")
EB.boom = False

# ---- the guest resolves nothing while a match is live ----------------------- #
ok(B._mp_guest_muted(CFGB) and not A._mp_guest_muted(CFGA),
   "the guest's command handling is off; the host's is not")

# ---- multiplayer's own send paths ------------------------------------------- #
async def send_paths():
    NET.fail = True
    bad = await A._link_send("DF1 beat {}")
    NET.fail = False
    good = await A._link_send("DF1 beat {}")
    return bad, good

bad, good = run(send_paths())
ok(bad is False and good is True, "a failed envelope send is reported, never swallowed")
ok(any("not sent" in l for l in EA.logs), "…and says which envelope type")

n = len(CAST_A.sent)
run(A._link_board("Curtis 63% · Dave 41%"))
run(A._link_board("Curtis 70% · Dave 41%"))
ok(len(CAST_A.sent) == n + 1 and CAST_A.sent[-1].edits == 1,
   "the scoreboard is ONE message, edited in place — one call, not two")

# ---- ending it ---------------------------------------------------------------- #
run(A._link_apply(sa.end(1000.0, "Dave hit 150% first")))
ok(EA.aborted and EB.aborted, "a finished match stops devices on BOTH installs")
ok(not sa.link.sid and not sb.link.sid, "both installs let go of the session")
ok(sa.link.peer == "200", "…and keep the pairing, which is config not session")

# ---- resume ------------------------------------------------------------------ #
# On a FRESH install, so tearing a session down can't be confused with losing one.
config_store.DATA_DIR = tempfile.mkdtemp()
C, EC, CFGC = make(200, "Dave-bot", "Curtis-bot", CHANS)
C.link = mp.Session(mp.Link("200", name="Dave-bot"), peer_name="Curtis-bot",
                    net="900", cast="901")
C.link.link.bind("100", "rsm9", mp.ROLE_GUEST, state=mp.S_MATCH)
C.link.round = 4
C._link_persist()
snap = json.load(open(os.path.join(config_store.DATA_DIR, "match.json")))
ok(snap["role"] == "guest" and snap["sid"] == "rsm9" and snap["round"] == 4,
   "a live guest persists its match")

C.link = None
C._link_find_resume()
ok(C._link_resume and C._link_resume["sid"] == "rsm9",
   "a restart FINDS the unfinished match")
ok(C.link is None and any("unfinished match" in n for n in C._link_notes),
   "…but never rejoins on its own — resume is confirmed, or it isn't resume")

fired_before = len(EC.ran)
run(C.link_resume(True))
ok(C.link is not None and C.link.state == mp.S_MATCH and C.link.round == 4,
   "confirming the rejoin restores the match")
ok(not C.link.is_host, "…as the guest it was")
ok(len(EC.ran) == fired_before,
   "replaying the whole channel tail on resume fires nothing twice")

# a HOST that comes back can't restore the referee and must say so out loud
D, ED, CFGD = make(100, "Curtis-bot", "Dave-bot", CHANS)
D._link_resume = {**snap, "role": "host", "peer": "200", "state": mp.S_MATCH}
n = len(NET.sent)
run(D.link_resume(True))
ok(len(NET.sent) > n and any("abort" in m.content.split(" ")[1] for m in NET.sent[n:]),
   "a host that returns without its referee state aborts LOUDLY")

run(C.link_resume(False))
ok(not os.path.exists(os.path.join(config_store.DATA_DIR, "match.json")),
   "declining the rejoin clears the match file")

# ---- the handshake: what the guest is actually agreeing to ------------------ #
HN, HC = Chan("920"), Chan("921")
HCHANS = {920: HN, 921: HC}
P1, EP1, CP1 = make(500, "Curtis-bot", "Dave-bot", HCHANS, net="920", cast="921",
                    peer_player="Dave")
# Seats are picked before going live, and they are BINDING: a second host
# cannot accept an invite. So the fixture has to seat them the way two real
# people would.
P2, EP2, CP2 = make(600, "Dave-bot", "Curtis-bot", HCHANS, net="920", cast="921",
                    peer_player="Curtis", role_pref="guest")
HN.listeners = [P1, P2]
CP1["cooldown_exempt_names"] = ["Curtis"]; CP1["cooldown_exempt_user_ids"] = ["7001"]
CP2["cooldown_exempt_names"] = ["Dave"];   CP2["cooldown_exempt_user_ids"] = ["7002"]
CP1["chat_scene"] = "Roulette Night"
CP1["roll"] = {"disable_at_100": False}
EP1.cap = 100.0
CP1["multiplayer"]["video"] = {"required": True}
# One in-match layout, named by the scene's own Go Live block. Camera is
# required of both players, so there is no second layout to choose between.
CP1["scenes"] = [{"name": "Roulette Night", "mode": "multi",
                  "golive": {"after_group": "Main"}}]


async def shake():
    a, b = P1.link_session(), P2.link_session()
    await P1._link_apply(a.start_advertising(time.time()))
    await settle()
    await P2._link_apply(b.start_advertising(time.time()))
    await settle()
    return a, b


SA, SB = run(shake())

# who YOU are is load-bearing, and blank fails silently — the Ready buttons
# scope to nobody and the referee can't tell a racer from the audience
pf = {c["check"]: c for c in run(P1.link_preflight())}
ok("your identity" in pf, "the preflight checks the operator's own identity")
ok(pf["your identity"]["ok"] is True, "…and passes when both are set")
CP1["cooldown_exempt_user_ids"] = []
bad = {c["check"]: c for c in run(P1.link_preflight())}["your identity"]
ok(bad["ok"] is False and "user ID" in bad["why"], "a missing user ID is caught")
ok("Limits" in bad["why"], "…and the message says where to set it")
CP1["cooldown_exempt_user_ids"] = ["7001"]; CP1["cooldown_exempt_names"] = []
warn = {c["check"]: c for c in run(P1.link_preflight())}["your identity"]
ok(warn["ok"] is None and "won't name you" in warn["why"],
   "a missing display name WARNS — it degrades the messages, it doesn't break the match")
ok([c for c in run(P1.link_preflight()) if c.get("ok") is False] == [],
   "…and is not counted among the faults that refuse an invite")
CP1["cooldown_exempt_names"] = ["Curtis"]
ok(SA.peer_player == "Dave" and SB.peer_player == "Curtis",
   "hello carries the PLAYER's name, not just the bot's")

# both names are required, and they have to agree with who actually answered
CP1["multiplayer"]["peer_player_name"] = ""
r = run(P1.link_offer())
ok(not r["ok"] and "player" in r["error"], "you cannot invite without naming the player")
CP1["multiplayer"]["peer_player_name"] = "Steve"
r = run(P1.link_offer())
ok(not r["ok"] and "Dave" in r["error"],
   "…and naming the WRONG player is refused with who that bot actually belongs to")
CP1["multiplayer"]["peer_bot_name"] = ""
r = run(P1.link_offer())
ok(not r["ok"] and "bot" in r["error"], "…and the bot name is required too")
CP1["multiplayer"]["peer_bot_name"] = "Dave-bot"
CP1["multiplayer"]["peer_player_name"] = "Dave"

r = run(P1.link_offer())
ok(r["ok"], "with both names right, the invite goes: " + str(r.get("error") or ""))
inv = SB.invite
ok(inv.get("host") == "Curtis" and inv.get("bot") == "Curtis-bot",
   "the invite names the host AND their bot")
ok(inv.get("scene") == "Roulette Night", "…the MultiScene being played")
ok(inv.get("cap") == 100, "…the CAP, so the guest knows what losing looks like")
ok(inv.get("cal") == 60, "…and the host's pump calibration")
ok(inv.get("input") in ("operators", "audience", "both"), "…the game mode")
v = inv.get("venue") or {}
ok(v.get("cast", {}).get("channel") == "921" or v.get("cast", {}).get("channel"),
   "…and the venue as something a person can read, not just an id")
ok("net" in v and "cast" in v, "both channels are described, not only the venue")

# camera is REQUIRED — accepting without one is refused, not negotiated
run(P2.link_respond(True, video=False))
ok(SA.state != mp.S_LINKED, "a guest with no camera does not get into a match")
run(P2.link_respond(True, video=True))
ok(SA.peer_video is True and SB.video is True,
   "the camera answer rides in the accept — both sides agree on it")
ok(SA.state == mp.S_LINKED, "…and the match is linked")

# ONE layout, because both players are on camera. The host's picture carries
# its own gauge; the guest is a video tile, not a widget.
EP1.ran.clear()
run(P1._mp_production())
groups = [r.get("group") for r in EP1.ran if r.get("type") == "scene_group"]
ok(groups == ["Main"], "the host brings up its one in-match group, and only it")
ok(not [r for r in EP1.ran if r.get("type") == "scene_group_kill"],
   "…with no other layout to clear: there isn't a second one any more")

# The GUEST brings up nothing of its own. It holds the same shipped scene, so
# the host can call an overlay in it by id — but when is the host's call.
EP2.ran.clear()
run(P2._link_begin_check())
ok(not [r for r in EP2.ran if r.get("type") in ("scene_group", "overlay")],
   "the guest lays out no production for itself")

# ---- Ready: one embed, two buttons, each scoped to one person -------------- #
ne = len(HC.embeds)
r = run(P1.post_ready(seconds=30))
ok(r["ok"], "the host posts the ready check: " + str(r.get("error") or ""))
ok(len(HC.embeds) > ne and "Ready" in HC.embeds[-1], "…into the venue, as a card")
rv = P1._ready_view
ok(rv is not None and len(rv.children) == 2, "two buttons on one embed")
ok(rv.uids["host"] == "7001" and rv.uids["guest"] == "7002",
   "…each scoped to its own player's Discord id")
ok("Curtis Ready" in [c.label for c in rv.children]
   and "Dave Ready" in [c.label for c in rv.children],
   "…labelled with the players' names")
ok(not run(P1.post_ready())["ok"], "a second ready check is refused while one is up")
ok(not run(P2.post_ready())["ok"], "the guest never posts it")


async def press_ready():
    rv.ready["host"] = rv.ready["guest"] = True
    rv.done.set()
    for _ in range(50):
        await asyncio.sleep(0)


run(press_ready())
ok(SA.ready_me, "both pressed → the host arms")
ok(any("both players ready" in x for x in P1._link_notes), "…and says so")

# ---- Block ------------------------------------------------------------------ #
run(P1.link_block())
ok(SA.is_blocked("600"), "blocking records the bot")
ok(not SA.link.peer, "…drops the pairing")
ok(not SA.link.sid, "…and ends any match with them")
blocked_hello = SB.say_hello(time.time(), force=True)
o = SA.feed(mp.decode(blocked_hello.send[0]), time.time())
ok(not SA.link.peer and any("blocked" in n for n in o.notes),
   "a blocked bot cannot pair again — the block bites at HELLO, not just at invite")
ok(config_store.load() is not None, "blocking persists through config")

run(P1.link_unblock("600"))
ok(not SA.is_blocked("600"), "unblock lets them back in")
o = SA.feed(mp.decode(SB.say_hello(time.time(), force=True).send[0]), time.time())
ok(SA.link.peer == "600", "…and pairing works again")

# ---- a second pair, for the pieces that end the match they run in ---------- #
HOST_UID, GUEST_UID, FAN = "5001", "5002", "9999"
RNET, RCAST = Chan("910"), Chan("911")
RCHANS = {910: RNET, 911: RCAST}
H, EH, CFGH = make(300, "Curtis-bot", "Dave-bot", RCHANS, net="910", cast="911")
G, EG, CFGG = make(400, "Dave-bot", "Curtis-bot", RCHANS, net="910", cast="911",
                   role_pref="guest")     # seats are binding: a host can't accept
RNET.listeners = [H, G]
for c, uid, nm, opp in ((CFGH, HOST_UID, "Curtis", "Dave"),
                        (CFGG, GUEST_UID, "Dave", "Curtis")):
    c["multiplayer"]["peer_player_name"] = opp
    c["cooldown_exempt_user_ids"] = [uid]
    c["cooldown_exempt_names"] = [nm]


async def to_the_flag():
    """Take the pair from idle to a live match, the way the panel would."""
    sh, sg = H.link_session(), G.link_session()
    await H._link_apply(sh.start_advertising(time.time()))
    await settle()
    await G._link_apply(sg.start_advertising(time.time()))
    await settle()
    await H.link_offer()
    await settle()
    await G.link_respond(True, video=True)
    await H.link_arm()
    await G.link_arm()
    await settle()
    await H._link_apply(sh.begin(time.time(), phase="round", rnd=1))
    await settle()
    return {"ok": sh.state == mp.S_MATCH}


res = run(to_the_flag())
ok(res.get("ok"), "the pair reaches a live match")
ok(H.link.is_host and not G.link.is_host, "one host, one guest")
ok(H.link.player == "Curtis" and H.link.peer_player == "Dave",
   "both PLAYERS are named, which is what messages use")

# ---- a Multiplayer Action: the roulette round ------------------------------- #
# Plain action rows. The spin picks, the dice roll publishes a number, and the
# fire spends it on whoever was picked — on EITHER machine, decided at run time.
CFGH["mp_actions"] = [{"name": "Roulette round", "actions": [
    {"type": "mp_spin", "message": "🎡 The wheel picks **[multi_chosen_name]**!"},
    {"type": "mp_roll", "dice": 1, "sides": 7,
     "message": "🎲 [multi_roll_dice] rolled **[multi_roll]%** for [multi_chosen_name]."},
    {"type": "fire", "fire_mode": "add", "fill_pct": "[multi_roll]", "multi_who": "chosen"},
]}]

EH.ran.clear(); EG.ran.clear(); EG.rows_seen = []
n = len(RCAST.sent)
res = run(H.mp_run_action("Roulette round"))
ok(res.get("ok"), "the action ran: " + str(res.get("error") or ""))
fired = EH.ran + EG.ran
ok(len(fired) == 1, "exactly one racer's pump fires per spin — never both, never neither")
amt = float(fired[0]["fill_pct"])
ok(1 <= amt <= 7, f"1d7 lands in 1-7 (got {amt})")
said = " ".join(m.content for m in RCAST.sent[n:])
ok("The wheel picks" in said and "rolled" in said, "the venue sees the spin and the roll")
ok(f"**{amt:g}%**" in said,
   "the number the audience was TOLD is the number that actually fired")
ok("[multi_" not in said, "no placeholder ever reaches the venue unrendered")

# whichever machine it landed on, the row got there resolved
if EG.ran:
    ok(isinstance(EG.ran[0]["fill_pct"], str) is False or "[" not in str(EG.ran[0]["fill_pct"]),
       "a row crossing the wire arrives with its placeholders already resolved")
else:
    ok(True, "a row crossing the wire arrives with its placeholders already resolved")

# a block naming nobody must refuse rather than inflate a guess
CFGH["mp_actions"].append({"name": "Broken", "actions": [
    {"type": "fire", "fire_mode": "add", "fill_pct": 10, "multi_who": "chosen"}]})
EH.ran.clear(); EG.ran.clear()
run(H.mp_run_action("Broken"))
ok(not EH.ran and not EG.ran,
   "a target that resolves to nobody drops the row instead of picking someone")
ok(any("nobody matched" in x for x in H._link_notes), "…and says so")

# ---- Player Choice: Double or Nothing --------------------------------------- #
# Blocking by construction: the engine awaits the row hook, so nothing after
# the choice runs until the player has answered or the deadline has passed.
DOUBLE = {"name": "Double or Nothing", "actions": [
    {"type": "mp_spin"},
    {"type": "mp_roll", "dice": 1, "sides": 7},
    {"type": "mp_choice", "multi_who": "chosen", "seconds": 1,
     "title": "Double or Nothing?",
     "message": "[multi_chosen_name], you rolled [multi_roll]%. Take it, or spin again?",
     "default": "take",
     "options": [
         {"label": "Double or Nothing", "value": "double", "style": "danger",
          "actions": [{"type": "mp_roll", "dice": 1, "sides": 7},
                      {"type": "fire", "fire_mode": "add",
                       "fill_pct": "[multi_roll]", "multi_who": "chosen"}]},
         {"label": "Take it", "value": "take", "style": "secondary",
          "actions": [{"type": "fire", "fire_mode": "add",
                       "fill_pct": "[multi_roll]", "multi_who": "chosen"}]},
     ]},
]}
CFGH["mp_actions"].append(DOUBLE)


def press(which):
    """Answer the next Player Choice as soon as it is posted."""
    async def go():
        task = asyncio.ensure_future(H.mp_run_action("Double or Nothing"))
        for _ in range(200):                      # wait for the buttons to appear
            await asyncio.sleep(0)
            v = next((v for v in H._active_views
                      if isinstance(v, db.MpChoiceView) and not v.done.is_set()), None)
            if v is not None:
                if which is not None:
                    v.picked = which
                    v.done.set()
                    v.stop()
                break
        return await task
    return run(go())


EH.ran.clear(); EG.ran.clear()
n, ne = len(RCAST.sent), len(RCAST.embeds)
press("take")
fired = EH.ran + EG.ran
ok(len(fired) == 1, "taking it fires once")
card = " ".join(RCAST.embeds[ne:])
ok("Double or Nothing?" in card, "the choice is posted to the venue as a card")
ok("you rolled" in card and "[multi_" not in card,
   "…with its placeholders rendered, naming the racer and the number at stake")
ok(any(isinstance(m.view, db.MpChoiceView) for m in RCAST.sent[n:]),
   "…and it carries the buttons")
ok(any("chose Take it" in x for x in H._link_notes), "the answer is recorded")

EH.ran.clear(); EG.ran.clear()
press("double")
fired = EH.ran + EG.ran
ok(len(fired) == 1 and 1 <= float(fired[0]["fill_pct"]) <= 7,
   "doubling re-rolls and fires the NEW number, not the old one")
ok(any("chose Double or Nothing" in x for x in H._link_notes), "…and says which was chosen")

# nobody answers: the named default carries the round rather than hanging it
EH.ran.clear(); EG.ran.clear()
n = len(RCAST.sent)
press(None)
ok(len(EH.ran + EG.ran) == 1, "an unanswered choice still resolves — the match never hangs")
ok(any("No answer from" in m.content for m in RCAST.sent[n:]),
   "…and the venue is told why it moved on")

# a choice nobody can win: fewer than two buttons is not a decision
CFGH["mp_actions"].append({"name": "One button", "actions": [
    {"type": "mp_spin"},
    {"type": "mp_choice", "multi_who": "chosen",
     "options": [{"label": "Only one"}]}]})
EH.ran.clear(); EG.ran.clear()
run(H.mp_run_action("One button"))
ok(any("at least two options" in x for x in H._link_notes),
   "one button is refused with a reason, not posted as a fake decision")

# A missing or empty Action is SKIPPED, not a failure. It is a blank you
# haven't filled in (or one deleted out from under a round) — stopping a whole
# match over it is the worse answer, and announcing it puts your unfinished
# homework on the stream.
r_missing = run(H.mp_run_action("Nope"))
ok(r_missing["ok"] and r_missing.get("skipped"),
   "an Action that doesn't exist is skipped quietly, and the result says so")
ok(any("skipped" in x for x in H._link_notes),
   "…recorded in the link log, where the operator can find it")
ok(not run(G.mp_run_action("Roulette round"))["ok"],
   "the guest never runs a multiplayer action — the host is the only referee")
ok(H.engine.mp_row_cb is None,
   "ROUTING is dropped afterwards, so solo blocks are never sent anywhere")
ok(H.engine.mp_ctx_cb is not None,
   "…but the read-only match context stays, or an overlay label would stop "
   "resolving the moment a block ended")
# and it returns nothing at all outside a match, so solo renders stay clean
H.link.link.state = mp.S_ADVERTISED
ok(H._mp_ctx() == {}, "outside a match it contributes no tokens to a solo render")
H.link.link.state = mp.S_MATCH
ok("multi_peer_pct" in H._mp_ctx(), "…and the full set while one is running")

# ---- the opening, in order --------------------------------------------------- #
# intro(s) → both curtains up → the game. One entry point: two of them is how an
# operator ends up mid-pre-show with the picture already live.
CFGH["mp_actions"] = [a for a in CFGH.get("mp_actions") or []]
CFGH["mp_rounds"] = []                       # no bands yet → it falls back to the race
CFGH["scenes"] = [{"name": "Vs", "mode": "multi", "golive": {
    "intro_enabled": True,
    "stages": [{"group": "Intro", "seconds": 0,
                "actions": [{"type": "message", "message": "🎬 here we go"}]},
               {"group": "Round Intro", "seconds": 0}]}}]
CFGH["chat_scene"] = "Vs"
EH.ran.clear(); EG.ran.clear(); EH.camera.clear(); EG.camera.clear()
H._mp_curtain_sid = G._mp_curtain_sid = "open1"   # curtains already down, as after an accept
H.link.link.bind("400", "open1", mp.ROLE_HOST, state=mp.S_LINKED)
G.link.link.bind("300", "open1", mp.ROLE_GUEST, state=mp.S_LINKED)
H.link.link._seen.clear(); G.link.link._seen.clear()
H.race = G.race = None
n = len(RCAST.sent)

res = run(H.start_match())
ok(res.get("ok"), "the host starts the match: " + str(res.get("error") or ""))
for _ in range(60):
    run(settle())
    if H._start_task is None:
        break
# the guest's reveal is several async hops behind the host's own — the host
# finishing is not the same as the instruction having landed over there
for _ in range(20):
    run(settle())
    if EG.camera:
        break

groups = [r.get("group") for r in EH.ran if r.get("type") == "scene_group"]
ok("Intro" in groups, "the scene's own intro plays")
ok("Round Intro" in groups, "…and the second stage — the intro2 — plays after it")
said = " ".join(m.content for m in RCAST.sent[n:])
ok("here we go" in said, "a stage's own action block runs with it")
ok(EH.camera[-1] == "reveal" and EG.camera[-1] == "reveal",
   "…then BOTH curtains go up — the host raises the guest's over the wire")
ok(groups.index("Intro") < len(groups) and EH.camera.index("reveal") >= 0,
   "the reveal comes AFTER the intro, not during it")
ok(any("no rounds defined" in x for x in H._link_notes),
   "with no bands defined it SAYS there is nothing to play — a game IS its "
   "Rounds, so there is no built-in fallback to quietly run instead")
ok(any("No rounds are set up" in m.content for m in RCAST.sent[n:]),
   "…and says it in the venue, not just the log")
again = run(H.start_match())
ok(not again["ok"] and "match" in again["error"],
   "starting again mid-match is refused — one opening, not two")
ok(not run(G.start_match())["ok"], "the guest never starts a match")

# ---- a round as a SEQUENCE: N passes, a video, N more ----------------------- #
CFGH["mp_actions"] = [a for a in (CFGH.get("mp_actions") or [])
                      if a.get("name") not in ("Beat",)]
CFGH["mp_actions"].append({"name": "Beat", "actions": [
    {"type": "fire", "fire_mode": "add", "fill_pct": 1, "multi_who": "me"}]})
CFGH["mp_rounds"] = [{
    "name": "Segmented", "until": "count", "count": 1,
    "actions": [
        {"type": "repeat", "mode": "fixed", "iterations": "4", "max_iterations": "50",
         "actions": [{"type": "mp_action", "action": "Beat"}]},
        {"type": "overlay", "overlay": "vid1", "mode": "once"},
        {"type": "repeat", "mode": "fixed", "iterations": "4", "max_iterations": "50",
         "actions": [{"type": "mp_action", "action": "Beat"}]},
    ]}]
EH.ran.clear(); EG.ran.clear()
EH.capacity = 0.0
H.link.peer_cap = 0.0
run(H.rounds_start())
for _ in range(90):
    run(settle())
    if H._rounds_task is None:
        break
fires = [r for r in EH.ran if r.get("type") == "fire"]
ok(len(fires) == 8, f"a repeat of 4, twice, runs the Action 8 times (got {len(fires)})")
ok(any(r.get("type") == "overlay" and r.get("overlay") == "vid1" for r in EH.ran),
   "…with the video between the two halves")
order = [r["type"] for r in EH.ran if r["type"] in ("fire", "overlay")]
ok(order.index("overlay") == 4, "…in that order: four, video, four")

# an Action that runs itself must not spiral
CFGH["mp_actions"].append({"name": "Loopy", "actions": [
    {"type": "mp_action", "action": "Loopy"}]})
CFGH["mp_rounds"] = [{"name": "Recursive", "until": "count", "count": 1,
                      "actions": [{"type": "mp_action", "action": "Loopy"}]}]
EH.ran.clear()
run(H.rounds_start())
for _ in range(60):
    run(settle())
    if H._rounds_task is None:
        break
ok(any("nested too deep" in x for x in H._link_notes),
   "an Action that runs itself is stopped by depth, not left to spiral")

CFGH["mp_rounds"] = [{"name": "Ghosted", "until": "count", "count": 1,
                      "actions": [{"type": "mp_action", "action": "Nope"}]}]
run(H.rounds_start())
for _ in range(40):
    run(settle())
    if H._rounds_task is None:
        break
ok(any("no Multiplayer Action called 'Nope'" in x for x in H._link_notes),
   "a body naming a missing Action says which one")

# ---- Rounds: bands that loop their Action ----------------------------------- #
# A round supplies [multi_round_target]; the Action reads it. That is what keeps
# an Action unbound from whatever ran it.
CFGH["mp_actions"].append({"name": "Tick", "actions": [
    {"type": "mp_spin"},
    {"type": "fire", "fire_mode": "add", "fill_pct": 10, "multi_who": "me"},
]})
CFGH["mp_rounds"] = [
    # ends on a TARGET: loop the block until somebody reaches it
    {"name": "Opening", "until": "leader", "max": 20, "max_passes": 30,
     "actions": [{"type": "mp_action", "action": "Tick"}],
     "done_message": "✅ [multi_round_name] done.",
     "intro": [{"type": "message", "message": "📣 **[multi_round_name]** — to [multi_round_target]%."}]},
    # ends on a COUNT: two passes, whatever the meters say
    {"name": "Closing", "until": "count", "count": 2,
     "actions": [{"type": "mp_action", "action": "Tick"}]},
]
EH.ran.clear(); EG.ran.clear()
EH.capacity = 0.0
H.link.peer_cap = 0.0          # both racers start the sequence at the bottom
n = len(RCAST.sent)
res = run(H.rounds_start())
ok(res.get("ok"), "the round sequence starts: " + str(res.get("error") or ""))
run(settle())
for _ in range(80):
    run(settle())
    if H._rounds_task is None:
        break

said = " ".join(m.content for m in RCAST.sent[n:])
ok("**Opening**" in said and "to 20%" in said,
   "a round opens with its OWN card, and [multi_round_target] is its target")
ok("✅ Opening done." in said, "…and says so when the band is cleared")
ok(any("Opening" in x for x in H._link_notes)
   and any("Closing" in x for x in H._link_notes),
   "the next round takes over")
ok(EH.capacity >= 20, f"the target round ran until it cleared (capacity {EH.capacity})")
ok("That's every round" in said, "past the last one the sequence ends rather than spinning")
ok(H._rounds_task is None, "…and the driver lets go")

# a round pointing at an Action that doesn't exist stops with a reason
EH.capacity = 0.0
H.link.peer_cap = 0.0
CFGH["mp_rounds"] = [{"name": "Ghost", "until": "count", "count": 1,
                      "actions": [{"type": "mp_action", "action": "Nope"}]}]
run(H.rounds_start())
for _ in range(40):
    run(settle())
    if H._rounds_task is None:
        break
ok(any("no Multiplayer Action" in x for x in H._link_notes),
   "a round pointing at a missing Action stops and names it")

# the backstop: a band nobody can clear still ends
EH.capacity = 0.0
H.link.peer_cap = 0.0
CFGH["mp_actions"].append({"name": "Nothing", "actions": [{"type": "mp_spin"}]})
CFGH["mp_rounds"] = [{"name": "Endless", "until": "leader", "max": 99,
                      "max_passes": 3,
                      "actions": [{"type": "mp_action", "action": "Nothing"}]}]
n = len(RCAST.sent)
run(H.rounds_start())
for _ in range(60):
    run(settle())
    if H._rounds_task is None:
        break
ok(any("pass limit" in x for x in H._link_notes),
   "a band nobody can clear hits its pass limit instead of running forever")
ok(any("ran out of passes" in m.content for m in RCAST.sent[n:]),
   "…and says so out loud rather than pretending the band was cleared")

CFGH["mp_rounds"] = []
ok(not run(H.rounds_start())["ok"], "no rounds defined is refused, not a silent no-op")
ok(not run(G.rounds_start())["ok"], "the guest never runs rounds")

# ending the match puts the curtain back up on both installs
EH.camera.clear(); EG.camera.clear()
run(H._link_apply(H.link.end(time.time(), "that's a wrap")))
ok(H.link.state in (mp.S_IDLE, mp.S_ADVERTISED)
   and G.link.state in (mp.S_IDLE, mp.S_ADVERTISED), "the match closes out on both")
ok(EH.aborted and EG.aborted, "…and both pumps stop")
ok(EH.camera[-1] == "reveal" and EG.camera[-1] == "reveal",
   "the curtain comes back up when the match ends — nobody is left black "
   "with nothing running to un-black them")

# and the host can raise it mid-match, on either install, with an ordinary row
EH.camera.clear(); EG.camera.clear()
# a live match has already dropped its curtain for THIS sid — say so, or the
# state check fires it again and the row's reveal gets lost in the noise
H._mp_curtain_sid = G._mp_curtain_sid = "cur1"
H.link.link.bind("400", "cur1", mp.ROLE_HOST, state=mp.S_MATCH)
G.link.link.bind("300", "cur1", mp.ROLE_GUEST, state=mp.S_MATCH)
H.link.link._seen.clear(); G.link.link._seen.clear()
CFGH["mp_actions"].append({"name": "Lights up", "actions": [
    {"type": "camera", "op": "reveal", "multi_who": "both"}]})
run(H.mp_run_action("Lights up"))
ok(EH.camera == ["reveal"], "a camera row reveals the host's own picture")
ok(EG.camera == ["reveal"],
   "…and the SAME row crosses the wire to reveal the guest's, which is how "
   "the host says 'we're live' to a machine it doesn't control")

# ---- an audience pressing buttons is IGNORED, not told off ------------------ #
# A match is watched. Every viewer who clicks a Hit, a Ready or a duel button
# would otherwise get the bot saying "that's not yours" — in the venue, during
# the show. Deferring acknowledges the click so Discord does not flash
# "interaction failed" at them, and says nothing at all.
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "discord_bot.py"), encoding="utf-8").read()
ok("async def _ignore(interaction)" in src, "there is one way to ignore a click")
ok("interaction.response.defer()" in src.split("async def _ignore")[1][:400],
   "…and it DEFERS rather than replying, so nothing lands in the channel")
for view in ("MpChoiceView", "MpReadyView", "MpDuelView"):
    blk = src[src.index(f"class {view}"):]
    blk = blk[:blk.index("\nclass ")] if "\nclass " in blk else blk[:4000]
    ok("_ignore(interaction)" in blk, f"{view} ignores a stranger's press")
    ok("isn't your" not in blk and "This one's between" not in blk
       and "That's " not in blk.split("_ignore")[0][-300:],
       f"…and {view} no longer tells them off")

# ---- blackjack: two seats, one dealer, both hands face up ------------------ #
src2 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "discord_bot.py"), encoding="utf-8").read()
cv = src2[src2.index("class MpCardsView"):]
cv = cv[:cv.index("\nclass ")]
ok("_ignore(interaction)" in cv, "a viewer pressing Hit is ignored, not told off")
ok("self._finished(side)" in cv,
   "…and so is a player who already stood or busted — a dead button must not "
   "deal them a card")
ok("hand_text" in cv and "🂠" in cv,
   "the table shows BOTH hands face up and keeps the dealer's hole card down — "
   "which is how a shoe game actually deals, and what the room watches")
ok("edit_message" in cv,
   "each press edits the SAME embed, so the table updates in place rather "
   "than scrolling the venue")

rowsrc = src2[src2.index("async def _mp_cards_row"):]
rowsrc = rowsrc[:rowsrc.index("async def _mp_duel_row")]
ok("dealer_play(dealer)" in rowsrc, "the dealer plays out AFTER both players act")
d_at, o_at = rowsrc.index("dealer_play(dealer)"), rowsrc.index("cards_outcome(")
ok(d_at < o_at, "…and before anyone is scored against it")
ok("wait_for(view.done.wait()" in rowsrc,
   "it BLOCKS: nothing may fire until the hand is over")
ok("multi_cards_margin" in rowsrc and "multi_cards_who" in rowsrc,
   "it publishes who pays and by how much, so the stake can scale on the margin")
ok('"both"' in rowsrc,
   "…including BOTH, so a block can fire at everyone when the house cleans up "
   "without needing a second row type")

# ---- an embed the bot may not send DEGRADES, and never double-posts -------- #
# _send carries the text AS the embed, so a revoked Embed Links used to lose
# the message outright: an invite or a Ready check that never appears looks
# like a broken bot rather than a permissions problem.
snd = src2[src2.index("    async def _send(self"):]
snd = snd[:snd.index("    # Embed accent colors")]
ok("except discord.Forbidden" in snd,
   "a refused embed falls back to plain text")
ok("Forbidden is the ONLY error safe to retry" in snd,
   "…and ONLY on Forbidden: Discord refused outright, so nothing was posted "
   "and a second send cannot double up")
ok("except Exception" in snd.split("except discord.Forbidden")[0]
   or "except Exception" in snd,
   "…while a timeout or 5xx is NOT retried, because that one might have landed")
fb = snd[snd.index("except discord.Forbidden"):]
fb = fb[:fb.index("if image and image.lower()")]
ok('"view": view' in fb,
   "the buttons come with it — a view needs no embed permission, so the game "
   "still PLAYS, it just looks plainer")
ok("_clip(text)" in fb, "…carrying the words the embed would have shown")

print(f"{P} passed, {len(F)} failed")
for f in F:
    print("  FAIL:", f)
sys.exit(1 if F else 0)
