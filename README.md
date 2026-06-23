# ShiftMind

AR-glasses shared-memory retrieval system for retail workers. Rokid AR glasses surface a HUD "memory card" when a returning customer is recognized, plus live transcript triggers (product mentions, intent) during the interaction.

**Status:** HUD on glasses, raw Camera2 live preview, built-in cam+audio recording, Rokid screenstream HUD recording, and `/pull-videos` post-processing into combined review MP4s.

## Repository layout

| Path | What |
| :--- | :--- |
| `shiftmind-glasses/` | Kotlin/Compose Android app for Rokid glasses (`RG-glasses` / Max Pro). Single-module Gradle project. |
| `shiftmind-server/` | Python FastAPI laptop server. `uv`-managed. |
| `CLAUDE.md` | Guidance for Claude Code agents working in this repo. |

Canonical design docs live outside this repo, in the user's Obsidian vault: `/Users/edutjie/Documents/obsidian/Myaku/Projects/ShiftMind/architecture_v2.md`.

## Quick start — full pipeline (glasses → laptop preview)

You need: Rokid glasses + USB-C cable to laptop, Android Studio or system Gradle 8.x, `adb` on PATH, `uv` for Python.

### 1. Start the server (terminal 1)

```bash
cd shiftmind-server
uv sync                                                       # first time only
uv run uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

### 2. Bridge the USB port to localhost (terminal 2)

The Rokid glasses don't share your laptop's WiFi by default, so glasses can't hit `localhost:8000` directly. `adb reverse` tunnels their `127.0.0.1:8000` over USB back to the laptop's port 8000.

```bash
adb devices                            # confirm glasses show up (model:RG_glasses)
adb reverse tcp:8000 tcp:8000          # set up the tunnel (per adb-server lifetime)
adb reverse --list                     # verify
```

> Re-run `adb reverse` after `adb kill-server`, after unplug/replug, or after restarting Android Studio.

### 3. Configure the glasses build (one-time)

`local.properties` is git-ignored. The default in `local.properties.example` points at `http://127.0.0.1:8000` which is what you want when using the USB tunnel.

```bash
cd shiftmind-glasses
cp local.properties.example local.properties   # if it doesn't exist yet
# verify: `shiftmind.serverUrl=http://127.0.0.1:8000`
```

If you'd rather use WiFi (glasses + laptop on the same network), put the laptop's LAN IP in `local.properties` and skip `adb reverse`. See `shiftmind-glasses/README.md` for details.

### 4. Build, install, launch (terminal 2)

```bash
cd shiftmind-glasses
./gradlew :app:installDebug
adb shell am start -n com.shiftmind.glasses/.MainActivity
```

Grant the CAMERA permission when prompted (first launch only).

### 5. Watch the live feed

Open `http://localhost:8000/api/preview.html` in a browser. You should see live glasses POV at ~10 fps (rotated 90° clockwise to compensate for the 270° sensor mount).

```bash
# verify frames are arriving
curl http://localhost:8000/api/preview-status
# → {"have_frame":true,"seq":N,"age_seconds":<small>}
```

If the page shows the "no frames yet" placeholder, follow the troubleshooting section in `shiftmind-glasses/README.md`.

## No-hardware path (server-only smoke test)

You can validate the server pipeline without glasses by feeding the laptop webcam:

```bash
# terminal 1
cd shiftmind-server && uv run uvicorn server:app --host 0.0.0.0 --port 8000 --reload

# terminal 2
cd shiftmind-server
uv run python tools/feed_webcam.py
open http://localhost:8000/api/preview.html
```

## Architecture

Two processes, plain HTTP, no phone middleware. Dev transport is USB-via-`adb reverse`; production is plain WiFi.

```
┌──────────────────────────┐    POST /api/detect-face    ┌──────────────────────────┐
│ Rokid glasses APK        │ ─────────────────────────→  │ Laptop FastAPI server    │
│                          │ ←─── customer + memory ──── │                          │
│ Camera2 + ML Kit face    │                             │ insightface + FAISS      │
│ Compose HUD              │    POST /api/transcribe     │ Whisper STT              │
│ AudioRecord              │ ─────────────────────────→  │ Trigger classifier       │
│ Foreground service       │    POST /api/chat           │ VLM relay                │
│ cam+HUD recording        │                             │ SQLite customer DB       │
└──────────────────────────┘                             └──────────────────────────┘
```

See `CLAUDE.md` for design constraints + gotchas, `shiftmind-glasses/README.md` and `shiftmind-server/README.md` for module-specific details.

## Still Pending

Glasses: ML Kit face detection and production network settings.
Server: real face recognition (insightface/FAISS), Whisper STT, trigger classifier, VLM relay, SQLite persistence.

Phase order and per-phase deliverables: `architecture_v2.md` §"Priority order to build" in the vault.
