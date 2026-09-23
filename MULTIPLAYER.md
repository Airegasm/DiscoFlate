# DiscoFlate — Multiplayer (the rail)

_Design doc for the two-install multiplayer mode. This describes the **rail**: the
transport, handshake, safety model and lifecycle that every game mode shares.
Individual games (MultiRoulette, Race to N%, …) are built on top, one at a time,
and none of them touch the wire._

Companion to HANDOFF.md. Local dev doc, same as that one.

---

## ⚠ Shipping without it

Multiplayer is not finished, so a public build ships with it **off**. Flip
`MULTIPLAYER_ENABLED` in `app.py`, or set `DISCOFLATE_MULTIPLAYER=0` without
touching the file:

```
DISCOFLATE_MULTIPLAYER=0 ./start.sh
```

The gate holds at **three** levels, because any one alone leaves a way in:

| Level | Without it |
|---|---|
| `config_store.load()` forces `mode: solo` | a config already in multi boots into a mode whose exit is hidden |
| `set_config` refuses `mode: multi` | a stale tab or an API call switches it anyway |
| the panel hides the toggle, the tab and its cards | a dead control is worse than no control |

The panel treats an **absent** flag as available, so an older server still
works. `test_mp_gate.py` covers all three.

## The shape in one paragraph

Two DiscoFlate installs — separate machines, separate bot tokens, separate pumps —
in the same Discord channel. They discover each other over a private **bot_network**
channel, agree to a match, and play one game across both. One side is **host**: it
referees, narrates, and is the only side that listens to commands. The other is
**guest**: devices, ceilings and a scene, nothing else. The host drives the guest by
sending it **action rows**, which the guest runs locally through its own block
runner, gated by its own limits. Discord is the wire. The guest's pump is never
reachable except through the guest's own gate.

---

## Hard requirements

1. Both **people** in the same broadcast channel
2. Both **bots** pointed at that same broadcast channel
3. Both **bots** pointed at the same bot_network channel
4. That broadcast channel is the **only** place either bot speaks, for the duration

Everything else — video, scenes, overlays — is enrichment.

---

## What carries over from single player

Four things, and nothing else:

1. **Devices** — the device list and their calibration
2. **Discord** — the token, the bot, the connection
3. **The player name** — what this person is called in messages
4. **The name of the bot you want to interface with** — multiplayer-only, but it's
   configured the same way: you *name* your opponent's bot rather than pairing with
   whoever answers. `hello` then resolves that name to an id and caches it. A third
   install sitting in the channel can't grab the pairing.

Everything else is multiplayer's own: its scenes, its game modes, its commands, its
referee. Commands, ranges, timed events, capacity events, prizes and minigames do not
come across at all.

---

## Play is programmatic, not operated

**During a match you should barely touch the DiscoFlate app.** The panel's entire job
in multiplayer is setup — pick mode, pick the two channels, name the peer bot, set
your ceilings, accept or decline — and then get out of the way. Everything after that
happens in Discord.

This is a hard constraint on the referee, not a UI preference:

- The match must be **fully self-driving**. Rounds advance on the host's own clock.
  There is no "press here to continue", no operator cockpit, no human in the loop.
- Anything a human must do during a match happens **in the broadcast channel**, as a
  command or as **a button on an embed, posted when they're asked to press it** —
  where both players and the audience already are.
- Because a component interaction only ever reaches the app that owns the message,
  **the host posts every button**, including the ones the guest's player presses.
  There is no way to delegate that, and no reason to want to.
- A match must be able to run start to finish with both panels closed.

The Chat tab can carry over, but it's a minor surface here and probably wants a
rethink — you can't see the other player's video in it anyway, so it isn't the place
you'll be watching from. Discord is.

---

## Roles

### Host
- Referees the match (owns scores, rounds, the clock, the RNG)
- Narrates in the broadcast channel
- **The only side that resolves commands.** The guest's command handling is off.
- Owns every Discord component (button/modal), because interactions only ever reach
  the app that owns the message
- Owns the scoreboard embed
- Its loaded multiplayer scene determines **which game is being played**

### Guest
- Devices, ceilings, a scene. That is the entire multiplayer config.
- Runs `do` rows locally, gated
- Reports what actually happened (`ack`) and its own capacity (`tele`)
- Never resolves a command, never adjudicates, never narrates unless told to
- No events, no intros, no go-live pre-show

### Targets vs audience
- **targets** — bound to an install, have a device, can receive `do` rows. Two in v1,
  but carried as a list from day one so teams don't need a protocol break.
- **audience** — everyone else in the channel. No install, no device, exists only in
  the host's engine. **Never appears in an envelope.** Gameshow mode therefore costs
  the protocol nothing.

`input: operators | audience | both` is a dial on the match, not a fork in the build —
and it is **orthogonal to the game mode**. Gameshow and Versus aren't two games; they
are two sets of hands on the same one. Treat "works under either input source" as a
design constraint on every game mode: same win condition, same referee, different
question about who gets to push.

---

## The two channels

| | bot_network | broadcast |
|---|---|---|
| Purpose | protocol envelopes only | the venue — narration, commands, audience |
| Who posts | both bots | host, plus the guest when told via `do {message}` |
| Humans | none needed | both players + audience |
| Needs | View Channel, Send Messages, **Read Message History** | View Channel, Send Messages, Embed Links |

They **do not have to be in the same server**. bot_network living in a private
two-person server while broadcast sits in the public one is probably the nicer
arrangement — but it means **both bots need invites to both servers**, and somebody
will forget one.

### Preflights (all local — `chat_perms` does this math with no API calls)

1. My bot can post in bot_network (+ Read Message History)
2. My bot can post in broadcast (+ Embed Links)
3. Peer's bot_network id == mine
4. Peer's broadcast id == mine
5. If the broadcast channel is a voice channel: both target owners are guild members
   (`_is_member`), and if cameras are expected, both are connected to it

Any failure must name **which check failed**, by name. "Dave's bot is pointed at
#general, you're pointed at #gameshow" — never a silent dead end.

### Panel states

```
⚠  Bot can't post here            — preflight failed, says which
◌  No peer seen yet               — posting hello, nobody answering
✓  Peer online: Dave · v3.90.2    — handshake complete
```

---

## The wire

### The channel is a log, not a socket

Envelopes persist as messages. That's the single best property of this design:
**reconnection is free.** A bot that drops off the gateway and re-IDENTIFYs (the one
case discord.py won't replay for you) reads the tail of bot_network, finds everything
with its live `sid` and `seq` > last-seen, and catches up. No acks, no retry queue,
no liveness dance.

Design it as an append-only sequenced log, not request/response.

### Envelope format

One line of message content, no embed:

```
DF1 state {"sid":"m7k2","seq":4,"from":"1130…","to":"1188…","tick":47,
           "phase":"round","n":2,"left":37.5}
```

- `DF1` — protocol version, and a cheap `startswith` test before any JSON parse
- `<type>` — one of the eight below
- `sid` — match id, generated by the host at invite
- `seq` — monotonic **per sender per session**. This is the whole reliability story:
  dedup on `(from, sid, seq)`, ignore anything ≤ last seen, and every envelope becomes
  idempotent. A `result` replayed after reconnect cannot fire a pump twice.
- `from` / `to` — bot user ids. Anything not addressed to me, or not from my paired
  peer, is dropped silently.
- `ts` — sender's clock. **Diagnostics only.** Never trust it for timing.

⚠ **Durations travel, timestamps don't.** `state` carries `left` (seconds remaining)
rather than an absolute deadline, and `invite` carries `ttl` rather than an expiry.
An absolute deadline is a value on the *sender's* clock, and the rule directly above
says that clock can't be trusted — the receiver turns the duration into a deadline on
its own the instant it arrives. Same reasoning as tick stamps: make the readings
comparable instead of making the clocks agree.

**`hello` carries both channel ids**, so preflights 3 and 4 are answerable the moment
the peer is seen rather than at invite time. "Dave's bot is pointed at #general" is a
fixable problem when the panel says it up front and a baffling dead end when it
surfaces as a declined invite. It also carries `ack` — the id of the peer whose hello
prompted it — which terminates the exchange in two messages instead of ping-ponging:
answer a hello only when its `ack` isn't you.

Plain content, not an embed: `message.content` comes through raw and unmangled, it's
readable in the channel when debugging, and it's one string compare to reject
everything else. 2000-char cap is ample if boards, images and per-tick state stay off
the wire.

**Everything is keyed by install id, never by role.** Roles are per-match and
transient; installs are permanent. `cap:{"1130…":63,"1188…":41}`, not
`cap:{"host":63,"guest":41}`.

### The twelve types

**Handshake** (both directions)

| Type | From | Carries |
|---|---|---|
| `hello` | either | bot id, owner id, install id, version, caps[], display name, **primary pump `calibration_seconds_to_100`**, **both channel ids**, `pref`, `ack` |
| `invite` | host | sid, game mode, **compensated targets**, **cost estimate**, both channel ids, targets[], expiry |
| `accept` | guest | its targets, caps, its advertised ceilings, confirms both channel ids |
| `decline` | guest | reason (named, never generic) |
| `ready` | either | armed |

**Match**

| Type | From | Carries |
|---|---|---|
| `state` | host→guest | phase, round, `tick`, `left`. Drives the guest's **panel**, not its scene. |
| `do` | host→guest | **an action row** (see below) |
| `ack` | guest→host | what actually happened + current capacity piggybacked |
| `tele` | guest→host | capacity + device status, stamped with the `tick` it reports for |

**Lifecycle**

| Type | From | Carries |
|---|---|---|
| `abort` | either | reason |
| `bye` | host | clean end |
| heartbeat | either | only while idle-in-match, ~25s |

90s of silence = peer presumed dead → abort → local safe stop (`stop_devices`).

### `do` carries an action row

Not a custom verb list — the **same object** `actionRow` produces and
`_run_action_block` runs on every surface already:

```
DF1 do {"sid":"m7k2","seq":9,…,
        "row":{"type":"message","style":"embed","text":"Dave's pump is charging…"}}

DF1 do {"sid":"m7k2","seq":10,…,
        "row":{"type":"fire","fire_mode":"seconds","seconds":20}}
```

- `message` rows post in the guest's voice (its bot name and avatar)
- `fire` rows hit the guest's pump, **through the ceiling gate**
- `overlay` / `scene_group` rows drive the guest's scene

No new vocabulary, no translation layer, and **anything added to the action system
later travels for free**. This is also why the protocol is already video-ready without
containing anything video-specific.

### `ack` reports truth, not intent

```
fired 20s ✓ · refused: over ceiling · refused: device offline · posted ✓
```

Without this the host announces a 20-second fire that never happened and the whole
match is fiction. **A refusal must be narratable** — "Dave's pump is offline" is a
better line than silence.

---

## Fires are percent. Pacing is calibration.

These are two halves of one problem and you need both.

**% fixes how much.** A second is not a portable unit across two rigs — "20 seconds"
is a different amount of inflation on every pump — whereas 10% is 10% everywhere. So
**multiplayer fires are %-only**: `fire_mode: add | to`, never `seconds`.

A `seconds` fire arriving over the wire is **refused, never converted.** The guest
could convert it with its own calibration, but that would silently reintroduce the
exact unfairness the rule exists to remove, and the host would never find out.

**Calibration fixes how fast.** % says nothing about rate: a pump that fills 100% in
60s reaches any finish line twice as quickly as one that takes 120s. So `hello` carries
each side's **primary pump `calibration_seconds_to_100`**, and the host compensates.

Time to a target is `target / rate`, so equal time means targets scale with rate. The
reference is the **fastest** pump, which means every adjusted target is ≤ the base:

```
base 150%   ·   A fills 100% in 60s   ·   B fills 100% in 120s
            →   A races to 150%,  B races to 75%
            →   both take 90 seconds of pumping
```

Referencing the fastest (rather than the slowest) is a consent decision, not a
mathematical one. The invite's **cost estimate is what the guest agreed to**, so
compensation must only ever bring a finish line *down* — never push someone past the
number they accepted. That also means the cost estimate is computed **after**
compensation, or informed consent is quietly wrong.

If either calibration is missing, `compensate()` returns `ok:false`, everyone gets the
base target, and **the match must say so out loud** rather than pretend the race is
even.

`multiplayer.fill_rate()` and `multiplayer.compensate()` implement this; the gate
(`Ceiling`) is entirely in % and knows nothing about rate.

---

## Safety model

**The peer never sends a command. It sends a row, and the guest decides.**

Every `fire` / device row passes a local gate at the one chokepoint before the device
call:

- max seconds per fire
- max total per session
- max capacity %
- kill switch

The host can ask for anything; the guest's ceiling is what actually happens, and the
refusal comes back as an `ack`. This is why authenticity isn't load-bearing — a
malicious peer's best available attack is **lying about who won**.

`sid` + `seq` + peer-id checks handle the realistic failure modes (a stale envelope
from last night's match, a third install accidentally in the channel, bugs). If you
want real authentication later, it's a pairing code exchanged **out of band** plus an
HMAC per envelope — about 20 lines. Generating the secret over bot_network would be
security theatre.

### Informed consent at accept

`invite` carries a **cost estimate**, not just a game name:

```
MultiRoulette · ~10 rounds · up to 40s per hit
```

The guest's panel shows it against their own ceilings before they accept. "Race to
150%" and "one round of Roulette" are wildly different amounts of inflation and the
guest is agreeing before they know which.

---

## Lifecycle

```
idle → advertised (hello posted) → inviting / invited → linked (accepted)
     → ready → in-match (round loop) → settling → done → idle
```

Every non-idle state has a timeout. Any `abort` or timeout → local safe stop.

### Resume

Cost splits unevenly, and it's worth building accordingly:

**Guest resume is nearly free.** Its whole job is "apply what I missed." Persist
`{sid, role, peer, last_seq, phase}` to a small `data/match.json`, read the tail on
boot, replay by `seq`, dedup does the rest. The common case is the best case — the
match ended while you were down, you replay the `bye` and close cleanly.

**Host resume is real work**, because the host owns the referee state. Persist what
you can, and — load-bearing rule — **a host that returns and can't restore posts
`abort` immediately.** A dead host must come back loudly, never silently. The guest's
90s timeout covers a host that never returns at all.

Either way: **confirm before resuming.** "Rejoin match with Dave, round 4 of 7?"
Never silent.

---

## Rate limits

### What exists today

**There is no rate-limit handling in DiscoFlate.** `_send` (`discord_bot.py:719-738`)
is a bare `ch.send()` inside a broad `except Exception` that logs `send failed` and
returns `None`.

**discord.py handles it at the HTTP layer** — per-bucket locks, reads `X-RateLimit-*`,
pre-emptively sleeps on an empty bucket, and on a 429 sleeps `retry_after` and retries
(up to 5 attempts). So you are protected from errors and from a Cloudflare ban.

**You are not protected from latency**, and that's what matters here:

- An over-budget send doesn't fail, it **silently waits**. Late, in order, no error.
- `broadcast()` awaits each `_send` in a loop (`:812-821`), so one throttled channel
  **stalls the whole loop** and everything behind it.
- If discord.py *does* exhaust its retries, `_send`'s `except Exception` swallows it
  and the message is gone.

⚠ **The protocol path must not go through `_send`.** A silently dropped envelope is a
stalled match. Envelopes are idempotent by `seq`, so the sender can safely retry the
same one — but only if it knows it failed. Give the protocol its own send that
surfaces the exception.

### The budget

- **5 messages / 5 seconds per channel, per bot** (~1/sec sustained). Headers are
  authoritative; treat this as the planning figure.
- Each bot has its **own** bucket, and bot_network and broadcast are **separate**
  channels — so each bot gets ~1/sec in each.
- Global ceiling 50 req/s across everything. Not reachable here.
- A `replace_key` round costs **two** calls (delete + send). Editing in place costs one.

**bot_network has room to spare. The broadcast channel is the real scarcity** — that's
where the show is. Keep every byte of telemetry out of it, batch narration into one
message per beat, and prefer editing in place over post-and-delete.

---

## Telemetry and the scoreboard

**Push, never pull.** Pull costs two messages per reading and adds a round trip
(200–400ms) at exactly the moment the host needs a number to decide something.

- Guest pushes `tele` unprompted, **~every 2s and only while its pump is firing**.
  Capacity doesn't move when nothing's running, so idle costs zero.
- **Piggyback capacity on every `ack`**, so the host has a fresh reading after every
  action for free.

### Tick stamps, not clock sync

The two machines don't need to agree on time — the readings need to be **comparable**.
The host's `state` carries a `tick` counter; each `tele` stamps the tick it reports
for. The host can then pair "tick 47: 63.4 vs 41.0" and know both samples describe the
same moment. No NTP, no drift, no trusting anyone's `ts`.

### Interval is for meters. Events decide outcomes.

⚠ **This is a correctness issue, not a display one.** If the host's own capacity is
instant and the guest's is up to 2s stale, the host wins every close finish — not
because anyone cheated, but because its samples are fresher.

- **Interval telemetry → gauges.** Cosmetic, lazy, stale is fine.
- **Threshold crossings → events.** The guest knows the instant it crosses 150% and
  reports it immediately, out of band. The host decides on **claims**, not samples, so
  both sides' claims travel the same path with the same latency.

**Dead heats are draws.** Both claims inside 500ms → draw. That's a constant, not a
policy, and it deletes all tie-break logic.

### The scoreboard

**One embed, owned by the host, edited in place**, carrying both meters:

```
Curtis 63% ██████░░░░ · Dave 41% ████░░░░░░
```

One message, one call per update, never scrolls, and it *is* the text base layer.
`_track_msg` / `_delete_tracked` already keep sent messages keyed by `replace_key` —
editing one in place is a small extension of that, not new machinery.

---

## Video

**The venue sets the ceiling, each player sets the reality.**

- Broadcast channel is a **voice channel** → either player can be on camera,
  independently, and change their mind at any time
- Broadcast channel is a **text channel** → nobody is, by Discord's rules, not ours

So there is **nothing to negotiate in the handshake**. No video capability gate, no
symmetric agreement, no decline path. An Android install simply means that person's
camera is off, which is a normal state rather than an error (there's no virtual camera
on a phone — video runs on a PC with the phone driving it via VC Remote).

If the bots want to be graceful, voice state carries `self_video`, so the host can
notice a camera go dark and fall back to a text presentation of whatever was on that
tile. Nice-to-have.

### Text-complete is forced, not preferred

The host can turn their camera off mid-round and the mutual production vanishes
mid-spin. **The text layer carries the whole game at all times**; video is pure
enrichment layered on top. Nothing may exist *only* on a tile.

Same sequence single-player already took: text first, video later, always text.

### Scenes

- **Host scene** — the production: wheel, scoreboard, round timer, both meters, intro,
  events
- **Guest scene** — identity: their name, their meter, a hard-to-miss flash when
  they're the target. No events, no intros, no go-live.

Multiplayer overlay kinds follow the **`poll_viewer` precedent**: `poll_view()` returns
None → the overlay is invisible. A multiplayer overlay outside a match is simply
invisible, so nothing breaks when a scene is mode-switched or a match ends
mid-stream. No mode enforcement needed in the compositor — the data source returns
None and that's the whole mechanism.

Discord VC video is ~720p at a modest bitrate, and the grid crops tiles depending on
layout and participant count. **Big, high-contrast overlays; keep anything important
away from the edges.** Worth adding a safe-area guide to the scene canvas — it already
draws a percentage grid, so it's one more overlay on the same canvas.

---

## Config

```
mode: "solo" | "multi"          # header toggle, LOCKED at go-live,
                                # switching while live is REFUSED with a reason

multiplayer: {
  bot_network:  {guild_id, channel_id},
  broadcast:    {guild_id, channel_id},
  peer_bot_name: "…",           # you NAME the bot to pair with (carried over concept)
  peer:         {bot_id, owner_id, name},   # resolved by hello, then cached
  limits:       {max_pct_per_fire, max_session_pct, max_pct, on_exceed},
  role_pref:    "host" | "guest" | "either",
  auto_accept:  false
}
```

Scenes gain `mode: "solo" | "multi"` (tag, don't fork the store — `#scnSel` filters by
current mode; duplicating into `scenes` / `mp_scenes` doubles every scene function
already written) and, for multi scenes, `multi_game_mode`.

**The host's loaded multiplayer scene determines the game.** A text-only match still
has a scene loaded as the match container; its overlays just aren't rendered.

⚠ `check.sh` enforces `default_config.json` parity with DEFAULTS — new keys must land
there or the check fails.

---

## Code hook points

| What | Where (as built) |
|---|---|
| The protocol core, pure | `multiplayer.py` — `encode`/`decode`, `Link`, `Ceiling`, `Session`, `compensate` |
| Envelope path runs **first**, returns early | `_link_envelope(message)`, ahead of `_chat_capture` and the bot filter in `_handle` |
| bot_network kept out of the game | `_targets()` filters it out of `_targets_raw()`, so `_allowed` / `_broadcast_targets` / `_chat_watched` all inherit the exclusion from one place |
| Protocol send that surfaces failures | `_link_send` — **not** `_send`, which swallows every exception |
| Multiplayer's own venue send | `_link_say` — resolves the one channel from config, never `_broadcast_targets` |
| Scoreboard edit-in-place | `_link_board`, on top of `_track_msg` |
| Running a `do` row | `_link_row` → `engine._run_action_block` (`engine.py:1693`); `message` rows go to `_link_say` instead |
| Preflights (no API calls) | `link_preflight`, on top of `chat_perms` + `_is_member` |
| The match clock | `_link_loop`, started with the bot, cancelled with it |
| Guest command muting | `_mp_guest_muted`, checked in `_handle` |
| Resume | `_link_persist` / `_link_find_resume` / `link_resume` / `_link_catchup` (`data/match.json`) |
| Panel | `web/index.html` `data-tab="multi"`, `mpApply` / `mpRender` / `mpPoll`; API at `/api/mp/*` |
| The referees | `mp_games.py` — `Race`, and `MODES` so a new game is one entry |
| Referee lifecycle | `_race_build` / `_race_tick` / `race_start`, on the 0.25s match clock |
| Multiplayer's command surface | `_mp_cmd` / `_mp_handle`, its own gate in `_handle` |
| Which game is played | `_mp_game()` — the host's loaded multi scene, else the panel picker |
| Scene mode tag | `sceneMode` / `appMode` / `stgSetMode` / `stgModeUi`; `config_store.live_scene` |

⚠ **Multiplayer does not reuse single-player's channel plumbing, and does not branch
it either.** There is exactly one broadcast channel for the whole match — named in
config, verified by the handshake. So there is no target list, no pinning, no
fail-open/fail-closed question, and **no Isolate**. Isolate exists in solo because a
broadcast can fan out to many channels and sometimes you want to narrow it; in
multiplayer there is nothing to narrow. Multiplayer resolves its one channel and
speaks there, full stop.

The same principle applies throughout: **multiplayer gets its own send path, its own
target resolution, its own listener gate.** What is shared with single player is
*engine primitives* — devices, calibration, the capacity meter, the fire path, the
action-block runner — not the Discord plumbing. Any sentence of the form "multiplayer
is solo's X but with a flag" is a design smell; write multiplayer's own X.

---

## Build order

**1. The rail — BUILT.** Handshake, both channels, preflights, `do`/`ack`, ceilings,
telemetry, resume, abort, the panel. Proven headlessly rather than between two
houses: `test_multiplayer.py` drives two `Session`s through one shared log,
`test_multiplayer_bot.py` drives two `BotManager`s through one fake channel, and
`test_multiplayer_ui.js` evaluates the real panel block against a stub DOM. All
three run in `scripts/check.sh`.

⚠ **Not yet proven on real hardware.** Two live bots in two houses is still the only
thing that exercises rate limits, gateway drops and actual pumps.

⚠ **The phone can't be the second player.** The Android install is a genuine second
install, but it runs on the *same bot token* — so its envelopes carry the same `from`
id and every one of them is dropped as `"self"` before it reaches the session. A real
test needs a **second bot token** (a second application in the Developer Portal),
invited to both channels. Two DiscoFlate installs is not enough; two *bots* is the
requirement.

**2. Games are AUTHORED, not written.** There is no built-in game. A game is
**Rounds** made of **Actions** made of action rows — so a new one is built in
the panel, and it adds **nothing to the wire**.

_("Race to N%" appeared here as a worked example and briefly existed as code.
It is gone: it configured a game nobody plays, its chat commands silently did
nothing once Rounds existed, and its settings filled a panel column that turned
out to be dead. A match with no Rounds now says so instead of quietly running
something else.)_

**3. MultiRoulette.** Adds real referee logic on a wire already proven. Text wheel
first — a message edited in place, `🎡 Curtis · Dave · Curtis · **DAVE**` over ~3s.

**4. Video pass.** Scenes, the spinning wheel, gauges. Adds **nothing** to the
protocol — new action rows in the host's blocks and new overlay kinds in the scenes,
both purely local. Can't break 1–3.

Each new game mode after that is additive: a scene with a `multi_game_mode`, a
referee, some action blocks. Nothing in that list touches the wire.

---

## What was built

### Multiplayer Actions — the reusable block

An **Action** is plain action rows, saved globally in `mp_actions`. It is not
bound to whatever invoked it, which is the point: two rules make that true.

1. **A row says WHO it acts on, not which player.** `multi_who` takes
   `chosen | other | both | host | guest | me | peer | leader | trailer |
   winner | loser`, resolved when the row runs. A row aimed at the guest leaves
   as a `do` envelope; one aimed at this install runs here. A target that
   resolves to nobody **drops the row** — inflating the wrong person because a
   name didn't resolve is the one outcome worth refusing.
2. **It reads the match out of `[multi_*]` placeholders**, never an argument it
   was handed. The prefix means a block can't confuse the other player's
   capacity with this install's own `[capacity]`.

| Row | Does |
|---|---|
| `mp_spin` | Picks a racer. Even by default; `weights` tilts it and **uneven odds are announced** — a wheel the audience can't see is indistinguishable from a rigged one. Fires nothing. |
| `mp_roll` | Rolls `NdN` (default **1d8**) and publishes `[multi_roll]`. **Rolls only** — a separate `fire` row spends it as a **percent**, because seconds are refused over the wire. |
| `mp_choice` | A **Player Choice**: buttons only the named player may press. **Blocks** until they answer. |
| `mp_duel` | Both racers pick from one embed, hidden until both are in. Each move carries what it **beats**, so RPS is data, not code. |
| `mp_action` | Runs another Action inline. **Depth-guarded** — one that runs itself would spiral. |

Everything else is the ordinary action system. The engine gained exactly one
hook (`mp_row_cb`) rather than a second block runner.

**A blocking row always has a way out.** `mp_choice` and `mp_duel` carry a
clamped deadline and a named default; a duel counts a **walkover** if only one
answered. A round that stalls on someone who walked away is the failure every
other timeout here exists to prevent. Both post as a **card** whatever
`rich_output` says — a player who misses a timed prompt because it looked like
chatter loses their turn.

**A draw resolves to nobody.** `multi_who: loser` returns an empty list, so the
fire is dropped rather than punishing someone arbitrarily.

### Rounds — a block, and how it ends

| Runs | Ends when | You enter |
|---|---|---|
| **Count** | N passes are done, whatever the meters say | Times |
| **First to %** | the first racer reaches the target | Target % |
| **Both Reach %** | everyone reaches it | Target % |

The **block is what repeats**: Count 1 is a plain sequence, Count 4 runs four
times, a target runs until somebody gets there. `repeat` still nests inside, so
"four spins, a video, four more, a video" is one pass of one round.

The editor is the **same** one a custom command uses — collapsible rows, drag to
reorder, unlimited.

⚠ **Order is LIST order**, with arrows. It used to sort by capacity band, which
stopped meaning anything once a round could end on a count. A `%` round with no
target is dropped; a Count round needs no target at all, and no give-up limit —
the count *is* the limit.

⚠ **Walk rounds by position, never by "which band is capacity in".** That read
one install's meter while the clear-test reads both, so a round the *other*
racer cleared left the driver on the same one forever. Every way a round can
fail to clear stops the sequence and says why.

### The opening — one entry point

```
both Ready → scene intro(s) → production layout → BOTH curtains up → the game
```

`start_match()` runs the lot; the Ready embed chains into it. **One** entry
point: two is how an operator ends up mid-pre-show with the picture already
live. The pre-show reuses `intro_stages()` — the same data Go Live uses — but
through multiplayer's own runner, not solo's go-live state machine.

With no Rounds defined it **says so** rather than quietly running something
else. A game IS its Rounds.

### The curtain

`set_blackout` blacks the room out **while still compositing overlays**, which
is what makes a pre-show possible. It falls when a match is **agreed**, on both
installs, and rises only when the host says so via a `camera` row.

⚠ Keyed on the **match**, not on whether the picture is currently black. The
check runs after every envelope, so a flag meaning "is it down" sees the host's
deliberate reveal and puts it straight back — the curtain slams shut the moment
the intro ends.

A bot cannot turn anyone's *Discord* camera on. This is DiscoFlate's own virtual
camera: the guest broadcasts black and the host's row reveals it.

### The handshake

```
hello ⇄ hello   resolve each other by NAME (blocked bots dropped HERE)
invite  →       host only; carries everything the popup must show
        ← accept  guest answers "on camera?"; calibration ships back
Ready embed     one embed, two buttons, each scoped to one player
```

**Both names are required to invite** — naming only the bot makes it possible to
start a match against the wrong person's install. The invite carries the host's
player and bot names, the scene, the game mode, the **cap** (a number, not "is
past 100% allowed" — the cap is what defines the guest's lose condition), the
calibration, and **both channels as names**: `#gameshow in The Den`, not `901`.

**Block bites at `hello`** — one that only refuses the next invite is not a
block. ⚠ It is persisted **before** the abort envelope goes out: applying first
posts a message, the bot sees its own post, the envelope path rebuilds the
session from config, and the block — in memory only at that instant — is read
straight back out.

**The guest's camera answer decides the host's production**: one gauge, or both
gauges labelled on opposite sides. Two scene groups, chosen once at match start;
the unused one is cleared so they can't stack.

### DiscoFlate Versus — the starter kit

Scene, Actions and Rounds **seed once** and are then yours: generate-when-
missing keyed by name and `_tpl_id`, and anything deleted never grows back.
All three together — a scene you can edit whose Actions are frozen is the worst
of both.

Its gauges are **vertical** (`orient: "v"`, which fills upward natively —
rotating a horizontal one would fill sideways and only look right by accident),
hugging each edge, labelled below. `capacity_gauge` gained `source: me | peer`;
the peer's meter was already arriving on `tele`/`ack`, so **nothing new crosses
the wire**. Default stays `me`, and outside a match a peer gauge reads **zero** —
showing your own capacity under your opponent's name is worse than nothing.

Videos are `media` overlays with no media, which draw nothing — so the show runs
before a single clip exists. **Videos are optional.**

### The guest is locked down, and Concede is the way out

A guest in a match **takes instructions**. Its commands are off — the mute sits
before the prefix check in `_handle`, so `!commands`, `#owner` macros and
everything after are dead for the whole session — and its panel locks to
**Chat plus the header**, because every editor would be editing something that
isn't driving anything.

The lock is driven by `mp_locked` on every poll, so it **releases itself** when
the match ends rather than being set once and needing to be unset. It moves the
guest to Chat once, not on every poll.

**Concede** sits on the Chat page for *both* sides: it aborts with a named
reason, which drops the handshake, stops both pumps and gives the guest its
panel back.

### Talking to the other bot

`mp_tell` sends the **name of an Action** for the other bot to run — not the
thing to do. They play *their* copy, on *their* scene.

That is the answer to "how do I put something on their stream": an overlay id
from your scene means something different over there, or nothing. So only
`fire`, `roll`, `stop_devices`, `camera` and `mp_action` may cross
(`CROSSABLE`), and the router refuses the rest by name — hiding the control is
not enforcement. `capacity` is deliberately out: the ceiling gates `fire`, not
a direct write to the meter.

Values ride along as `[multi_told_*]`, so "the wheel picked Dave" can be said
in their words.

⚠ **Capacity goes one way only.** The guest reports; the host narrates, owns
the scoreboard and carries the production, so the guest never needs the host's
number — sending it would be traffic for nobody on the channel with the
tightest budget. `tele` covers a running pump; the guest's **heartbeat carries
its capacity too**, so the number doesn't age between fires. Any envelope with
a `cap` field refreshes it, read in one place, so a new type carries it without
anyone remembering to wire it up.

### ⚠ Rules that are enforced, not just offered

- **A scene never crosses modes.** Solo is solo, multi is multi, fixed at
  creation. `config_store.lock_scene_modes()` runs in `set_config`, because the
  panel sends every scene on every save — the panel offering no button is not
  enforcement. The live scene drives gameplay, so one that crossed would drag
  its overlays and rules into a game with no use for them.
- **A group name never crosses modes.** Group names are scoped to the loaded
  scene by design. The hole was the fallback when nothing is loaded: it searched
  every scene, and both a solo scene and the versus one have an "Intro".
  `scenes_in_mode()` confines it.
- **One save payload.** The multiplayer editors reuse `actionRow()`, which calls
  `scheduleSave()` — so `gatherConfig()` must carry `mp_actions`, `mp_rounds`
  and `multiplayer`, or a row edit saves solo config, drops the edit and bumps
  the rev underneath the multiplayer save in flight ("config changed
  elsewhere"). It contributes **nothing** before the panel has hydrated, and
  never includes `blocked` — that is the server's.
- **Awards stack on what is queued, not on the meter.** The roulette spins again
  without waiting for a pump, so a second award lands mid-fire routinely.
  Measuring an `add` from the live meter loses the difference — two 10% awards
  15s apart on a 60s pump gave **17%, not 20%** — and loses *more* of it the
  faster they arrive, which shorts the **slower** rig hardest. A fairness bug,
  not a rounding one. `engine._pending_capacity` is the fix.

## Open — game design, not plumbing

- Does the audience influence the **wheel** (tilt the odds, then watch) or the
  **outcome** (see who it landed on, then decide how hard)? Different round shapes.
  _(Race sidesteps this — there's no wheel. MultiRoulette has to answer it.)_
- Round shape for games that HAVE rounds. _(Race is one continuous sprint.)_
- ~~What the multiplayer `!commands` actually are~~ — answered above for Race;
  a new mode adds its own to `multiplayer.commands`.
- Which aspects feed the weighting — capacity, streaks, stakes, votes
- Does capacity still move through named bands in multiplayer (dice, announce text,
  milestones), or is it a bare 0–999 that each game says something interesting about?

Minigames are **out** — multiplayer gets entirely new ones. The reuse here is
infrastructure, not content: devices, calibration, the capacity meter, the fire path,
scenes and overlays, the action-block runner, the competition referee shape, and the
bot plumbing.
