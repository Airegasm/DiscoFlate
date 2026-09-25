"""DiscoFlate — multiplayer protocol core.

The wire between two installs: envelope encode/decode, sequencing, dedup, the
link state machine.

This module deliberately imports NOTHING from discord or engine. Everything in
here is pure, so the whole rail can be verified headlessly instead of needing
two live bots in two houses — which is the only practical way to test a
protocol whose normal test rig is a second person.

See MULTIPLAYER.md for the design and the reasoning behind each rule.
"""

import json
import random

PROTO = "DF1"

# Discord's content cap is 2000. Leave room so a long reason string can never
# be the thing that silently truncates an envelope into unparseable JSON.
MAX_ENVELOPE = 1900

# ---- the twelve types ------------------------------------------------------ #
# handshake (pre-session; `hello` carries no sid because there isn't one yet)
T_HELLO = "hello"
T_INVITE = "invite"
T_ACCEPT = "accept"
T_DECLINE = "decline"
T_READY = "ready"
# match
T_STATE = "state"
T_DO = "do"
T_ACK = "ack"
T_TELE = "tele"
# lifecycle
T_ABORT = "abort"
T_BYE = "bye"
T_BEAT = "beat"
# Giving up — the ONLY natural way a match ends. Three things send it: the
# Concede button, !concede in the venue, and reaching the lose-at capacity
# (getting there first IS conceding). Going off air sends it too, because
# walking away is conceding by other means.
T_CONCEDE = "concede"

TYPES = frozenset({T_HELLO, T_INVITE, T_ACCEPT, T_DECLINE, T_READY,
                   T_STATE, T_DO, T_ACK, T_TELE,
                   T_ABORT, T_BYE, T_BEAT, T_CONCEDE})

# `hello` and `beat` are pure identity/liveness: reprocessing one is harmless,
# and they're the two that legitimately repeat with a RESET counter after the
# sender restarts. Dedupping them would reject a peer that just came back.
NO_DEDUP = frozenset({T_HELLO, T_BEAT})

# ---- link states ----------------------------------------------------------- #
S_IDLE = "idle"                # nothing going on
S_ADVERTISED = "advertised"    # hello posted, waiting to hear one back
S_INVITING = "inviting"        # we're host; invite sent
S_INVITED = "invited"          # we're guest; invite received, undecided
S_LINKED = "linked"            # accepted both ways
S_READY = "ready"              # both armed
S_MATCH = "match"              # rounds running
S_SETTLING = "settling"        # outcome blocks running
S_DONE = "done"                # finished; awaiting reset to idle

ROLE_HOST = "host"
ROLE_GUEST = "guest"


class ProtocolError(ValueError):
    """An envelope we refused to build. Receiving is never an exception —
    a bad envelope from the wire is dropped, not raised."""


def encode(t: str, *, frm: str, to: str = "", sid: str = "",
           seq: int = 0, **body) -> str:
    """Build one envelope line. Raises ProtocolError rather than emitting
    something the peer can't parse — a send that fails loudly is the whole
    point of not reusing the bot's swallow-everything _send()."""
    if t not in TYPES:
        raise ProtocolError(f"unknown envelope type: {t!r}")
    if not str(frm or "").strip():
        raise ProtocolError("envelope needs a 'from'")
    head = {"sid": str(sid or ""), "seq": int(seq),
            "from": str(frm), "to": str(to or "")}
    # body last so a game payload can never overwrite the routing keys
    payload = {**body, **head}
    line = f"{PROTO} {t} {json.dumps(payload, separators=(',', ':'))}"
    if len(line) > MAX_ENVELOPE:
        raise ProtocolError(
            f"envelope too large ({len(line)} > {MAX_ENVELOPE}) — keep boards, "
            f"images and per-tick state off the wire")
    return line


def decode(content) -> dict | None:
    """Parse one message's content into an envelope, or None if it isn't one.

    Returns None (never raises) for anything that isn't ours, because this runs
    on EVERY message in the bot_network channel. The cheap prefix test comes
    first so the common case costs one string compare.
    """
    text = str(content or "").strip()
    if not text.startswith(PROTO + " "):
        return None
    parts = text.split(" ", 2)
    if len(parts) != 3:
        return None
    _, t, raw = parts
    if t not in TYPES:
        return None
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    frm = str(body.get("from") or "").strip()
    if not frm:
        return None
    try:
        seq = int(body.get("seq") or 0)
    except (TypeError, ValueError):
        return None
    env = dict(body)
    env["t"] = t
    env["from"] = frm
    env["to"] = str(body.get("to") or "")
    env["sid"] = str(body.get("sid") or "")
    env["seq"] = seq
    return env


class Link:
    """One install's view of the link: who we are, who the peer is, what
    session we're in, and what we've already applied.

    Dedup is the whole reliability story. Because the bot_network channel is a
    LOG rather than a socket, a reconnecting install re-reads the tail and will
    legitimately see envelopes it has already applied. Keying on
    (from, sid, seq) makes every envelope idempotent, which is what lets a
    replayed `do` not fire a pump twice.
    """

    def __init__(self, me: str, *, owner: str = "", version: str = "",
                 install: str = "", name: str = "") -> None:
        self.me = str(me)              # my bot user id
        self.owner = str(owner)        # my owner's discord user id
        self.version = str(version)
        self.install = str(install)
        self.name = str(name)

        self.peer = ""                 # peer BOT id, resolved from hello
        self.peer_name = ""            # the name the user configured / saw
        self.peer_owner = ""
        self.peer_version = ""

        self.sid = ""
        self.role = ""
        self.state = S_IDLE

        self._seq = 0                  # my outgoing counter
        self._seen: dict = {}          # (from, sid) -> highest seq applied

    # -- outgoing ----------------------------------------------------------- #
    @property
    def seq(self) -> int:
        """The last sequence number we spent — what an `ack` refers back to."""
        return self._seq

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def out(self, t: str, **body) -> str:
        """Encode an envelope addressed to the current peer in the current
        session, consuming a sequence number."""
        return encode(t, frm=self.me, to=self.peer, sid=self.sid,
                      seq=self.next_seq(), **body)

    # -- incoming ----------------------------------------------------------- #
    def accept(self, env: dict) -> tuple[bool, str]:
        """Should this envelope be applied? Returns (ok, why-not).

        Every rejection reason is named, because "the match silently did
        nothing" is the failure mode this protocol exists to avoid.
        """
        if not env or env.get("t") not in TYPES:
            return False, "not an envelope"
        t = env["t"]
        frm = env.get("from") or ""
        if frm == self.me:
            return False, "self"                     # our own post, echoed back
        to = env.get("to") or ""
        if to and to != self.me:
            return False, "addressed elsewhere"
        # Before pairing, anyone may say hello; after it, only our peer exists.
        if self.peer and frm != self.peer:
            return False, "foreign peer"
        if t not in (T_HELLO, T_INVITE):
            # every session-scoped type must name OUR session
            if not self.sid or env.get("sid") != self.sid:
                return False, "wrong session"
        if t in NO_DEDUP:
            return True, ""
        key = (frm, env.get("sid") or "")
        last = self._seen.get(key)
        seq = int(env.get("seq") or 0)
        if last is not None and seq <= last:
            return False, "duplicate"
        if len(self._seen) > 32 and key not in self._seen:
            # An `invite` is the one type a stranger can get this far with (it
            # is exempt from the session check by design — it's what CREATES a
            # session). Bound the map so a spammer can't grow it forever.
            self._seen.pop(next(iter(self._seen)), None)
        self._seen[key] = seq
        return True, ""

    def mark_seen(self, frm: str, sid: str, seq: int) -> None:
        """Record a sequence number without applying it — used when restoring
        `last_seq` from disk on resume, so the tail replay skips what we
        already did before the process died."""
        key = (str(frm), str(sid or ""))
        cur = self._seen.get(key)
        seq = int(seq)
        if cur is None or seq > cur:
            self._seen[key] = seq

    def last_seq(self, frm: str = "") -> int:
        """Highest sequence applied from a sender in the current session."""
        return int(self._seen.get((str(frm or self.peer), self.sid), 0))

    # -- session ------------------------------------------------------------ #
    def bind(self, peer: str, sid: str, role: str, state: str = S_LINKED) -> None:
        self.peer = str(peer)
        self.sid = str(sid)
        self.role = str(role)
        self.state = str(state)

    def reset(self) -> None:
        """Back to idle, keeping the resolved peer identity (that's config, not
        session state) but dropping everything about the match."""
        self.sid = ""
        self.role = ""
        self.state = S_IDLE
        self._seq = 0
        self._seen.clear()

    @property
    def is_host(self) -> bool:
        return self.role == ROLE_HOST

    def snapshot(self) -> dict:
        """What gets persisted to data/match.json so a restart can resume."""
        return {"sid": self.sid, "role": self.role, "state": self.state,
                "peer": self.peer, "peer_name": self.peer_name,
                "seq": self._seq, "last_seq": self.last_seq()}

    def restore(self, snap: dict) -> None:
        snap = snap or {}
        self.sid = str(snap.get("sid") or "")
        self.role = str(snap.get("role") or "")
        self.state = str(snap.get("state") or S_IDLE)
        self.peer = str(snap.get("peer") or self.peer)
        self.peer_name = str(snap.get("peer_name") or self.peer_name)
        try:
            self._seq = int(snap.get("seq") or 0)
        except (TypeError, ValueError):
            self._seq = 0
        if self.peer and self.sid:
            self.mark_seen(self.peer, self.sid, snap.get("last_seq") or 0)


# ---- one finish line, not two ---------------------------------------------- #
# Percent is the whole point: +10% is +10% on any rig, so a 900s pump and a
# 300s pump take the SAME number of percentage points to reach the same line.
# Damage here is dealt by game outcomes measured in percent, never by how fast
# a motor runs, so pump speed cannot decide the match and there is nothing to
# compensate for. Calibration still crosses in the invite — but only so each
# side can SHOW what a hit will cost in its own seconds, and so the two can
# agree to meet in the middle if they want the pumping to FEEL the same.
# (A rate-scaled "pace compensation" lived here until v4.0.2. It handed the
#  slower rig a lower finish line, which is exactly the rig-power advantage
#  percent exists to abolish. Do not bring it back.)


# ---- the lifecycle --------------------------------------------------------- #
#
# Every non-idle state has a timeout, because the failure this protocol exists
# to avoid is not "the wrong thing happened" — it is "nothing happened and
# nobody said why". A state with no exit is a match that hangs forever with two
# people staring at a panel.

HELLO_EVERY = 20.0      # re-post hello while hunting for the peer
PEER_STALE = 90.0       # heard nothing this long → peer is not online
INVITE_TTL = 120.0      # an invite nobody answers
LINK_TTL = 300.0        # linked but never armed
BEAT_EVERY = 25.0       # heartbeat while a match is otherwise quiet
DEAD_AFTER = 90.0       # silence inside a match → presumed dead → safe stop
TELE_EVERY = 2.0        # guest capacity push, ONLY while its pump is firing
DEAD_HEAT_MS = 500.0    # two finish claims this close are a draw, not a race

_SID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"   # no l/o/0/1 — these get read aloud

# States where a match is live enough that losing the peer must stop a pump.
LIVE = frozenset({S_LINKED, S_READY, S_MATCH, S_SETTLING})


def new_sid() -> str:
    """A short match id. Four characters is plenty — it only has to be unlike
    LAST night's match, not globally unique, and it rides in every envelope."""
    return "".join(random.choice(_SID_ALPHABET) for _ in range(4))


def decide_role(me: str, peer: str, my_pref: str = "either",
                peer_pref: str = "either") -> str:
    """Who referees, computed identically on both installs so they can never
    both believe they're the host. Stated preferences win when they agree; when
    they collide the lower bot id hosts — arbitrary, but deterministic, which
    is the only property that actually matters here."""
    mine = my_pref if my_pref in (ROLE_HOST, ROLE_GUEST) else ""
    theirs = peer_pref if peer_pref in (ROLE_HOST, ROLE_GUEST) else ""
    if mine and not theirs:
        return mine
    if theirs and not mine:
        return ROLE_GUEST if theirs == ROLE_HOST else ROLE_HOST
    if mine and theirs and mine != theirs:
        return mine
    return ROLE_HOST if str(me) < str(peer) else ROLE_GUEST


def check_channels(mine: dict, theirs: dict) -> list:
    """Preflights 3 and 4 — the two that need the peer's answer. Returns named
    failures; empty means agreed.

    Never a bare bool. Two bots pointed at different channels is the single
    most likely way a match dies, and the message has to say WHICH channel and
    WHOSE, or the operator is left guessing at a silent dead end.
    """
    out = []
    for key, label in (("net", "bot_network"), ("cast", "broadcast")):
        a = str((mine or {}).get(key) or "").strip()
        b = str((theirs or {}).get(key) or "").strip()
        if not a:
            out.append(f"no {label} channel set here")
        elif not b:
            out.append(f"peer has no {label} channel set")
        elif a != b:
            out.append(f"{label} mismatch — you: {a}, peer: {b}")
    return out


def dead_heat(a_ms, b_ms, window: float = DEAD_HEAT_MS) -> bool:
    """Two finish claims inside the window are a draw. A constant rather than a
    policy, which is what lets every tie-break rule be deleted."""
    a, b = _num(a_ms), _num(b_ms)
    if a is None or b is None:
        return False
    return abs(a - b) <= window



class Out:
    """What the adapter must actually do. The protocol decides; the caller
    performs. Keeping those apart is the whole reason a rail whose natural test
    rig is "a second person in another house" can be proven on one machine.
    """

    __slots__ = ("send", "rows", "notes", "stop", "dirty", "seq", "say", "board",
                 "cal")

    def __init__(self) -> None:
        self.send = []      # envelope lines → bot_network, in order
        # [{"re": seq, "row": row}] → run locally. `re` is the `do` sequence an
        # ack must quote; **re 0 means the row is our own**, not the peer's, so
        # there is nobody to ack to.
        self.rows = []
        self.notes = []     # human lines → panel + log
        self.say = []       # lines to post in the BROADCAST channel, in my voice
        self.board = None   # scoreboard text — edited in place, never reposted
        self.stop = False   # local safe stop (stop_devices) required
        self.dirty = False  # session changed → persist data/match.json
        self.seq = 0        # seq of the last envelope built (correlate acks)
        # A new seconds-to-100% for THIS install's primary pump, agreed by both
        # sides (the split-the-difference offer). None = leave the rig alone.
        self.cal = None

    def __bool__(self) -> bool:
        return bool(self.send or self.rows or self.notes or self.say
                    or self.board is not None or self.stop)

    def merge(self, other: "Out") -> "Out":
        self.send.extend(other.send)
        self.rows.extend(other.rows)
        self.notes.extend(other.notes)
        self.say.extend(other.say)
        if other.cal is not None:
            self.cal = other.cal
        if other.board is not None:
            self.board = other.board
        self.stop = self.stop or other.stop
        self.dirty = self.dirty or other.dirty
        self.seq = other.seq or self.seq
        return self


class Session:
    """The lifecycle on top of `Link`: handshake, match, teardown.

    Nothing in here touches discord, a clock or a device. Time arrives as `now`
    on every call and every side effect leaves as an `Out`. The adapter feeds
    it messages and performs what comes back; that's the entire contract.
    """

    def __init__(self, link: Link, *,
                 peer_name: str = "", net: str = "", cast: str = "",
                 calibration=0, caps=(), role_pref: str = "either",
                 player: str = "") -> None:
        self.link = link
        # The BOT is who the protocol addresses; the PLAYER is who the messages
        # are about. "Dave-bot fired 10%" is plumbing leaking into the show.
        self.player = str(player or "")
        self.peer_player = ""
        self.peer_name = str(peer_name or "")      # the bot we're WAITING for
        self.channels = {"net": str(net or ""), "cast": str(cast or "")}
        self.calibration = calibration             # my primary pump, secs→100%
        self.caps = list(caps)
        self.role_pref = str(role_pref or "either")

        # Bots this operator has blocked. Checked at HELLO, not only at invite:
        # a blocked install should not be able to make the panel light up at all.
        self.blocked = []
        self.advertising = False
        self.capacity = 0.0                        # mine, refreshed by the adapter
        self.peer_cal = 0
        self.split_offer = False   # host offered to meet in the middle
        self.base_target = 0.0     # the shared finish line, in percent
        # What this pump was calibrated at BEFORE a split-the-difference. The
        # split is match-scoped: the rig goes back to its own number when the
        # match ends, however it ends.
        self.cal_before = None
        # Who gave up, and why — set on BOTH installs so either can narrate it.
        self.conceded = ""
        self.conceded_why = ""
        # THE LOSE-AT CAPACITY, agreed at invite time and held by BOTH sides.
        #
        # Each install watches its OWN meter against it, on its own clock. The
        # host does not police the guest: it only ever sees a rounded number a
        # heartbeat late, while the guest knows its own exactly and instantly.
        # The machine that crosses the line is the machine that says so.
        self.end_max = 0.0
        self.peer_caps = []
        self.peer_install = ""
        self.peer_pref = "either"
        # Will each side be on camera? Settled at accept, because it decides
        # what the HOST's stream has to carry.
        self.video = False
        self.peer_video = False
        self.peer_channels = {}                    # what THEIR bot is pointed at
        self.peer_cap = 0.0                        # their capacity, from ack/tele
        self.pump_left = 0.0                       # MY pump's seconds remaining
        self.peer_pump = 0.0                       # theirs, off their heartbeat
        self.peer_claim = None

        self.game = ""
        self.input = "operators"
        self.cost = ""
        self.targets = {}                          # install id → finish line %
                                                   # (always the same number for both)
        self.phase = ""
        self.round = 0
        self.tick = 0
        self.deadline = 0.0                        # MY clock, never theirs
        # When the match went live, on MY clock. Both sides stamp it the moment
        # they learn the match started — not on their next tick — because every
        # crossing is reported as an elapsed time from this instant, and a
        # quarter-second of tick jitter would eat half the dead-heat window.
        self.match_at = 0.0
        # The band the current round ends at. Rounds are bands with an action
        # block, the way a capacity range is — the block loops until somebody
        # reaches this, then the next round takes over.
        self.round_target = 0.0
        self.invite = {}                           # guest: the pending offer
        self.ready_me = False
        self.ready_peer = False
        self.last_why = ""                         # why the last match ended

        self._hello_at = 0.0
        self._seen_at = 0.0                        # last thing heard from peer
        self._sent_at = 0.0                        # last thing we posted
        self._tele_at = 0.0
        self._expires = 0.0                        # deadline for the CURRENT state

    # -- small helpers ------------------------------------------------------- #
    @property
    def state(self) -> str:
        return self.link.state

    @property
    def is_host(self) -> bool:
        return self.link.is_host

    def _emit(self, out: Out, t: str, **body) -> Out:
        """Build one envelope into `out`. A ProtocolError here is a bug in the
        caller's payload, not a wire problem — it becomes a named note rather
        than an exception that kills the bot's message handler."""
        try:
            out.send.append(self.link.out(t, **body))
            out.seq = self.link.seq
        except ProtocolError as e:
            out.notes.append(f"not sent ({t}): {e}")
        return out

    def _mine(self) -> dict:
        return {"net": self.channels.get("net", ""), "cast": self.channels.get("cast", "")}

    def is_blocked(self, bot_id: str) -> bool:
        return str(bot_id or "") in {str(b.get("bot_id") or "")
                                     for b in (self.blocked or []) if isinstance(b, dict)}

    def peer_online(self, now: float) -> bool:
        return bool(self.link.peer) and (now - self._seen_at) < PEER_STALE

    def status(self, now: float = 0.0) -> dict:
        """What the panel renders. One place, so ⚠ / ◌ / ✓ can't drift."""
        return {"state": self.state, "role": self.link.role, "sid": self.link.sid,
                "me": self.link.me,
                "peer": self.link.peer, "peer_name": self.link.peer_name,
                "peer_version": self.link.peer_version,
                "peer_online": self.peer_online(now),
                "player": self.player, "peer_player": self.peer_player,
                "video": self.video, "peer_video": self.peer_video,
                "peer_channels": dict(self.peer_channels),
                "channel_faults": check_channels(self._mine(), self.peer_channels)
                if self.peer_channels else [],
                "game": self.game, "input": self.input, "cost": self.cost,
                "phase": self.phase, "round": self.round,
                "targets": dict(self.targets),
                "capacity": self.capacity, "peer_capacity": self.peer_cap,

                "invite": dict(self.invite), "why": self.last_why}

    # -- handshake ----------------------------------------------------------- #
    def start_advertising(self, now: float) -> Out:
        self.advertising = True
        if self.link.state == S_IDLE:
            self.link.state = S_ADVERTISED
        return self.say_hello(now, force=True)

    def stop_advertising(self) -> None:
        self.advertising = False
        if self.link.state == S_ADVERTISED:
            self.link.state = S_IDLE

    def say_hello(self, now: float, *, force: bool = False, ack: str = "") -> Out:
        out = Out()
        if not force and (now - self._hello_at) < HELLO_EVERY:
            return out
        self._hello_at = now
        self._sent_at = now
        # The channel ids ride along so preflights 3 and 4 can be answered the
        # moment the peer is seen, not at invite time. "Dave's bot is pointed at
        # #general" is a fixable problem when the panel says it up front and a
        # baffling dead end when it surfaces as a declined invite.
        return self._emit(out, T_HELLO, name=self.link.name, owner=self.link.owner,
                          install=self.link.install, ver=self.link.version,
                          caps=self.caps, cal=self.calibration, player=self.player,
                          pref=self.role_pref, ack=str(ack or ""), **self._mine())

    def offer(self, now: float, *, game: str, base_target=0, rounds=0,
              max_pct=0, input: str = "operators", cost: str = "",
              sid: str = "", ttl: float = INVITE_TTL, scene: str = "",
              cap=None, venue=None, split=False) -> Out:
        """Host: invite the peer into a match.

        The cost estimate is built AFTER compensation, because the estimate is
        what the guest is agreeing to — quoting them a pre-compensation number
        and then running a different one is informed consent done wrong.
        """
        out = Out()
        if not self.link.peer:
            out.notes.append("no peer yet — nobody to invite")
            return out
        if self.link.state not in (S_IDLE, S_ADVERTISED):
            out.notes.append(f"can't invite while {self.link.state}")
            return out
        # The SEAT is a decision, not a negotiation. Two installs quietly
        # agreeing a role between themselves is how you start a match in a seat
        # you didn't mean to be in.
        if self.role_pref == ROLE_GUEST:
            out.notes.append("you're seated as the GUEST — switch to Host in "
                             "the header to send invites")
            return out
        missing = [lbl for key, lbl in (("net", "bot_network"), ("cast", "broadcast"))
                   if not self.channels.get(key)]
        if missing:
            out.notes.append("set a " + " and a ".join(missing) + " channel first")
            return out

        self.link.bind(self.link.peer, sid or new_sid(), ROLE_HOST, state=S_INVITING)
        # An OFFER, not a decision: the guest still has to take it, and the
        # number is only settled once they do.
        self.split_offer = bool(split)
        self.base_target = float(_num(base_target) or 0)
        self.end_max = float(_num(cap) or 0)
        self.game = str(game)
        self.input = str(input or "operators")
        self.ready_me = self.ready_peer = False
        self.last_why = ""

        # ONE line, the same number for both. Percent is rig-independent, so
        # the finish line never needs adjusting for whose pump is quicker.
        mine = self.base_target
        self.targets = {self.link.me: mine, self.link.peer: mine} if mine else {}
        self.cost = str(cost or cost_line(game, rounds=rounds, max_pct=max_pct,
                                          target=mine))

        self._expires = now + float(ttl)
        self._sent_at = now
        out.dirty = True
        out.notes.append(f"invited {self.link.peer_name or self.link.peer}: {self.cost}")
        # Everything the guest's popup has to SHOW, in one envelope. Channel
        # NAMES travel beside the ids: an id is unanswerable ("is 901 the right
        # channel?") where "#gameshow in The Den" is a question anyone can
        # settle in a second.
        return self._emit(out, T_INVITE, game=self.game, input=self.input,
                          cost=self.cost, ttl=float(ttl), targets=self.targets,
                          mine=mine, host=self.player, bot=self.link.name,
                          cal=self.calibration, scene=str(scene or ""),
                          split=self.split_offer,
                          # The CAP, not "is over 100 allowed". A yes/no tells
                          # the guest nothing about how far this can go — the
                          # number is what lets them judge the risk and know
                          # what losing looks like before they agree to it.
                          cap=_num(cap) or 0, venue=dict(venue or {}),
                          **self._mine())

    def respond(self, now: float, ok: bool, why: str = "", video=None,
                cal=None, split=False) -> Out:
        """Guest: accept or decline the pending invite.

        `video` is the guest's camera, and it is REQUIRED — accepting without
        it is refused here rather than allowed to fail later.

        A match assumes both players are a video tile. That assumption is what
        lets each install carry only its own gauge and its own pump timer
        instead of drawing a remote one; the peer's numbers still arrive on the
        heartbeat and drive the game's maths. A guest with no camera would
        leave the host narrating a player nobody can see.
        """
        out = Out()
        if self.link.state != S_INVITED or not self.invite:
            out.notes.append("no invite to answer")
            return out
        sid = str(self.invite.get("sid") or "")
        if not ok:
            reason = str(why or "declined")
            self.last_why = reason
            try:
                out.send.append(encode(T_DECLINE, frm=self.link.me, to=self.link.peer,
                                       sid=sid, seq=self.link.next_seq(), why=reason))
            except ProtocolError as e:
                out.notes.append(f"not sent (decline): {e}")
            self.invite = {}
            self.link.reset()
            if self.advertising:
                self.link.state = S_ADVERTISED
            out.notes.append(f"declined: {reason}")
            out.dirty = True
            self._sent_at = now
            return out

        if not video:
            out.notes.append("camera required: a match needs both players on "
                             "camera — not accepted")
            return out
        # The guest may CORRECT their own pump speed in the popup — the figure
        # the panel had could be stale, and every target is computed from it.
        if _num(cal) is not None and float(_num(cal)) > 0:
            self.calibration = float(_num(cal))
        # Split the difference: only if the host OFFERED and the guest TOOK it.
        # One side deciding alone would be one rig silently re-tuning another.
        mid = None
        if split and self.invite.get("split"):
            hc, mc = _num(self.invite.get("cal")), _num(self.calibration)
            if hc and mc and float(hc) > 0 and float(mc) > 0:
                mid = round((float(hc) + float(mc)) / 2.0, 2)
                self.cal_before = float(mc)      # put back when the match ends
                self.calibration = mid
                out.cal = mid
                out.notes.append(
                    f"split the difference: both pumps set to {mid:g}s to 100% "
                    f"(was {float(mc):g} here, {float(hc):g} there)")
            else:
                out.notes.append("can't split the difference — a calibration "
                                 "is missing, so both rigs keep their own")
        if self.role_pref == ROLE_HOST:
            out.notes.append("you're seated as the HOST — switch to Guest in "
                             "the header to accept an invite")
            return out
        self.link.bind(self.link.peer, sid, ROLE_GUEST, state=S_LINKED)
        # The host's number becomes the guest's own, so both watch the same
        # line without having to ask each other where it is.
        self.end_max = float(_num(self.invite.get("cap")) or 0)
        self.game = str(self.invite.get("game") or "")
        self.input = str(self.invite.get("input") or "operators")
        self.cost = str(self.invite.get("cost") or "")
        self.targets = dict(self.invite.get("targets") or {})
        self.ready_me = self.ready_peer = False
        self.invite = {}
        self._expires = now + LINK_TTL
        self._sent_at = now
        out.dirty = True
        out.notes.append(f"accepted: {self.cost}")
        self.video = bool(video)
        return self._emit(out, T_ACCEPT, caps=self.caps,
                          cal=self.calibration, player=self.player,
                          video=self.video, split_cal=mid, **self._mine())

    def arm(self, now: float) -> Out:
        """Either side: I'm ready. Both armed → READY, and the host may begin."""
        out = Out()
        if self.link.state not in (S_LINKED, S_READY):
            out.notes.append(f"can't arm while {self.link.state}")
            return out
        self.ready_me = True
        self._sent_at = now
        self._emit(out, T_READY, armed=True)
        if self.ready_peer:
            self.link.state = S_READY
            self._expires = now + LINK_TTL
            out.notes.append("both armed")
        out.dirty = True
        return out

    # -- the match ----------------------------------------------------------- #
    def begin(self, now: float, *, phase: str = "round", rnd: int = 1,
              left: float = 0.0) -> Out:
        """Host: the match is running."""
        out = Out()
        if not self.is_host:
            out.notes.append("only the host starts the match")
            return out
        if self.link.state not in (S_READY, S_LINKED):
            out.notes.append(f"can't begin while {self.link.state}")
            return out
        self.link.state = S_MATCH
        self.match_at = now
        return out.merge(self.push_state(now, phase=phase, rnd=rnd, left=left))

    def push_state(self, now: float, *, phase: str = "", rnd=None,
                   left: float = 0.0) -> Out:
        """Host→guest: where we are. Drives the guest's PANEL, not its scene.

        `left` is seconds remaining, not an absolute deadline: the two machines
        never agree on the time, and the doc's own rule is that a sender's `ts`
        is diagnostics only. The guest turns it into a deadline on its own
        clock the moment it arrives, which is the only clock it can trust.
        """
        out = Out()
        if not self.is_host:
            return out
        if phase:
            self.phase = str(phase)
        if rnd is not None:
            self.round = int(rnd)
        self.tick += 1
        self.deadline = (now + float(left)) if left else 0.0
        self._sent_at = now
        out.dirty = True
        return self._emit(out, T_STATE, phase=self.phase, n=self.round,
                          tick=self.tick, left=round(float(left), 2))

    def send_do(self, now: float, row: dict) -> Out:
        """Host→guest: run this action row.

        Nothing gates it here. Safety is the HARDWARE's job — a pump's own
        limits, a plug you can reach — not a number in a web panel that the
        other machine cannot see and that would silently swallow a fire with
        nowhere to look for why.
        """
        out = Out()
        if not self.is_host:
            out.notes.append("only the host sends rows")
            return out
        if self.link.state not in (S_MATCH, S_SETTLING):
            out.notes.append(f"no rows outside a match ({self.link.state})")
            return out
        self._sent_at = now
        return self._emit(out, T_DO, row=dict(row or {}))

    def ack(self, now: float, *, re: int, did: str = "", ok: bool = True,
            why: str = "", capacity=None) -> Out:
        """Guest→host: what ACTUALLY happened, plus capacity for free.

        Without this the host narrates a 20-second fire that never ran and the
        whole match becomes fiction. A refusal must be narratable.
        """
        out = Out()
        if capacity is not None:
            self.capacity = float(capacity)
        self._sent_at = now
        return self._emit(out, T_ACK, re=int(re), ok=bool(ok), did=str(did),
                          why=str(why), cap=round(float(self.capacity), 1))

    def push_tele(self, now: float, capacity, *, firing: bool = False,
                  dev: str = "", claim=None, force: bool = False) -> Out:
        """Guest→host: capacity, stamped with the tick it describes.

        Idle costs nothing — capacity doesn't move when no pump is running, so
        this only goes out WHILE FIRING. A `claim` (a threshold crossing) is an
        event, not a sample: it jumps the interval and goes immediately, so
        both sides' claims travel the same path with the same latency.
        """
        out = Out()
        self.capacity = float(_num(capacity) or 0.0)
        if self.link.state not in (S_MATCH, S_SETTLING):
            return out
        urgent = bool(force or claim)
        if not urgent:
            if not firing or (now - self._tele_at) < TELE_EVERY:
                return out
        self._tele_at = now
        self._sent_at = now
        body = {"cap": round(self.capacity, 1), "tick": self.tick,
                "firing": bool(firing), "dev": str(dev or "")}
        if claim:
            body["claim"] = claim
        return self._emit(out, T_TELE, **body)

    # -- teardown ------------------------------------------------------------ #
    def end(self, now: float, why: str = "") -> Out:
        """Host: clean end. Devices stop on both sides — a match that's over has
        no business leaving a pump running on someone else's machine."""
        out = Out()
        if not self.is_host or self.link.state not in LIVE:
            out.notes.append("nothing to end")
            return out
        self.last_why = str(why or "match over")
        self._emit(out, T_BYE, why=self.last_why)
        out.notes.append(f"match over: {self.last_why}")
        out.stop = True
        out.dirty = True
        self._settle(out)
        return out

    def block(self, now: float, *, bot_id="", name="", player="", owner="") -> Out:
        """Block a bot. Ends any match with them, drops the pairing, and stops
        us answering their hello — a block that only refuses the NEXT invite is
        not a block."""
        bid = str(bot_id or self.link.peer or "")
        out = Out()
        if not bid:
            out.notes.append("nobody to block")
            return out
        entry = {"bot_id": bid,
                 "bot_name": str(name or self.link.peer_name or ""),
                 "player": str(player or self.peer_player or ""),
                 "owner_id": str(owner or self.link.peer_owner or "")}
        self.blocked = [b for b in self.blocked
                        if str((b or {}).get("bot_id")) != bid] + [entry]
        if self.link.peer == bid and self.link.sid:
            out.merge(self.abort(now, "blocked"))
        if self.link.peer == bid:
            self.link.peer = ""
            self.link.peer_name = ""
            self.link.peer_owner = ""
            self.peer_player = ""
            self.peer_channels = {}
        out.notes.append(f"blocked {entry['bot_name'] or bid}")
        out.dirty = True
        return out

    def unblock(self, bot_id: str) -> Out:
        out = Out()
        bid = str(bot_id or "")
        before = len(self.blocked)
        self.blocked = [b for b in self.blocked
                        if str((b or {}).get("bot_id")) != bid]
        out.notes.append("unblocked" if len(self.blocked) < before else "was not blocked")
        out.dirty = True
        return out

    def check_end(self, now: float, capacity=None) -> Out:
        """Have I reached the line? Then I have lost, and I say so.

        Run on BOTH installs, on each one's own clock — the same place any
        other live capacity threshold would be watched. Deliberately only ever
        tests THIS install's own meter: the other machine's number arrives
        rounded and a heartbeat late, and a match must not be decided on a
        stale copy of a meter its owner holds exactly.
        """
        out = Out()
        if self.link.state not in LIVE or self.conceded or self.end_max <= 0:
            return out
        cap = _num(self.capacity if capacity is None else capacity)
        if cap is None or float(cap) < self.end_max:
            return out
        return out.merge(self.concede(now, f"reached {self.end_max:g}%"))

    def concede(self, now: float, why: str = "conceded", who: str = "") -> Out:
        """Give up. Tells the other bot, and leaves the match STANDING.

        Deliberately not an abort. The End Condition's action block still has
        to run — the outro, the result card, whatever the author wrote — and a
        match that tore itself down first would have nothing left to run it on.
        The caller ends the match once that block is done.
        """
        out = Out()
        loser = str(who or self.link.me)
        if self.link.state not in LIVE:
            out.notes.append("no match to concede")
            return out
        if self.conceded:
            out.notes.append(f"already conceded by {self.conceded}")
            return out
        self.conceded, self.conceded_why = loser, str(why or "conceded")
        out.dirty = True
        out.notes.append(f"{loser} conceded: {self.conceded_why}")
        if loser == self.link.me:
            self._emit(out, T_CONCEDE, why=self.conceded_why, loser=loser)
        self._sent_at = now
        return out

    def _on_concede(self, env: dict, now: float, out: Out) -> Out:
        if self.link.state not in LIVE:
            return out
        loser = str(env.get("loser") or env.get("from") or "")
        if self.conceded:
            return out                       # both gave up at once; first wins
        self.conceded = loser
        self.conceded_why = str(env.get("why") or "conceded")
        out.dirty = True
        out.notes.append(f"{self.peer_player or loser} conceded: "
                         f"{self.conceded_why}")
        return out

    def abort(self, now: float, why: str) -> Out:
        """Either side, any time. Always names a reason, always stops locally."""
        out = Out()
        reason = str(why or "aborted")
        self.last_why = reason
        if self.link.sid and self.link.peer:
            self._emit(out, T_ABORT, why=reason)
        out.notes.append(f"aborted: {reason}")
        out.stop = True
        out.dirty = True
        self._settle(out)
        self._sent_at = now
        return out

    def _settle(self, out: Out | None = None) -> None:
        """Drop the match, keep the pairing. The peer is config — you named
        that bot — while everything about the session is transient. An install
        that's still advertising goes straight back to looking for its peer
        rather than sitting in a dead state nobody clears.

        This is also where a split-the-difference is UNDONE. Every way a match
        can end comes through here — abort, bye, decline, timeout, finish — so
        the rig cannot keep a calibration it only agreed to for one match.
        """
        self.conceded = self.conceded_why = ""
        self.end_max = 0.0
        if self.cal_before is not None:
            back = self.cal_before
            self.cal_before = None
            self.calibration = back
            if out is not None:
                out.cal = back
                out.notes.append(
                    f"match over — pump back to its own {float(back):g}s to 100%")
        self.link.reset()
        self.invite = {}
        self.ready_me = self.ready_peer = False
        self.phase = ""
        self.round = 0
        self.tick = 0
        self.deadline = 0.0
        self.match_at = 0.0
        self.targets = {}
        self.peer_claim = None
        self._expires = 0.0
        self.link.state = S_ADVERTISED if self.advertising else S_IDLE

    # -- incoming ------------------------------------------------------------ #
    def feed(self, env: dict, now: float, capacity=None) -> Out:
        """One message off bot_network. Returns what to do about it."""
        out = Out()
        if capacity is not None:
            self.capacity = float(_num(capacity) or 0.0)
        ok, why = self.link.accept(env)
        if not ok:
            # "self" is our own post echoing back and is not worth a line;
            # everything else is named, because a silently ignored envelope is
            # exactly the dead end this protocol is built to avoid.
            if why not in ("self", "not an envelope"):
                out.notes.append(f"dropped {env.get('t')} from "
                                 f"{env.get('from')}: {why}")
            return out
        self._seen_at = now
        # Capacity rides on whatever is already being sent — `tele`, `ack`, or
        # a heartbeat. Reading it in one place means a new envelope type can
        # carry it without anyone remembering to wire it up.
        cap = _num(env.get("cap"))
        if cap is not None and env.get("from") == self.link.peer:
            self.peer_cap = cap
        # A DURATION, never a timestamp: "4.2 seconds left" survives clock skew
        # between two machines, "finishes at 10:04:31" does not.
        pt = _num(env.get("pt"))
        if pt is not None and env.get("from") == self.link.peer:
            self.peer_pump = max(0.0, pt)
        t = env["t"]
        if t == T_HELLO:
            return self._on_hello(env, now, out)
        if t == T_BEAT:
            return out
        if t == T_CONCEDE:
            return self._on_concede(env, now, out)
        if t == T_INVITE:
            return self._on_invite(env, now, out)
        if t == T_ACCEPT:
            return self._on_accept(env, now, out)
        if t == T_DECLINE:
            self.last_why = str(env.get("why") or "declined")
            out.notes.append(f"{self.link.peer_name or 'peer'} declined: {self.last_why}")
            out.dirty = True
            self._settle(out)
            return out
        if t == T_READY:
            self.ready_peer = True
            if self.ready_me and self.link.state == S_LINKED:
                self.link.state = S_READY
                self._expires = now + LINK_TTL
                out.notes.append("both armed")
            out.dirty = True
            return out
        if t == T_STATE:
            return self._on_state(env, now, out)
        if t == T_DO:
            return self._on_do(env, now, out)
        if t == T_ACK:
            return self._on_ack(env, now, out)
        if t == T_TELE:
            return self._on_tele(env, now, out)
        if t == T_ABORT:
            self.last_why = str(env.get("why") or "peer aborted")
            out.notes.append(f"peer aborted: {self.last_why}")
            out.stop = True
            out.dirty = True
            self._settle(out)
            return out
        if t == T_BYE:
            self.last_why = str(env.get("why") or "match over")
            out.notes.append(f"match over: {self.last_why}")
            out.stop = True
            out.dirty = True
            self._settle(out)
            return out
        return out

    def _on_hello(self, env: dict, now: float, out: Out) -> Out:
        frm = env["from"]
        name = str(env.get("name") or "")
        if self.link.peer and frm == self.link.peer and self.link.state in LIVE:
            # Our peer is advertising again, which means it came back WITHOUT
            # resuming our session — its seq counter has reset and nothing it
            # says will line up. A dead peer must come back loudly.
            return out.merge(self.abort(now, f"{name or 'peer'} restarted mid-match"))
        if self.is_blocked(frm):
            # Silent to THEM, named to us: a blocked peer learns nothing, and
            # the operator can still see why their panel stayed empty.
            out.notes.append(f"ignoring a blocked bot ({name or frm})")
            return out
        if not self.link.peer:
            want = self.peer_name.strip()
            if not want:
                out.notes.append("no peer bot name set — nobody to pair with")
                return out
            if name.strip().lower() != want.lower():
                out.notes.append(f"ignoring hello from {name or frm} — "
                                 f"waiting for {want}")
                return out
            self.link.peer = frm
            out.dirty = True
        self.link.peer_name = name or self.link.peer_name
        self.link.peer_owner = str(env.get("owner") or "")
        self.link.peer_version = str(env.get("ver") or "")
        self.peer_install = str(env.get("install") or "")
        self.peer_caps = list(env.get("caps") or [])
        self.peer_cal = env.get("cal")
        self.peer_pref = str(env.get("pref") or "either")
        self.peer_player = str(env.get("player") or "") or self.peer_player
        self.peer_channels = {"net": str(env.get("net") or ""),
                              "cast": str(env.get("cast") or "")}
        for line in check_channels(self._mine(), self.peer_channels):
            out.notes.append(f"⚠ {line}")
        out.notes.append(f"peer online: {self.link.peer_name or frm}"
                         + (f" · {self.link.peer_version}" if self.link.peer_version else ""))
        if str(env.get("ack") or "") != self.link.me:
            # They haven't seen us yet. Answer once, stamped with their id, so
            # the exchange terminates in two messages instead of ping-ponging.
            out.merge(self.say_hello(now, force=True, ack=frm))
        return out

    def _on_invite(self, env: dict, now: float, out: Out) -> Out:
        frm = env["from"]
        sid = env.get("sid") or ""
        if self.is_blocked(frm):
            out.notes.append(f"ignoring an invite from a blocked bot ({frm})")
            return out
        if frm != self.link.peer:
            out.notes.append(f"ignoring invite from an unpaired bot ({frm})")
            return out
        if self.link.state not in (S_IDLE, S_ADVERTISED):
            try:
                out.send.append(encode(T_DECLINE, frm=self.link.me, to=frm, sid=sid,
                                       seq=self.link.next_seq(),
                                       why=f"busy ({self.link.state})"))
            except ProtocolError:
                pass
            out.notes.append(f"declined an invite — already {self.link.state}")
            return out
        bad = check_channels(self._mine(), {"net": env.get("net"), "cast": env.get("cast")})
        if bad:
            why = "; ".join(bad)
            try:
                out.send.append(encode(T_DECLINE, frm=self.link.me, to=frm, sid=sid,
                                       seq=self.link.next_seq(), why=why))
            except ProtocolError:
                pass
            out.notes.append(f"declined: {why}")
            return out
        self.invite = {"sid": sid, "game": str(env.get("game") or ""),
                       "input": str(env.get("input") or "operators"),
                       "cost": str(env.get("cost") or ""),
                       "targets": dict(env.get("targets") or {}),
                       "target": (env.get("targets") or {}).get(self.link.me),
                       # who is asking, where it will be played, and on what terms
                       "host": str(env.get("host") or "") or self.peer_player,
                       "bot": str(env.get("bot") or "") or self.link.peer_name,
                       "cal": env.get("cal"),
                       "scene": str(env.get("scene") or ""),
                       "cap": _num(env.get("cap")) or 0,
                       "venue": dict(env.get("venue") or {}),
                       "net": str(env.get("net") or ""),
                       "cast": str(env.get("cast") or ""),
                       # The host's pump speed, and whether they offered to
                       # meet in the middle on it. Both are shown in the popup.
                       "cal": _num(env.get("cal")),
                       "split": bool(env.get("split")),
                       "from": frm}
        self.link.state = S_INVITED
        self._expires = now + float(_num(env.get("ttl")) or INVITE_TTL)
        out.dirty = True
        out.notes.append(f"invited to {self.invite['game'] or 'a match'}: "
                         f"{self.invite['cost'] or 'no estimate given'}")
        return out

    def _on_accept(self, env: dict, now: float, out: Out) -> Out:
        if self.link.state != S_INVITING:
            out.notes.append(f"ignoring accept while {self.link.state}")
            return out
        bad = check_channels(self._mine(), {"net": env.get("net"), "cast": env.get("cast")})
        if bad:
            return out.merge(self.abort(now, "; ".join(bad)))
        self.peer_caps = list(env.get("caps") or self.peer_caps)
        self.peer_video = bool(env.get("video"))
        self.peer_player = str(env.get("player") or "") or self.peer_player
        if env.get("cal") is not None:
            self.peer_cal = env.get("cal")
        # The guest took the split. The number was computed THERE, from the
        # host's own figure in the invite and the guest's corrected one, and it
        # travels back rather than being recomputed — two machines arriving at
        # the same midpoint separately is one rounding rule away from two
        # different pumps.
        mid = _num(env.get("split_cal"))
        if self.split_offer and mid and float(mid) > 0:
            mid = float(mid)
            was = self.calibration
            self.cal_before = float(_num(was) or 0) or None   # restore on end
            self.calibration = self.peer_cal = mid
            out.cal = mid
            out.notes.append(f"split the difference: both pumps set to {mid:g}s "
                             f"to 100% (was {float(_num(was) or 0):g} here)")
            # Only the FEEL changes: both pumps now take the same wall-clock
            # time per percent. The finish line was already identical.
        self.link.state = S_LINKED
        self._expires = now + LINK_TTL
        out.dirty = True
        out.notes.append(f"{self.link.peer_name or 'peer'} accepted")
        return out

    def _on_state(self, env: dict, now: float, out: Out) -> Out:
        if self.is_host:
            return out
        self.phase = str(env.get("phase") or self.phase)
        self.round = int(_num(env.get("n")) or 0)
        self.tick = int(_num(env.get("tick")) or 0)
        left = _num(env.get("left")) or 0.0
        self.deadline = (now + left) if left else 0.0
        if self.link.state in (S_LINKED, S_READY):
            self.link.state = S_MATCH
            self.match_at = now
            out.notes.append("match started")
        out.dirty = True
        return out

    def _on_do(self, env: dict, now: float, out: Out) -> Out:
        if self.is_host:
            out.notes.append("ignoring a do row — we're the host")
            return out
        if self.link.state not in (S_MATCH, S_SETTLING):
            return out.merge(self.ack(now, re=env["seq"], ok=False,
                                      why=f"refused: not in a match ({self.link.state})"))
        row = env.get("row")
        if not isinstance(row, dict) or not row.get("type"):
            return out.merge(self.ack(now, re=env["seq"], ok=False,
                                      why="refused: not an action row"))
        # SECONDS NEVER CROSS. Not a limit — a unit problem: 20 seconds is a
        # different amount of inflation on every rig, so a row in seconds means
        # something different over here than it did over there. Percent is the
        # only portable unit, and this is the last place to catch it.
        if (str(row.get("type")) == "fire"
                and str(row.get("fire_mode") or "") == "seconds"):
            why = "refused: % only — seconds aren't portable between rigs"
            out.notes.append(f"fire {why}")
            return out.merge(self.ack(now, re=env["seq"], ok=False, why=why))
        out.rows.append({"re": int(env["seq"]), "row": dict(row)})
        return out

    def _on_ack(self, env: dict, now: float, out: Out) -> Out:
        line = str(env.get("did") or "") if env.get("ok") else str(env.get("why") or "")
        if line:
            out.notes.append(f"{self.link.peer_name or 'peer'}: {line}")
        return out

    def _on_tele(self, env: dict, now: float, out: Out) -> Out:
        claim = env.get("claim")
        if claim:
            self.peer_claim = dict(claim) if isinstance(claim, dict) else claim
            out.notes.append(f"{self.link.peer_name or 'peer'} claims "
                             f"{self.peer_claim}")
            out.dirty = True
        return out

    # -- the clock ----------------------------------------------------------- #
    def tick_clock(self, now: float) -> Out:
        """Call about once a second. Re-advertises, heartbeats, and enforces
        every timeout — a state with no exit is a hung match."""
        out = Out()
        st = self.link.state

        if self.advertising and st in (S_IDLE, S_ADVERTISED) and not self.link.peer:
            out.merge(self.say_hello(now))
        elif self.advertising and st in (S_IDLE, S_ADVERTISED):
            # Paired but idle: a much lazier hello, purely so a peer that
            # restarted can find us again without us spamming the channel.
            if (now - self._hello_at) >= (HELLO_EVERY * 6):
                out.merge(self.say_hello(now))

        if st in (S_INVITING, S_INVITED) and self._expires and now > self._expires:
            who = "nobody answered the invite" if st == S_INVITING else "invite expired"
            self.last_why = who
            out.notes.append(who)
            out.dirty = True
            self._settle(out)
            return out

        if st in (S_LINKED, S_READY) and self._expires and now > self._expires:
            return out.merge(self.abort(now, "linked but never started"))

        if st in LIVE and self._seen_at and (now - self._seen_at) > DEAD_AFTER:
            return out.merge(self.abort(
                now, f"{self.link.peer_name or 'peer'} went silent for "
                     f"{int(now - self._seen_at)}s"))

        if st in LIVE and (now - self._sent_at) >= BEAT_EVERY:
            self._sent_at = now
            # The GUEST's heartbeat carries its capacity. `tele` only goes out
            # while its pump is running, so between fires the host's number
            # ages — and the host is the one narrating from it. One extra field
            # on an envelope already being sent costs nothing.
            #
            # Not the other way round: the guest never needs the host's number.
            # The host narrates, owns the scoreboard and carries the
            # production, so sending it would be traffic for nobody on the
            # channel with the tightest budget.
            if self.is_host:
                self._emit(out, T_BEAT)
            else:
                self._emit(out, T_BEAT,
                           cap=round(float(self.capacity or 0), 1),
                           pt=round(float(self.pump_left or 0), 1))
        return out

    # -- resume -------------------------------------------------------------- #
    def snapshot(self) -> dict:
        """data/match.json. Guest resume is nearly free — replay the tail and
        dedup does the rest. A HOST that comes back and can't restore must post
        `abort` immediately; it owns referee state this can't capture."""
        snap = self.link.snapshot()
        snap.update({"game": self.game, "input": self.input, "cost": self.cost,
                     "phase": self.phase, "round": self.round, "tick": self.tick,
                     "targets": self.targets,
                     # A split-the-difference outlives a crash: without this the
                     # rig would come back still tuned to the other person's
                     # match, with nothing left that knows what it used to be.
                     "cal": self.calibration, "cal_before": self.cal_before,
                     "end_max": self.end_max,
                     "ready_me": self.ready_me, "ready_peer": self.ready_peer})
        return snap

    def restore(self, snap: dict, now: float = 0.0) -> Out:
        """Load a persisted match. Never resumes silently — the caller confirms
        first ("Rejoin match with Dave, round 4 of 7?")."""
        out = Out()
        snap = snap or {}
        self.link.restore(snap)
        self.game = str(snap.get("game") or "")
        self.input = str(snap.get("input") or "operators")
        self.cost = str(snap.get("cost") or "")
        self.phase = str(snap.get("phase") or "")
        self.round = int(_num(snap.get("round")) or 0)
        self.tick = int(_num(snap.get("tick")) or 0)
        self.targets = dict(snap.get("targets") or {})
        self.ready_me = bool(snap.get("ready_me"))
        self.ready_peer = bool(snap.get("ready_peer"))
        self.end_max = float(_num(snap.get("end_max")) or 0)
        if _num(snap.get("cal_before")) is not None:
            self.cal_before = float(_num(snap.get("cal_before")))
            self.calibration = float(_num(snap.get("cal")) or self.calibration)
        self._seen_at = self._sent_at = now
        if self.link.state in LIVE and self.link.is_host:
            # Host state is the referee — scores, the clock, the RNG — and none
            # of that survives a restart. Come back loudly rather than pretend.
            out.merge(self.abort(now, "host restarted — referee state is gone"))
        elif self.link.state in LIVE:
            out.notes.append(f"resumed {self.game or 'match'} with "
                             f"{self.link.peer_name or self.link.peer}"
                             + (f", round {self.round}" if self.round else ""))
        return out


def cost_line(game: str, *, rounds=0, max_pct=0, target=0) -> str:
    """The estimate the guest agrees to. "Race to 150%" and "one round of
    Roulette" are wildly different amounts of inflation, and a bare game name
    asks someone to consent without telling them to what."""
    bits = [str(game or "a match")]
    if _num(target):
        bits.append(f"race to {_num(target):g}%")
    if _num(rounds):
        bits.append(f"~{int(_num(rounds))} rounds")
    if _num(max_pct):
        bits.append(f"up to {_num(max_pct):g}% per hit")
    return " · ".join(bits)


def _num(v):
    """A number, or None when it's blank or a [placeholder] — the engine's
    placeholder-safe coercion rule, kept local so this module stays pure."""
    try:
        s = str(v).strip()
    except Exception:  # noqa: BLE001
        return None
    if not s or s.startswith("["):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None
