# DiscoFlate

A self-hosted **Discord bot + local web control panel** that turns chat commands
into timed smart-plug fires, driving an inflation-themed capacity game. Everything
runs **on your own machine** and controls **your own** smart plugs on **your own**
network — no cloud middleman, no third-party service holding your devices.

It ships as both a desktop app (Python) and a **one-tap Android app** (the whole
backend embedded via Chaquopy — no Termux, no separate process).

> **This is a real bot account**, not a self-bot. Automating a personal user
> account is what Discord bans — DiscoFlate doesn't do that.

---

## What it does

Players type commands (e.g. `!agroll`) in Discord. The bot rolls that capacity
range's dice, fires the active smart plug for the rolled number of seconds, and
a **capacity meter (0–100%+)** climbs based on the device's calibration. As
capacity rises it moves through **ranges**, each with its own dice, cooldowns,
announcements, commands, images, and one-time milestones.

**Everything is an action block.** A custom command, a timed-event round, a
capacity event, a poll option, a competition outcome, a bonus round, a prize
unlock — they're all the same ordered list of **action rows**:

`message` (plain/embed, optional gated button) · `fire` (fixed seconds or until
a %) · `roll` · `chance` (a gamble with nested win/miss blocks) · `capacity` ·
`wait` · `poll` · `competition` · `bonus_round` · `award` (grant a bonus command
or bank volume) · `command_gate` (Block/Allow/Resume play) · `achievement` ·
`stop_devices` · `end_session` — plus `broadcast` and `command`. The bot speaks
only through **message** rows; results (`[secs]`, `[result]`, `[bonus_cmd]`, …)
flow into the rows after them.

- **Capacity engine** — capacity accumulates while the active pump runs, at a
  rate set by that device's `seconds-to-100%` calibration.
- **Per-range everything** — dice (`N`d`sides`) + luck, per-command cooldowns
  (per-user or shared), per-person use budgets, **fire overrides** (one `!pump`
  can drive different pumps for different lengths per band), announce text,
  one-time milestone messages + images. **Range values always beat a command's
  own**; **Always-On** hosts range-free utility commands (a command lives in
  ranges *or* Always-On, never both).
- **Custom commands = gates + an action block.** Six interactive **minigames**
  (push-luck, simon, balloon, RPS, slots, blackjack) are the only special types —
  private button play, score→tier outcomes that can run any action block.
- **Timed events = three blocks** — 🚀 on activation → 🔁 each round → 🏁 when it
  ends. Rounds never overlap and never stall the game; `clean_previous` keeps
  loops to a single live message.
- **Capacity events** — one-shot blocks at a threshold (1–999%), whose
  `command_gate` rows can lock down play for the event or the session.
- **Polls, roll-off competitions** (fully button-driven — Enter Challenge →
  private rolls → results all at once), **team Bonus Rounds** (banked volume,
  Confirm to cash in), and **Perfect Prizes** (N perfect rolls — or any named
  achievement, like a blackjack 21 — runs an unlock block for the earner;
  re-earnable).
- **End sequences** — let capacity run past 100% to a final burst threshold with
  its own entry milestone and final message/image.
- **Modes, leaderboards (session / lifetime / per-range), per-user cooldowns,
  anti-spam buffer,** multi-server support with anonymized cross-server echoes.
- **Snapshot** — capture a webcam frame and post it to the channel.
- **Operator Controls** — dashboard buttons (Roll, Stop, Pump, Broadcasts,
  Leaderboards, Cleanup) that fire real in-channel actions *as the owner*.
- **Gameplay Presets & Templates** — save/load whole game setups (the shipped
  **Defaults (built-in)** preset included) or single commands/events/ranges;
  everything auto-migrates across versions, presets and templates included.
- **Mock mode** — run the entire game (capacity, timers, messages, leaderboard)
  with **no hardware at all**; set the virtual pump's calibration on the Devices tab.

Fully templated messages via `[placeholders]` — `[capacity]`, `[capacity_bar]`,
`[dice]`, `[result]`, `[secs2capacity]`, `[timer]`, `[commands]`, `[winner]`,
`[cmd_remain]`, `[operator]`, per-line `[if name]` conditionals, `[!command]`
inline fires, and more (full table in Help → Placeholders).

---

## Supported smart plugs

| Brand | How | Status |
|---|---|---|
| **Kasa** (TP-Link, legacy local) | Local UDP/TCP 9999, no account | ✅ verified |
| **Home Assistant** | REST + long-lived token (recommended path for Tapo) | ported |
| **Tuya / Geeni** | Tuya Cloud OpenAPI | ported |
| **Govee** | Govee Developer Cloud API | ported |
| **Wyze** | Wyze cloud login | ported |
| **Tapo** (TP-Link, KLAP) | Local KLAP handshake — fragile; prefer Home Assistant | ported |
| **Kauf** (ESPHome) | Local ESPHome web-server REST (`web_server`), no account | ported |

Only **Kasa** is verified against real hardware. The others are faithful ports of
their documented protocols but **unverified** — a user with that hardware confirms
them. Each device is a **pump** (drives capacity) or **other** (just fires).

> **Recommended hardware:** for the least hassle, get a **Kasa HS103** — local, no
> account, and verified. **Do not update its firmware** — newer firmware can break
> local control. The Devices tab links straight to it.

**Calibration:** on the Devices tab, hit **Calibrate**, let the pump run until
full, and tap **Full** — the elapsed seconds become that device's
`seconds-to-100%`.

---

## Run it (desktop)

```bash
git clone https://github.com/Airegasm/DiscoFlate.git
cd DiscoFlate
./start.sh          # Linux/macOS — creates the venv on first run, then serves
                    # → http://127.0.0.1:8765   (Ctrl-C to stop)
```

Windows: run **`start.bat`** (needs Python 3 with "Add to PATH" ticked).

The control panel is **loopback-only** (127.0.0.1). Everything is configured
there — no `.env`.

### First-time setup

1. **Create your bot** at the [Developer Portal](https://discord.com/developers/applications):
   **Bot** → Reset Token; turn **Message Content Intent** ON; set **Public Bot
   OFF** (keep it private). Invite it to your server with *Send Messages*.
2. **Discord tab** → paste your token → *Save & connect* → pick the server +
   channel to listen on.
3. **Devices tab** → Discover (Kasa) or add a device, pick the **active** pump,
   and **Calibrate** it.
4. Flip **Activation** on. Test with the dashboard **Controls**.

> No verification or app review is needed for a personal bot — that only applies
> at 100+ servers. Each person runs their own bot with their own token; never
> share a token.

---

## Android app

DiscoFlate runs as a real installable Android app via **Chaquopy** — the Python
backend (bot + server + device control) is embedded in the APK and runs
in-process. One install, one tap, no Termux.

```
android-proof/        # Android Studio (Gradle) project
```

Build the release APK (needs Android SDK + a full JDK 17 with `jlink`, plus the
release keystore — `discoflate-release.keystore` + `keystore.properties`, which
are **never committed**; back yours up):

```bash
scripts/sync-android.sh          # mirror the desktop sources into the app
cd android-proof
./gradlew :app:assembleRelease "-Dorg.gradle.java.home=$JDK17_HOME"
# → app/build/outputs/apk/release/app-release.apk
```

A Wi-Fi `MulticastLock` handles Kasa discovery on Android; a foreground service
keeps the bot alive and forces devices **off** if the app is stopped or swiped
away.

---

## Updates

> **Cloned before Sept 2026?** The repo's history was rewritten (old APK blobs
> purged — it shrank from ~1 GB to ~20 MB), so a plain `git pull` on an old
> clone refuses. One-time fix from your DiscoFlate folder (your `data/` —
> token, config, leaderboard — is untouched):
> `curl -fsSL https://raw.githubusercontent.com/Airegasm/DiscoFlate/main/scripts/fix-clone.sh | bash`
>
> Or download the fixer, drop it in the DiscoFlate folder, and run it:
> [**fix-clone.bat** (Windows)](https://github.com/Airegasm/DiscoFlate/releases/download/v3.9.0/fix-clone.bat) ·
> [**fix-clone.sh** (Linux/Mac)](https://github.com/Airegasm/DiscoFlate/releases/download/v3.9.0/fix-clone.sh).
> Either is just `git fetch origin && git reset --hard origin/main`.

The app checks for a newer release on launch and shows a banner on the Dashboard.
**Help → Updates** applies it: on desktop it `git pull`s the latest code (then
restart DiscoFlate); on the phone it opens the new APK's download link in your
browser — install it from there. Since v3.7.0 every APK is release-signed with
the same key, so the phone updates **in place** and keeps your config and token.
(Coming from v3.6.3 or older debug-signed builds: one-time uninstall/reinstall —
export your config first via **Help → Export config**.) APKs are published on
[GitHub Releases](https://github.com/Airegasm/DiscoFlate/releases).

---

## Project layout

| Path | Role |
|---|---|
| `app.py` | entry: capacity engine + aiohttp web UI/API + Discord bot in one asyncio loop |
| `engine.py` | capacity, the action-block system, dice-by-range, firing, events, competitions, prizes, tracking |
| `discord_bot.py` | the bot: command dispatch, broadcasts, operator controls |
| `device_control.py` | vendor-agnostic on/off/discovery router |
| `vendors/` | per-brand drivers (kasa is `kasa_legacy.py`) |
| `kasa_legacy.py` | legacy Kasa driver (UDP/TCP 9999, XOR autokey cipher) |
| `config_store.py` | `data/config.json` (atomic + fsync, 0600 — holds your token; git-ignored; auto-backups in `data/backups/`) |
| `web/index.html` | the control-surface GUI |
| `android-proof/` | the Chaquopy Android app |

Your token and runtime state live in `data/` (git-ignored) — it's recreated on
first run.

---

## Safety & legitimate use

DiscoFlate controls **your own** smart plugs on **your own** LAN via **your own**
bot. It's built for consenting adult roleplay. Physical devices are involved:
the app forces the active device **off** on clean shutdown and (on Android) when
the app is torn down, but you should always have a physical power switch as a
backstop.
