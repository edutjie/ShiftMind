# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

ShiftMind — AR-glasses shared-memory retrieval system for retail workers. Rokid Max Pro glasses surface a HUD "memory card" when a returning customer is recognized, plus live transcript triggers (product mentions, intent) during the interaction.

**Status:** repo is empty. Next step is bootstrapping the two modules described under Architecture below.

## Canonical design docs (read these before changing architecture)

Source-of-truth specs live outside this repo, in the user's Obsidian vault:

- `/Users/edutjie/Documents/obsidian/Myaku/Projects/ShiftMind/architecture_v2.md` — **current architecture.** Endpoints, glasses + server module layout, data flow, latency budget, 10-phase build plan, edge cases.
- `/Users/edutjie/Documents/obsidian/Myaku/Projects/ShiftMind/rokid_android_dev_guide.md` — Rokid-specific Android knowledge (CXR SDK, Camera2 quirks, prism mirror flag, recording broadcasts, foreground services, gotchas).
- `/Users/edutjie/Documents/obsidian/Myaku/Projects/ShiftMind/CLAUDE.md` — vault-side dashboard, points at v2 and next actions.
- `android_app_backbone_decision.md` (same dir) — **archived v1** two-app phone↔glasses plan. Superseded. Do not implement.

When implementation deviates from the spec, update `architecture_v2.md` in the vault, not just this file.

## Architecture (v2, locked)

Two modules:

1. **Glasses APK** (`shiftmind-glasses/`) — single-module Kotlin/Compose Android app on Rokid Max Pro. Handles I/O: Camera2/CameraX preview, ML Kit on-device face detection, AudioRecord, Compose HUD, Rokid recording broadcasts, foreground service. No on-device ML beyond face *detection* (not recognition).
2. **Laptop server** (`shiftmind-server/`) — Python FastAPI on developer laptop. Handles compute: insightface face recognition, FAISS top-1 retrieval, YOLOv8, Whisper STT, trigger classifier, VLM relay, SQLite customer DB.

Transport: **plain HTTP over local WiFi.** No phone middleware, no CXR SDK, no BLE/BT SPP. Seven endpoints (`/api/detect-face`, `/api/first-seen`, `/api/transcribe`, `/api/analyze-transcript`, `/api/chat`, `/api/store-conversation`, `/api/health`).

Critical latency targets: face→HUD ~204 ms (achievable), audio→transcript ~1650 ms (borderline — may need streaming ASR like Deepgram or faster-whisper).

## Non-obvious constraints

- **Rokid Max Pro prism does NOT mirror.** The `rokid_android_dev_guide.md` recommends `graphicsLayer { scaleX = -1f }` on the HUD root for prism mirror. Tested on real Max Pro hardware: the flag produces *reversed* text. Removed for Max Pro. If a different Rokid model is added later, gate on `Build.MODEL` rather than restoring the flag globally.
- **CXR SDK NPE.** If the CXR SDK is ever added, guard callback (un)registration with a boolean — otherwise NPE on lifecycle teardown. Pattern in `rokid_android_dev_guide.md` §2.
- **`mix_record` broadcast** (HUD+camera fused recording): `com.rokid.os.master.assist.server.cmd` + `cmd_type=control_scene` + `scene=mix_record` + `open=true|false`. Firmware-dependent — validate on real hardware before relying on it.
- **Camera2 vs Rokid SDK camera.** Rokid SDK camera is single-shot only. For continuous preview (used by `CameraPreviewStreamer`) use raw Camera2 (`ImageReader`). CameraX does NOT work on this firmware — see next bullet.
- **CameraX is broken on Rokid RG-glasses; use raw Camera2.** The HAL exposes one camera (id `"0"`) with `LENS_FACING=null` to CameraX's wrapper. `CameraSelector.DEFAULT_BACK_CAMERA` matches zero cameras → analyzer never fires (silently). Raw Camera2 with `manager.openCamera("0", ...)` works — don't filter by lens facing. Phase 1.5 ships with Camera2.
- **Rokid prism never reports `state ON` to AOSP.** `dumpsys display` shows `state OFF` even when the prism is lit, so the framework treats the activity as sleeping (proc state 20 = NONEXISTENT) and `CameraService` rejects `openCamera` with `CAMERA_DISABLED`. Fix in `MainActivity.onCreate`: `setShowWhenLocked(true)` + `setTurnScreenOn(true)` + `FLAG_KEEP_SCREEN_ON` + `KeyguardManager.requestDismissKeyguard()` + a `PowerManager.SCREEN_BRIGHT_WAKE_LOCK or ACQUIRE_CAUSES_WAKEUP` (deprecated but only API that flips framework display state on). Manual sanity check: `adb shell input keyevent KEYCODE_WAKEUP` should reproduce the workaround instantly.
- **Foreground service must start from Activity, not Application.** Android 12+ rejects `startForegroundService()` from `Application.onCreate` as a background-context start (logs show `Background start not allowed: ... startFg?=true` even though `Background started FGS: Allowed` appears first — the first line is misleading). Service ends up with `app=null` and `startForegroundCount=0` in `dumpsys`. Start from `MainActivity.onCreate` instead.
- **FGS type=`camera` requires CAMERA perm at startForeground() time.** Android 14 strict. For phase 1 the service passes only `FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE` to `startForeground()` — the activity owns camera access while foreground. When audio capture lands (phase 5+) the service can OR in `MICROPHONE` after the perm grant.
- **Dev networking via `adb reverse tcp:8000 tcp:8000`.** Glasses on USB can reach laptop server without shared WiFi by tunneling through adb. Set `shiftmind.serverUrl=http://127.0.0.1:8000` in `local.properties`. Re-run `adb reverse` after `adb kill-server`. For deployment we'll need actual WiFi.
- **Video files stay on glasses.** Recordings are not uploaded over HTTP (too large). Pull manually from MediaStore `%ScreenRecorder%`.
- **Server URL is build-time** (`BuildConfig.SERVER_URL` in `app/build.gradle.kts`). Phase-1 simplification — phase 4+ should move it to a runtime setting so users can change networks without rebuilding.

## Repo licensing (do not violate)

| Repo | License | Use |
| :--- | :--- | :--- |
| zero2005x/RokidAIAssistant | **proprietary** | Study only, no fork, no copy. |
| Anezium/OverlayRec | GPL-3.0 | Viral. Reuse intent string *facts* only — never copy source files. |
| zero2005x/M365-Rokid-HUD | **MIT** | Forkable. Useful for HudScreen composable, foreground service pattern, Rokid Maven repo config, permission declarations. |

## Commands

No build system yet — repo is empty. Once the modules exist:

**Glasses APK** (Android Studio project under `shiftmind-glasses/`):
- `./gradlew :app:assembleDebug` — build debug APK
- `./gradlew :app:installDebug` — sideload to connected glasses (`adb devices` first)
- `./gradlew :app:test` — unit tests
- `./gradlew :app:connectedAndroidTest` — instrumented tests on device
- `adb logcat -s ShiftMind:V` — filter logs

**Laptop server** (`shiftmind-server/`):
- `uvicorn server:app --host 0.0.0.0 --port 8000 --reload` — dev server (bind 0.0.0.0 so glasses on same WiFi can reach it)
- `pip install -r requirements.txt` — install deps
- `pytest` — run tests

Update this section once the actual scripts exist.

## Build order (per architecture_v2.md §"Priority order to build")

10 phases, ~15 dev-days total. Phase 1 = scaffold + HUD with prism mirror. Phase 2 = CameraX + ML Kit face detection. Phase 3 = OkHttp + FastAPI wired end-to-end. Do not skip phase order — each phase's deliverable de-risks the next.

## What NOT to do

- Don't reopen the v1 vs v2 architecture debate. User confirmed v2.
- Don't add a phone app to MVP. Laptop is the backend.
- Don't fork RokidAIAssistant (proprietary).
- Don't copy OverlayRec source files (GPL-3.0 viral) — only reuse intent string facts.
- Don't add CXR SDK / BLE / BT SPP transports. Plain HTTP only.
