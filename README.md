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

Players type a command (default `!agroll`) in Discord. The bot rolls that
capacity range's dice, fires the active smart plug for the rolled number of
seconds, and a **capacity meter (0–100%+)** climbs based on the device's
calibration. As capacity rises it moves through **ranges**, each with its own
dice, cooldowns, announcements, custom commands, images, and one-time milestones.

- **Capacity engine** — capacity accumulates while the active pump runs, at a
  rate set by that device's `seconds-to-100%` calibration.
- **Per-range everything** — dice (`N`d`sides`), a luck modifier, per-command
  cooldowns (per-user or shared), custom commands with per-person use budgets,
  ongoing announce text, one-time milestone messages + images.
- **Custom commands** — build `fire` / `roll` / `say` commands in the UI; add
  them to whichever ranges they should be active in.
- **Max-roll prizes** — hitting a perfect roll N times unlocks limited-use bonus
  commands, range-gated or global, with a cumulative counter.
- **End sequences** — let capacity run past 100% to a final burst threshold with
  its own entry milestone and final message/image.
- **Timed events, modes, leaderboard, per-user cooldowns, anti-spam buffer,**
  multi-server support with anonymized cross-server echoes.
- **Snapshot** — capture a webcam frame and post it to the channel.
- **Operator Controls** — dashboard buttons (Roll, Stop, Pump, Broadcast
  Capacity/Users) that fire real in-channel actions *as the owner*, exactly as if
  they'd typed the command. The **owner name** is set right at the top of the Dashboard.
- **Mock mode** — run the entire game (capacity, timers, messages, leaderboard)
  with **no hardware at all**; set the virtual pump's calibration on the Devices tab.

Fully templated messages via `[placeholders]` — `[capacity]`, `[capacity_bar]`,
`[dice]`, `[result]`, `[secs2capacity]`, `[timer]`, `[commands]`,
`[custom_commands]`, `[cmd_remain]`, `[operator]`, and more.

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
| `engine.py` | capacity, dice-by-range, firing, milestones, cooldowns, prizes, tracking |
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
