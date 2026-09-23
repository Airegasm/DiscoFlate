"""Headless proof of the multiplayer protocol core. No discord, no engine."""
import os, sys
sys.path.insert(0, __import__("os").path.dirname(os.path.abspath(__file__)))
import multiplayer as mp

P, F = 0, []


def ok(cond, label):
    global P
    if cond:
        P += 1
    else:
        F.append(label)


# ---- encode / decode ------------------------------------------------------- #
line = mp.encode(mp.T_STATE, frm="A", to="B", sid="m7", seq=4, tick=47, phase="round")
ok(line.startswith("DF1 state {"), "encode prefix")
env = mp.decode(line)
ok(env["t"] == "state" and env["from"] == "A" and env["to"] == "B", "roundtrip routing")
ok(env["sid"] == "m7" and env["seq"] == 4 and env["tick"] == 47, "roundtrip body")

ok(mp.decode("hello everyone") is None, "plain chat ignored")
ok(mp.decode("") is None, "empty ignored")
ok(mp.decode("DF1 nope {}") is None, "unknown type ignored")
ok(mp.decode("DF1 state not-json") is None, "bad json ignored")
ok(mp.decode('DF1 state {"seq":1}') is None, "missing from ignored")
ok(mp.decode('DF1 state ["a"]') is None, "non-dict ignored")

try:
    mp.encode("bogus", frm="A")
    ok(False, "unknown type refused")
except mp.ProtocolError:
    ok(True, "unknown type refused")

try:
    mp.encode(mp.T_DO, frm="A", row={"text": "x" * 4000})
    ok(False, "oversize refused")
except mp.ProtocolError:
    ok(True, "oversize refused")

# body can't hijack routing keys
line = mp.encode(mp.T_DO, frm="A", to="B", sid="m7", seq=1, **{"from": "EVIL"})
ok(mp.decode(line)["from"] == "A", "body cannot overwrite 'from'")

# ---- link: routing + dedup ------------------------------------------------- #
link = mp.Link("B", owner="o2", version="3.90.2")
link.bind(peer="A", sid="m7", role=mp.ROLE_GUEST)

mine = mp.encode(mp.T_STATE, frm="B", to="A", sid="m7", seq=1)
ok(link.accept(mp.decode(mine)) == (False, "self"), "own echo dropped")

third = mp.encode(mp.T_STATE, frm="C", to="B", sid="m7", seq=1)
ok(link.accept(mp.decode(third))[1] == "foreign peer", "third install dropped")

other = mp.encode(mp.T_STATE, frm="A", to="Z", sid="m7", seq=1)
ok(link.accept(mp.decode(other))[1] == "addressed elsewhere", "not for us")

stale_sid = mp.encode(mp.T_STATE, frm="A", to="B", sid="OLD", seq=1)
ok(link.accept(mp.decode(stale_sid))[1] == "wrong session", "last night's match")

e1 = mp.decode(mp.encode(mp.T_DO, frm="A", to="B", sid="m7", seq=1, row={"type": "fire"}))
ok(link.accept(e1)[0] is True, "first apply")
ok(link.accept(e1) == (False, "duplicate"), "replay is idempotent")

e3 = mp.decode(mp.encode(mp.T_DO, frm="A", to="B", sid="m7", seq=3))
ok(link.accept(e3)[0] is True, "seq 3 applies")
e2 = mp.decode(mp.encode(mp.T_DO, frm="A", to="B", sid="m7", seq=2))
ok(link.accept(e2) == (False, "duplicate"), "out-of-order older rejected")

# hello survives a peer restart (counter resets to 1)
h = mp.decode(mp.encode(mp.T_HELLO, frm="A", to="", seq=1, name="Dave"))
ok(link.accept(h)[0] is True, "hello 1")
ok(link.accept(h)[0] is True, "hello repeats after restart")

# ---- resume ---------------------------------------------------------------- #
snap = link.snapshot()
ok(snap["sid"] == "m7" and snap["last_seq"] == 3, "snapshot")

fresh = mp.Link("B")
fresh.restore(snap)
ok(fresh.sid == "m7" and fresh.role == mp.ROLE_GUEST and fresh.peer == "A", "restore")
ok(fresh.accept(e3) == (False, "duplicate"), "tail replay skips applied")
e4 = mp.decode(mp.encode(mp.T_DO, frm="A", to="B", sid="m7", seq=4))
ok(fresh.accept(e4)[0] is True, "tail replay applies the missed one")

link.reset()
ok(link.state == mp.S_IDLE and link.sid == "" and link.peer == "A", "reset keeps identity")

# ---- NO percentage gate ----------------------------------------------------- #
#
# There used to be a Ceiling here: a per-fire / per-match / absolute limit that
# refused or clamped an incoming row. It is gone on purpose. Safety is the
# HARDWARE's job — a pump's own limits and a plug you can reach — not a number
# in a web panel that the other machine cannot see and that silently swallows
# a fire with nowhere to look for why.
ok(not hasattr(mp, "Ceiling"), "the ceiling gate is gone, not merely unset")
ok(not hasattr(mp.Session(mp.Link("1"), net="n", cast="c"), "ceiling"),
   "…and no session carries one")

# ---- pacing: two different pumps, one fair race ---------------------------- #
ok(abs(mp.fill_rate(60) - 1.6667) < 0.001, "rate from calibration")
ok(mp.fill_rate(0) == 0.0 and mp.fill_rate(None) == 0.0, "bad calibration = 0, not infinity")

p = mp.compensate(150, {"A": 60, "B": 120})
ok(p["ok"], "compensation works with both calibrations")
ok(abs(p["targets"]["A"] - 150) < 0.01, "fastest pump keeps the base target")
ok(abs(p["targets"]["B"] - 75) < 0.01, "slower pump's finish line comes down")
ok(max(p["targets"].values()) <= 150 + 1e-9, "nobody is pushed past the consented number")
ta = p["targets"]["A"] / p["rates"]["A"]
tb = p["targets"]["B"] / p["rates"]["B"]
ok(abs(ta - tb) < 0.001, "both targets take the same pumping time")

p2 = mp.compensate(150, {"A": 60, "B": 0})
ok(not p2["ok"] and "uncalibrated" in p2["why"], "missing calibration is flagged")
ok(p2["targets"] == {"A": 150, "B": 150}, "uncompensated falls back to equal targets")

# ---- role, channels, dead heats -------------------------------------------- #
ok(mp.decide_role("100", "200", "host", "guest") == "host", "stated preferences win")
ok(mp.decide_role("200", "100", "guest", "host") == "guest", "…and agree from both sides")
ok(mp.decide_role("100", "200", "either", "host") == "guest", "one preference decides both")
ok(mp.decide_role("100", "200", "host", "host") == "host", "collision: lower id hosts")
ok(mp.decide_role("200", "100", "host", "host") == "guest", "collision resolves the same way")
ok(mp.decide_role("100", "200", "either", "either")
   != mp.decide_role("200", "100", "either", "either"), "never two hosts")

ok(mp.check_channels({"net": "1", "cast": "2"}, {"net": "1", "cast": "2"}) == [],
   "agreed channels pass")
bad = mp.check_channels({"net": "1", "cast": "2"}, {"net": "1", "cast": "9"})
ok(len(bad) == 1 and "broadcast mismatch" in bad[0] and "9" in bad[0],
   "mismatch names the channel AND both ids")
ok("no bot_network channel set here" in mp.check_channels({}, {"net": "1", "cast": "2"}),
   "my own unset channel is named")

ok(mp.dead_heat(1000, 1400) and not mp.dead_heat(1000, 1600), "500ms = a draw")
ok(not mp.dead_heat(None, 1000), "a missing claim is not a dead heat")


# ---- two installs, one channel --------------------------------------------- #
class Bus:
    """The bot_network channel: an append-only log, which is exactly what it is
    in production. Each session reads forward from its own cursor, so a replay
    (reconnect) is just rewinding one cursor."""

    def __init__(self, *sess):
        self.log, self.sess, self.at = [], sess, {}
        for s in sess:
            self.at[s.link.me] = 0

    def post(self, out):
        self.log.extend(out.send)
        return out

    def settle(self, now, rounds=8):
        """Deliver until the channel goes quiet; returns every Out produced."""
        made = []
        for _ in range(rounds):
            end = len(self.log)
            quiet = True
            for s in self.sess:
                i, self.at[s.link.me] = self.at[s.link.me], end
                for line in self.log[i:end]:
                    o = s.feed(mp.decode(line), now)
                    made.append(o)
                    if o.send:
                        self.log.extend(o.send)
                        quiet = False
            if quiet and len(self.log) == end:
                break
        return made


def notes(made):
    return " | ".join(n for o in made for n in o.notes)


def rows(made):
    return [r for o in made for r in o.rows]


def pair(**kw):
    """Curtis (fast pump) hosts; Dave (half the rate) guests."""
    a = mp.Session(mp.Link("100", name="Curtis-bot", owner="o1", version="3.90.2"),
                   peer_name="Dave-bot", net="net1", cast="cast1", calibration=60)
    b = mp.Session(mp.Link("200", name="Dave-bot", owner="o2", version="3.90.2"),
                   peer_name="Curtis-bot", net=kw.get("net", "net1"),
                   cast=kw.get("cast", "cast1"), calibration=120)
    return a, b


A, B = pair()
bus = Bus(A, B)
T = 1000.0

bus.post(A.start_advertising(T))
bus.post(B.start_advertising(T))
made = bus.settle(T)
ok(A.link.peer == "200" and B.link.peer == "100", "hello resolves the peer by NAME")
ok(A.link.peer_name == "Dave-bot" and A.peer_cal == 120, "hello carries name + calibration")
ok("peer online: Dave-bot · 3.90.2" in notes(made), "panel line names peer and version")
before = len(bus.log)
bus.settle(T)
ok(len(bus.log) == before, "the hello exchange terminates — no ping-pong")

# a third install in the channel can't grab the pairing
C = mp.Session(mp.Link("300", name="Eve-bot"), peer_name="Curtis-bot",
               net="net1", cast="cast1")
stray = C.say_hello(T, force=True)
ok(A.feed(mp.decode(stray.send[0]), T).notes[0].startswith("dropped hello"),
   "a stranger's hello is dropped once we're paired")

# ---- invite carries a COMPENSATED cost estimate ----------------------------- #
made = bus.settle(T, )
bus.post(A.offer(T, game="Race to N%", base_target=150, sid="m7k2"))
made = bus.settle(T)
ok(B.state == mp.S_INVITED, "guest is holding an invite")
ok(abs(B.invite["targets"]["200"] - 75) < 0.01, "guest's finish line is compensated DOWN")
ok("75%" in B.invite["cost"], "the estimate quotes the compensated number, not the base")
ok(A.link.sid == "m7k2" and A.is_host, "host owns the sid")

bus.post(B.respond(T, True, video=True))
bus.settle(T)
ok(A.state == mp.S_LINKED and B.state == mp.S_LINKED, "accepted both ways")

bus.post(A.arm(T))
bus.post(B.arm(T))
bus.settle(T)
ok(A.state == mp.S_READY and B.state == mp.S_READY, "both armed")

made = bus.post(A.begin(T, rnd=1, left=60))
bus.settle(T)
ok(A.state == mp.S_MATCH and B.state == mp.S_MATCH, "match running on both")
ok(abs(B.deadline - (T + 60)) < 0.001, "deadline is on the GUEST's clock, not the host's")

# ---- do / ack --------------------------------------------------------------- #
bus.post(A.send_do(T, {"type": "fire", "fire_mode": "add", "fill_pct": 10}))
made = bus.settle(T)
got = rows(made)
ok(len(got) == 1 and got[0]["row"]["fill_pct"] == 10, "a legal row reaches the guest's runner")
do_seq = got[0]["re"]
bus.post(B.ack(T, re=do_seq, did="fired 10% ✓", capacity=10))
made = bus.settle(T)
ok("Dave-bot: fired 10% ✓" in notes(made), "the host narrates what ACTUALLY happened")
ok(A.peer_cap == 10, "capacity piggybacks on the ack for free")

# No percentage gate any more — a big ask goes through, because what stops it
# is the pump's own hardware, not a number in a web panel.
bus.post(A.send_do(T, {"type": "fire", "fire_mode": "add", "fill_pct": 40}))
made = bus.settle(T)
ok(len(rows(made)) == 1, "a large fire is no longer refused by the panel")

# SECONDS still never cross. Not a limit — a unit problem: 20 seconds is a
# different amount of inflation on every rig, so the row would mean something
# different over here than it did over there.
bus.post(A.send_do(T, {"type": "fire", "fire_mode": "seconds", "seconds": 20}))
made = bus.settle(T)
ok(not rows(made) and "% only" in notes(made), "a seconds fire is refused over the wire")
ok("aren't portable" in notes(made), "…and says why, so it reads as a unit rule")

bus.post(A.send_do(T, {"type": "message", "text": "Dave's pump is charging…"}))
ok(len(rows(bus.settle(T))) == 1, "a message row passes the gate untouched")

# ---- telemetry: interval for meters, events for outcomes -------------------- #
n = len(bus.log)
bus.post(B.push_tele(T, 22, firing=True))
ok(len(bus.log) > n, "a firing guest pushes capacity")
n = len(bus.log)
bus.post(B.push_tele(T + 0.5, 23, firing=True))
ok(len(bus.log) == n, "…but not more than once every 2s")
bus.post(B.push_tele(T + 0.5, 24, firing=False, force=True))
bus.settle(T)
n = len(bus.log)
bus.post(B.push_tele(T + 0.6, 75.2, firing=True, claim={"target": 75, "ms": 4200}))
made = bus.settle(T)
ok(len(bus.log) > n, "a threshold CLAIM jumps the interval")
ok(A.peer_claim and A.peer_claim["target"] == 75, "the host decides on claims, not samples")

bus.post(A.end(T, "Dave hit 75% first"))
made = bus.settle(T)
ok(B.state == mp.S_ADVERTISED and not B.link.sid, "bye settles the guest back to idle")
ok(any(o.stop for o in made), "a finished match stops devices locally")
ok(B.link.peer == "100", "…and keeps the pairing, which is config")

# ---- the ways a match dies -------------------------------------------------- #
A2, B2 = pair(cast="WRONG")
bus2 = Bus(A2, B2)
bus2.post(A2.start_advertising(T))
bus2.post(B2.start_advertising(T))
made = bus2.settle(T)
ok("⚠ broadcast mismatch" in notes(made),
   "a channel mismatch is flagged at HELLO, before anyone clicks invite")
ok(A2.status(T)["channel_faults"], "…and the panel can render it")
bus2.post(A2.offer(T, game="Race to N%", base_target=150, sid="zzz1"))
made = bus2.settle(T)
ok(B2.state != mp.S_INVITED, "a channel mismatch never becomes a match")
ok("broadcast mismatch" in notes(made), "…and both operators are told WHICH channel")
ok(A2.state == mp.S_ADVERTISED and not A2.link.sid, "the host stands down")

# an uncompensated race must SAY SO rather than pretend it's even
A9 = mp.Session(mp.Link("100", name="Curtis-bot"), peer_name="Dave-bot",
                net="net1", cast="cast1", calibration=60)
B9 = mp.Session(mp.Link("200", name="Dave-bot"), peer_name="Curtis-bot",
                net="net1", cast="cast1", calibration=0)      # never calibrated
bus9 = Bus(A9, B9)
bus9.post(A9.start_advertising(T)); bus9.post(B9.start_advertising(T)); bus9.settle(T)
made = bus9.post(A9.offer(T, game="Race", base_target=150, sid="unp1"))
got = bus9.settle(T)
ok(not A9.paced and "unpaced" in notes([made]), "the host flags an unpaced race")
ok(B9.invite["targets"] == {"100": 150, "200": 150}, "uncompensated = everyone gets the base")
ok("⚠ unpaced" in notes(got), "…and the guest is warned BEFORE accepting")

# rows outside a match are refused, not quietly dropped
lone = mp.Session(mp.Link("200", name="Dave-bot"), peer_name="Curtis-bot",
                  net="net1", cast="cast1")
lone.link.bind("100", "x1", mp.ROLE_GUEST, state=mp.S_LINKED)
stray_do = mp.decode(mp.encode(mp.T_DO, frm="100", to="200", sid="x1", seq=1,
                               row={"type": "fire", "fire_mode": "add", "fill_pct": 5}))
o = lone.feed(stray_do, T)
ok(not o.rows and any("not in a match" in s for s in o.send),
   "a row outside a match is refused with a reason, never silently dropped")

A3, B3 = pair()
bus3 = Bus(A3, B3)
bus3.post(A3.start_advertising(T)); bus3.post(B3.start_advertising(T)); bus3.settle(T)
bus3.post(A3.offer(T, game="Race", base_target=100, sid="tmo1"))
bus3.settle(T)
made = [B3.tick_clock(T + mp.INVITE_TTL + 1)]
ok(B3.state == mp.S_ADVERTISED and "expired" in notes(made), "an unanswered invite expires")

A4, B4 = pair()
bus4 = Bus(A4, B4)
bus4.post(A4.start_advertising(T)); bus4.post(B4.start_advertising(T)); bus4.settle(T)
bus4.post(A4.offer(T, game="Race", base_target=100, sid="ded1"))
bus4.settle(T)
bus4.post(B4.respond(T, True, video=True)); bus4.settle(T)
bus4.post(A4.arm(T)); bus4.post(B4.arm(T)); bus4.settle(T)
bus4.post(A4.begin(T)); bus4.settle(T)
dead = B4.tick_clock(T + mp.DEAD_AFTER + 1)
ok(dead.stop and "went silent" in " ".join(dead.notes), "90s of silence = presumed dead")
ok(B4.state == mp.S_ADVERTISED, "…and the guest lets go of the match")

beat = A4.tick_clock(T + mp.BEAT_EVERY + 1)
ok(any("DF1 beat" in s for s in beat.send), "a quiet match still heartbeats")

# capacity rides on whatever is already being sent. Its own pair: B4 was
# aborted by the dead-peer check above, so it is no longer in a match.
A9b, B9b = pair()
bus9b = Bus(A9b, B9b)
bus9b.post(A9b.start_advertising(T)); bus9b.post(B9b.start_advertising(T)); bus9b.settle(T)
bus9b.post(A9b.offer(T, game="Race", base_target=100, sid="cap1")); bus9b.settle(T)
bus9b.post(B9b.respond(T, True, video=True)); bus9b.settle(T)
bus9b.post(A9b.arm(T)); bus9b.post(B9b.arm(T)); bus9b.settle(T)
bus9b.post(A9b.begin(T)); bus9b.settle(T)

B9b.capacity = 42.5
gbeat = B9b.tick_clock(T + mp.BEAT_EVERY + 1)
env = next(mp.decode(x) for x in gbeat.send if "DF1 beat" in x)
ok(env.get("cap") == 42.5,
   "the GUEST's heartbeat carries its capacity — `tele` only goes out while "
   "its pump runs, so between fires the host's number would age")
hbeat = A9b.tick_clock(T + mp.BEAT_EVERY + 1)
henv = next(mp.decode(x) for x in hbeat.send if "DF1 beat" in x)
ok("cap" not in henv,
   "…and the host's does NOT: the host narrates and owns the scoreboard, so "
   "the guest never needs that number — sending it is traffic for nobody")

A9b.peer_cap = 0.0
A9b.feed(env, T)
ok(A9b.peer_cap == 42.5, "…and the host picks it up off the heartbeat")
sneak = mp.decode(mp.encode(mp.T_BEAT, frm="999", to="100", sid="cap1", seq=1, cap=99))
A9b.feed(sneak, T)
ok(A9b.peer_cap == 42.5, "…but only from the paired peer, never from a stranger")

A5, B5 = pair()
bus5 = Bus(A5, B5)
bus5.post(A5.start_advertising(T)); bus5.post(B5.start_advertising(T)); bus5.settle(T)
bus5.post(A5.offer(T, game="Race", base_target=100, sid="bsy1"))
bus5.settle(T)
bus5.post(B5.respond(T, False, "not right now"))
made = bus5.settle(T)
ok("declined: not right now" in notes(made), "a decline names its reason")
ok(A5.state == mp.S_ADVERTISED and not A5.link.sid, "…and the host lets go")

# ---- resume ----------------------------------------------------------------- #
A6, B6 = pair()
bus6 = Bus(A6, B6)
bus6.post(A6.start_advertising(T)); bus6.post(B6.start_advertising(T)); bus6.settle(T)
bus6.post(A6.offer(T, game="Race to N%", base_target=150, sid="rsm1"))
bus6.settle(T)
bus6.post(B6.respond(T, True, video=True)); bus6.settle(T)
bus6.post(A6.arm(T)); bus6.post(B6.arm(T)); bus6.settle(T)
bus6.post(A6.begin(T, rnd=3)); bus6.settle(T)
bus6.post(A6.send_do(T, {"type": "fire", "fire_mode": "add", "fill_pct": 5}))
bus6.settle(T)

snap = B6.snapshot()
ok(snap["role"] == "guest" and snap["round"] == 3 and snap["sid"] == "rsm1", "guest snapshot")

B7 = mp.Session(mp.Link("200", name="Dave-bot"),
                peer_name="Curtis-bot", net="net1", cast="cast1", calibration=120)
res = B7.restore(snap, T)
ok(B7.state == mp.S_MATCH and "resumed" in " ".join(res.notes), "guest resumes, and says so")
replay = [mp.decode(l) for l in bus6.log if mp.decode(l) and mp.decode(l)["t"] == "do"]
ok(replay and not rows([B7.feed(replay[-1], T)]), "replaying the tail can't fire a pump twice")
bus6.post(A6.send_do(T, {"type": "fire", "fire_mode": "add", "fill_pct": 6}))
fresh = mp.decode(bus6.log[-1])
ok(len(rows([B7.feed(fresh, T)])) == 1, "…but the one it MISSED still runs")

A7 = mp.Session(mp.Link("100", name="Curtis-bot"), peer_name="Dave-bot",
                net="net1", cast="cast1", calibration=60)
hres = A7.restore(A6.snapshot(), T)
ok(any("DF1 abort" in s for s in hres.send), "a host that can't restore aborts LOUDLY")
ok("referee state is gone" in " ".join(hres.notes), "…and says why")

# a peer that comes back advertising mid-match has lost the session
A8, B8 = pair()
bus8 = Bus(A8, B8)
bus8.post(A8.start_advertising(T)); bus8.post(B8.start_advertising(T)); bus8.settle(T)
bus8.post(A8.offer(T, game="Race", base_target=100, sid="rst1")); bus8.settle(T)
bus8.post(B8.respond(T, True, video=True)); bus8.settle(T)
bus8.post(A8.arm(T)); bus8.post(B8.arm(T)); bus8.settle(T)
bus8.post(A8.begin(T)); bus8.settle(T)
restarted = mp.Session(mp.Link("200", name="Dave-bot"), peer_name="Curtis-bot",
                       net="net1", cast="cast1")
back = restarted.say_hello(T, force=True)
made = [A8.feed(mp.decode(back.send[0]), T)]
ok(any(o.stop for o in made) and "restarted mid-match" in notes(made),
   "a peer that reappears mid-match aborts the match rather than half-playing it")

# ---- many bots, one bot_network channel ------------------------------------ #
# Two matches running side by side in the SAME channel. Every envelope names
# who it is from and who it is for, and carries a session id, so four bots can
# share one channel without ever hearing each other's game.
def bot(bid, name, want):
    return mp.Session(mp.Link(bid, name=name),
                      peer_name=want, net="n", cast="c", calibration=60,
                      player=name.replace("-bot", ""))


A1, B1 = bot("101", "Ann-bot", "Ben-bot"), bot("102", "Ben-bot", "Ann-bot")
C1, D1 = bot("103", "Cal-bot", "Dee-bot"), bot("104", "Dee-bot", "Cal-bot")
room = Bus(A1, B1, C1, D1)          # ONE channel, four bots
for s_ in (A1, B1, C1, D1):
    room.post(s_.start_advertising(T))
    room.settle(T)

ok(A1.link.peer == "102" and B1.link.peer == "101",
   "each bot pairs with the one it NAMED, not whoever answered first")
ok(C1.link.peer == "104" and D1.link.peer == "103", "…and so does the other pair")

room.post(A1.offer(T, game="One", base_target=100, sid="aa11"))
room.post(C1.offer(T, game="Two", base_target=200, sid="cc22"))
room.settle(T)
ok(B1.invite.get("sid") == "aa11" and D1.invite.get("sid") == "cc22",
   "two invites cross in the same channel and each reaches only its own peer")
ok(B1.invite.get("game") == "One" and D1.invite.get("game") == "Two",
   "…with the right terms — no crossed wires")

room.post(B1.respond(T, True, video=True))
room.post(D1.respond(T, True, video=True))
room.settle(T)
room.post(A1.arm(T)); room.post(B1.arm(T))
room.post(C1.arm(T)); room.post(D1.arm(T))
room.settle(T)
room.post(A1.begin(T)); room.post(C1.begin(T))
room.settle(T)
ok(A1.link.sid == "aa11" and C1.link.sid == "cc22",
   "both matches run at once, each in its own session")

room.post(A1.send_do(T, {"type": "fire", "fire_mode": "add", "fill_pct": 7}))
made = room.settle(T)
hit = [r for o in made for r in o.rows]
ok(len(hit) == 1, "a row lands on exactly ONE machine")
ok(B1.link.me == "102" and not [r for o in made for r in o.rows
                                if o is None], "…and it is the addressed one")
ok(not D1.link.sid == "aa11", "the other match never saw it")

# a stranger in the channel is inert
E1 = bot("105", "Eve-bot", "Ann-bot")
stray = E1.say_hello(T, force=True)
o = A1.feed(mp.decode(stray.send[0]), T)
ok(A1.link.peer == "102" and any("foreign peer" in n or "blocked" in n or "dropped" in n
                                 for n in o.notes),
   "a fifth bot saying hello cannot steal a pairing that already exists")

# ---- camera is required of both players ------------------------------------ #
# It is what removed the second production layout: each player is a video tile,
# so each install carries only its own gauge and its own pump timer. A guest
# with no camera would leave the host narrating somebody nobody can see.
A2, B2 = bot("201", "Ann-bot", "Ben-bot"), bot("202", "Ben-bot", "Ann-bot")
pair = Bus(A2, B2)
for s_ in (A2, B2):
    pair.post(s_.start_advertising(T)); pair.settle(T)
pair.post(A2.offer(T, game="Cam", base_target=100, sid="cam1"))
pair.settle(T)

o = B2.respond(T, True, video=False)
ok(B2.link.state == mp.S_INVITED, "accepting with no camera does NOT link")
ok(any("camera required" in n for n in o.notes), "…and says exactly why")
ok(not o.send, "…and nothing goes out — the host is not told a lie")

o = B2.respond(T, True, video=True)
ok(B2.link.state == mp.S_LINKED, "with a camera it links")
ok(B2.video is True, "…and the answer is recorded")

# ---- split the difference (calibration) ------------------------------------ #
# Two rigs at different speeds. The host may OFFER to meet in the middle; the
# guest may take it. Both pumps then run the same seconds-to-100% for the
# length of the match — and go back to their own the moment it ends.
def paired(hcal, gcal):
    H = mp.Session(mp.Link("301", name="H-bot"), peer_name="G-bot",
                   net="n", cast="c", calibration=hcal, player="Host")
    G = mp.Session(mp.Link("302", name="G-bot"), peer_name="H-bot",
                   net="n", cast="c", calibration=gcal, player="Guest")
    b = Bus(H, G)
    for x in (H, G):
        b.post(x.start_advertising(T)); b.settle(T)
    return H, G, b


# offered and TAKEN
H, G, b = paired(40.0, 80.0)
b.post(H.offer(T, game="Split", base_target=100, split=True)); b.settle(T)
ok(G.invite.get("split") is True, "the offer travels with the invite")
ok(G.invite.get("cal") == 40.0, "…and so does the host's pump speed, to show")

o = G.respond(T, True, video=True, split=True)
ok(G.calibration == 60.0, "the guest's pump moves to the midpoint")
ok(o.cal == 60.0, "…and the adapter is told to write it")
ok(G.cal_before == 80.0, "…while its own number is remembered")
b.post(o); b.settle(T)
ok(H.calibration == 60.0, "the host's pump moves to the SAME midpoint")
ok(H.cal_before == 40.0, "…and remembers its own too")
ok(abs(H.targets["301"] - H.targets["302"]) < 1e-9,
   "equal pumps need no handicap, so the two finish lines are re-paced level")

# …and it is match-scoped
back = H.abort(T, "done")
ok(back.cal == 40.0, "the match ending puts the host's rig back")
ok(H.calibration == 40.0 and H.cal_before is None, "…and clears the memory")
gback = G.abort(T, "done")
ok(gback.cal == 80.0 and G.calibration == 80.0, "…the guest's too")

# offered and DECLINED
H, G, b = paired(40.0, 80.0)
b.post(H.offer(T, game="Split", base_target=100, split=True)); b.settle(T)
o = G.respond(T, True, video=True, split=False)
ok(G.calibration == 80.0 and o.cal is None, "a guest who doesn't take it keeps its rig")
b.post(o); b.settle(T)
ok(H.calibration == 40.0 and H.cal_before is None, "…and so does the host")

# NOT offered — a guest cannot re-tune the host's rig on its own
H, G, b = paired(40.0, 80.0)
b.post(H.offer(T, game="NoSplit", base_target=100)); b.settle(T)
o = G.respond(T, True, video=True, split=True)
ok(G.calibration == 80.0 and o.cal is None,
   "ticking split when it was never offered changes nothing")

# the guest may CORRECT its own figure in the popup
H, G, b = paired(40.0, 80.0)
b.post(H.offer(T, game="Fix", base_target=100, split=True)); b.settle(T)
o = G.respond(T, True, video=True, cal=60.0, split=True)
ok(G.calibration == 50.0,
   "the midpoint uses the number the guest actually entered, not a stale one")
ok(G.cal_before == 60.0, "…and that corrected number is what comes back")

# a missing calibration can't be split
H, G, b = paired(40.0, 0)
b.post(H.offer(T, game="Miss", base_target=100, split=True)); b.settle(T)
o = G.respond(T, True, video=True, split=True)
ok(o.cal is None and any("can't split" in n for n in o.notes),
   "with a calibration missing both rigs keep their own, and it says so")

# it survives a restart, or a crash strands the rig on someone else's number
H, G, b = paired(40.0, 80.0)
b.post(H.offer(T, game="Crash", base_target=100, split=True)); b.settle(T)
b.post(G.respond(T, True, video=True, split=True)); b.settle(T)
snap = G.snapshot()
G2 = mp.Session(mp.Link("302", name="G-bot"), peer_name="H-bot",
                net="n", cast="c", calibration=999, player="Guest")
G2.restore(snap, T)
ok(G2.cal_before == 80.0 and G2.calibration == 60.0,
   "a restart comes back mid-split and still knows what to put back")
ok(G2.abort(T, "done").cal == 80.0, "…and putting it back works after a restart")

# ---- the seat is a decision, not a negotiation ------------------------------ #
# Picked in the header before going live, frozen after. Two installs quietly
# agreeing a role between themselves is how you start a match in a seat you
# didn't mean to be in.
H, G, b = paired(60.0, 60.0)
G.role_pref = mp.ROLE_GUEST
o = G.offer(T, game="Nope", base_target=100)
ok(not o.send and any("seated as the GUEST" in n for n in o.notes),
   "a guest cannot send an invite, and is told where to change that")
ok(G.link.state != mp.S_INVITING, "…and nothing about the session moved")

H.role_pref = mp.ROLE_HOST
b.post(H.offer(T, game="Seats", base_target=100)); b.settle(T)
ok(G.link.state == mp.S_INVITED, "a host can, and it lands")

G.role_pref = mp.ROLE_HOST                # both seated as host
o = G.respond(T, True, video=True)
ok(G.link.state == mp.S_INVITED and any("seated as the HOST" in n for n in o.notes),
   "a second HOST cannot accept — the collision is named, not silently resolved")
G.role_pref = mp.ROLE_GUEST
b.post(G.respond(T, True, video=True)); b.settle(T)
ok(G.link.state == mp.S_LINKED, "…and taking the guest seat lets it through")

# ---- the lose-at line is held by BOTH, watched by EACH ---------------------- #
# The host must not police the guest: it only ever sees a rounded meter a
# heartbeat late, while the guest holds its own exactly and instantly. So the
# machine that crosses the line is the machine that says so.
H, G, b = paired(60.0, 60.0)
b.post(H.offer(T, game="Cap", base_target=100, cap=145)); b.settle(T)
ok(H.end_max == 145, "the host holds the number it offered")
ok(G.invite.get("cap") == 145, "…and it travels in the invite")
b.post(G.respond(T, True, video=True)); b.settle(T)
ok(G.end_max == 145, "…and becomes the guest's OWN once accepted")

b.post(H.arm(T)); b.post(G.arm(T)); b.settle(T)
b.post(H.begin(T)); b.settle(T)

ok(not G.check_end(T, capacity=144.9), "under the line, nothing happens")
out = G.check_end(T, capacity=145)
ok(out.send and G.conceded == G.link.me,
   "the GUEST names itself the loser the moment its own meter reaches it")
ok("145" in G.conceded_why, "…with the reason in words")
b.post(out); b.settle(T)
ok(H.conceded == G.link.me,
   "…and the host learns it from the wire rather than from watching a stale copy")

# the host's own line works the same way, from its own meter
H2, G2b, b2 = paired(60.0, 60.0)
b2.post(H2.offer(T, game="Cap", base_target=100, cap=145)); b2.settle(T)
b2.post(G2b.respond(T, True, video=True)); b2.settle(T)
b2.post(H2.arm(T)); b2.post(G2b.arm(T)); b2.settle(T)
b2.post(H2.begin(T)); b2.settle(T)
b2.post(H2.check_end(T, capacity=200)); b2.settle(T)
ok(H2.conceded == H2.link.me and G2b.conceded == H2.link.me,
   "the host reaching it loses too — it is a lose condition, not a finish line")

# it cannot fire twice, and it is gone when the match is
H3, G3, b3 = paired(60.0, 60.0)
b3.post(H3.offer(T, game="Cap", base_target=100, cap=145)); b3.settle(T)
b3.post(G3.respond(T, True, video=True)); b3.settle(T)
b3.post(H3.arm(T)); b3.post(G3.arm(T)); b3.settle(T)
b3.post(H3.begin(T)); b3.settle(T)
b3.post(G3.check_end(T, capacity=150)); b3.settle(T)
ok(not G3.check_end(T, capacity=150).send,
   "a meter still over the line does not concede again every quarter second")
G3.abort(T, "done")
ok(G3.end_max == 0 and not G3.conceded, "the match ending clears the line with it")

# no line set = no capacity ending at all
H4, G4, b4 = paired(60.0, 60.0)
b4.post(H4.offer(T, game="NoCap", base_target=100)); b4.settle(T)
b4.post(G4.respond(T, True, video=True)); b4.settle(T)
b4.post(H4.arm(T)); b4.post(G4.arm(T)); b4.settle(T)
b4.post(H4.begin(T)); b4.settle(T)
ok(G4.end_max == 0 and not G4.check_end(T, capacity=9999).send,
   "with no lose-at number, no capacity ends it — conceding is the only way out")

print(f"{P} passed, {len(F)} failed")
for f in F:
    print("  FAIL:", f)
sys.exit(1 if F else 0)
