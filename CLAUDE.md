# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

ShiftMind — AR-glasses shared-memory retrieval system for retail workers. Rokid Max Pro glasses surface a HUD "memory card" when a returning customer is recognized, plus live transcript triggers (product mentions, intent) during the interaction.

**Status:** repo now contains the glasses APK and laptop server modules. Keep
the vault architecture docs as source of truth when changing architecture.

## Canonical design docs (read these before changing architecture)

Source-of-truth specs live outside this repo, in the user's Obsidian vault:

- `/Users/edutjie/Documents/obsidian/Myaku/Projects/ShiftMind/architecture_v2.md` — **current architecture.** Endpoints, glasses + server module layout, data flow, latency budget, 10-phase build plan, edge cases.
- `/Users/edutjie/Documents/obsidian/Myaku/Projects/ShiftMind/rokid_android_dev_guide.md` — Rokid-specific Android knowledge (CXR SDK, Camera2 quirks, prism mirror flag, recording broadcasts, foreground services, gotchas).
- `/Users/edutjie/Documents/obsidian/Myaku/Projects/ShiftMind/CLAUDE.md` — vault-side dashboard, points at v2 and next actions.
- `android_app_backbone_decision.md` (same dir) — **archived v1** two-app phone↔glasses plan. Superseded. Do not implement.

When implementation deviates from the spec, update `architecture_v2.md` in the vault, not just this file.

## Architecture (v2, locked)

Two modules:

1. **Glasses APK** (`shiftmind-glasses/`) — single-module Kotlin/Compose Android app on Rokid Max Pro. Handles I/O: raw Camera2 preview/recording, ML Kit on-device face detection, AudioRecord, Compose HUD, Rokid screenstream broadcasts, foreground service. No on-device ML beyond face *detection* (not recognition).
2. **Laptop server** (`shiftmind-server/`) — Python FastAPI on developer laptop. Handles compute: insightface face recognition, FAISS top-1 retrieval, YOLOv8, Whisper STT, trigger classifier, VLM relay, SQLite customer DB.

Transport: **plain HTTP over local WiFi.** No phone middleware, no CXR SDK, no BLE/BT SPP. Seven endpoints (`/api/detect-face`, `/api/first-seen`, `/api/transcribe`, `/api/analyze-transcript`, `/api/chat`, `/api/store-conversation`, `/api/health`).

Critical latency targets: face→HUD ~204 ms (achievable), audio→transcript ~1650 ms (borderline — may need streaming ASR like Deepgram or faster-whisper).

## Non-obvious constraints

- **Rokid Max Pro prism does NOT mirror.** The `rokid_android_dev_guide.md` recommends `graphicsLayer { scaleX = -1f }` on the HUD root for prism mirror. Tested on real Max Pro hardware: the flag produces *reversed* text. Removed for Max Pro. If a different Rokid model is added later, gate on `Build.MODEL` rather than restoring the flag globally.
- **CXR SDK NPE.** If the CXR SDK is ever added, guard callback (un)registration with a boolean — otherwise NPE on lifecycle teardown. Pattern in `rokid_android_dev_guide.md` §2.
- **`mix_record` broadcast** (external HUD+camera fused recording): `com.rokid.os.master.assist.server.cmd` + `cmd_type=control_scene` + `scene=mix_record` + `open=true|false`. Firmware-dependent. **No longer used** — ShiftMind has its own built-in recorder (phase 8.0 — `recording/ArRecorder.kt` + Camera2 multi-output session + OpenGL ES HUD compositor + MediaCodec H.264 → MediaMuxer MP4 saved to `Movies/ShiftMind/`). External recorders would conflict with our camera ownership; `RecordingStateReceiver` still listens for diagnostic logging only.
- **HUD recording uses Rokid's screenstream broadcast.** `RokidHudRecorder` (in `recording/`) broadcasts `com.rokid.yodaos.action.SCREENRECORD_ON/_OFF` to `com.rokid.os.master.screenstream` (the vendor service that records the display). **`intent.setPackage("com.rokid.os.master.screenstream")` is REQUIRED** — without it the broadcast is silently dropped (`adb shell am broadcast` returns `result=0`, no logs, no file). Confirm by waiting for the echo `SCREENRECORD_START` broadcast (~200ms after request) in logcat. HUD MP4 lands in `/sdcard/ScreenRecorder/ScreenRecorder_<ts>.mp4` named by screenstream's own clock — `/pull-videos` pairs it with the cam-side MP4 and auto-creates `shiftmind_<ts>_hud_keyed_opacity65.mp4`.
- **Camera2 multi-output capture session.** The same Camera2 session can have multiple output Surfaces (ImageReader + SurfaceTexture). `CameraPreviewStreamer.attachRecordingTarget(surface)` triggers session recreation with both outputs; `detachRecordingTarget()` reverts. Face-detect pipeline keeps running while recording. Look for `outputs=2` in `ShiftMind.Camera` log to confirm session reconfigured — missing = recording will have no video frames.
- **SurfaceTexture.setDefaultBufferSize must match the preview camera stream.** Use the same raw `640x480` camera buffer as preview (`cameraBufferWidth=640, cameraBufferHeight=480`) with a `480x640` encoder surface and `extraRotationDeg=0`. Do not request a portrait stream from Camera2 and do not add GL rotation; both can crop/scale differently from preview. HUD recording is a separate screenstream MP4.
- **Shared PTS anchor between audio + video encoders.** Video PTS comes from `SurfaceTexture.timestamp` (raw monotonic-nanos uptime). Audio PTS comes from `System.nanoTime() - startEpochNs` (delta-nanos). Without a common anchor, MP4 duration is wildly wrong and audio/video desync. Fix: `HudCompositor.setPtsAnchor(anchorNs)` rebases video PTS to the same `System.nanoTime()` value passed to `AudioEncoder.start(anchorNs)`. Both encoders then produce timestamps from the same zero.
- **AudioRecord source on Rokid HAL — empirical findings, do NOT re-derive from theory.** Tested on Rokid RG-glasses while Camera2 session is open: (1) `MIC` works, ~-19dB peaks for spoken voice (×4 software gain in `AudioEncoder` brings mean to ~-27dB, max ~-2dB — Whisper-friendly); (2) `VOICE_RECOGNITION` returns digital silence (hot-word DSP path, ~-91dB); (3) `CAMCORDER` and `UNPROCESSED` probes wedge the Camera2 session — touching them while camera is open kills `attachRecordingTarget` so the capture session never reconfigures to `outputs=2`, producing a few-KB MP4 with no video frames. Use `MIC` unconditionally. If you must probe alternates, do it during app start before camera ever opens.
- **MediaMuxer must wait for both tracks before start.** With audio enabled, `RecordingMuxer` registers each track on `INFO_OUTPUT_FORMAT_CHANGED` then starts the muxer when both tracks are present. The video track's format change only fires after the encoder processes at least one frame — so if the camera session never reconfigures to outputs=2, video format never arrives, muxer never starts, and audio data piles up undrained until stop EOS.
- **GLSL `#extension` directive position.** Extensions must come BEFORE `precision mediump float;` — putting them in the body after precision causes "requires extension GL_OES_EGL_image_external" compile failure on Adreno GPUs. See `recording/GlSupport.kt`.
- **`FloatBuffer.wrap()` is heap-backed.** GLES20 needs DIRECT buffers — use `ByteBuffer.allocateDirect(...).order(ByteOrder.nativeOrder()).asFloatBuffer()`. Heap-backed wraps throw `glVertexAttribPointerBounds` at draw time.
- **HudState is a single source of truth for the HUD content.** `HudScreen.kt` consumes it for the prism HUD, and Rokid screenstream captures that same display directly. Do not add a second Canvas/video HUD renderer; it will drift from the prism.
- **Recording rotation:** cam MP4 is `480x640` portrait with `extraRotationDeg=0`, matching the server live preview while keeping the camera input raw `640x480`. If a future Rokid model reports a different orientation, override `shiftmind.recordingRotation` in `local.properties` only after verifying the pulled MP4.
- **Camera2 vs Rokid SDK camera.** Rokid SDK camera is single-shot only. For continuous preview (used by `CameraPreviewStreamer`) use raw Camera2 (`ImageReader`). CameraX does NOT work on this firmware — see next bullet.
- **CameraX is broken on Rokid RG-glasses; use raw Camera2.** The HAL exposes one camera (id `"0"`) with `LENS_FACING=null` to CameraX's wrapper. `CameraSelector.DEFAULT_BACK_CAMERA` matches zero cameras → analyzer never fires (silently). Raw Camera2 with `manager.openCamera("0", ...)` works — don't filter by lens facing. Phase 1.5 ships with Camera2.
- **Rokid prism never reports `state ON` to AOSP.** `dumpsys display` shows `state OFF` even when the prism is lit, so the framework treats the activity as sleeping (proc state 20 = NONEXISTENT) and `CameraService` rejects `openCamera` with `CAMERA_DISABLED`. Fix in `MainActivity.onCreate`: `setShowWhenLocked(true)` + `setTurnScreenOn(true)` + `FLAG_KEEP_SCREEN_ON` + `KeyguardManager.requestDismissKeyguard()` + a `PowerManager.SCREEN_BRIGHT_WAKE_LOCK or ACQUIRE_CAUSES_WAKEUP` (deprecated but only API that flips framework display state on). Manual sanity check: `adb shell input keyevent KEYCODE_WAKEUP` should reproduce the workaround instantly.
- **Foreground service must start from Activity, not Application.** Android 12+ rejects `startForegroundService()` from `Application.onCreate` as a background-context start (logs show `Background start not allowed: ... startFg?=true` even though `Background started FGS: Allowed` appears first — the first line is misleading). Service ends up with `app=null` and `startForegroundCount=0` in `dumpsys`. Start from `MainActivity.onCreate` instead.
- **FGS type=`camera` requires CAMERA perm at startForeground() time.** Android 14 strict. For phase 1 the service passes only `FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE` to `startForeground()` — the activity owns camera access while foreground. When audio capture lands (phase 5+) the service can OR in `MICROPHONE` after the perm grant.
- **Dev networking via `adb reverse tcp:8000 tcp:8000`.** Glasses on USB can reach laptop server without shared WiFi by tunneling through adb. Set `shiftmind.serverUrl=http://127.0.0.1:8000` in `local.properties`. Re-run `adb reverse` after `adb kill-server`. For deployment we'll need actual WiFi.
- **Video files stay on glasses.** Recordings are not uploaded over HTTP (too large). Pull manually from MediaStore `%ScreenRecorder%`.
- **Server URL is build-time** (`BuildConfig.SERVER_URL` in `app/build.gradle.kts`). Phase-1 simplification — phase 4+ should move it to a runtime setting so users can change networks without rebuilding.
- **Session memory contract.** Keep per-session summaries in
  `customer_sessions.summary` / `important_points_json`; keep the cumulative HUD
  card in `customers.memory_card`. Update the cumulative card by summarizing and
  merging facts, not by appending raw transcripts forever.
- **Stored memory vs live transcript are separate HUD fields.** `memoryLine` is
  long-lived customer memory; `liveTranscript` is current speech. Do not route
  live ASR text through `memoryLine`, or the HUD flickers between memory and
  transcript.
- **LLM summarization uses OpenAI-compatible chat completions JSON output.** Do
  not swap back to an unverified Responses API call unless the SDK usage is
  tested. Missing LLM credentials should degrade to bounded fallback summaries.

## Repo licensing (do not violate)

| Repo | License | Use |
| :--- | :--- | :--- |
| zero2005x/RokidAIAssistant | **proprietary** | Study only, no fork, no copy. |
| Anezium/OverlayRec | GPL-3.0 | Viral. Reuse intent string *facts* only — never copy source files. |
| zero2005x/M365-Rokid-HUD | **MIT** | Forkable. Useful for HudScreen composable, foreground service pattern, Rokid Maven repo config, permission declarations. |

## Commands

Use these module-local commands:

**Glasses APK** (Android Studio project under `shiftmind-glasses/`):
- `./gradlew :app:assembleDebug` — build debug APK
- `./gradlew :app:installDebug` — sideload to connected glasses (`adb devices` first)
- `./gradlew :app:test` — unit tests
- `./gradlew :app:connectedAndroidTest` — instrumented tests on device
- `adb logcat -s ShiftMind:V` — filter logs

**Laptop server** (`shiftmind-server/`):
- `uv sync` — install deps from `pyproject.toml` / `uv.lock`
- `./run.sh` — set up `adb reverse` and run uvicorn
- `NO_ADB=1 ./run.sh` — run over WiFi or without connected glasses
- `uv run pytest` — run server tests
- `RUN_LIVE_SESSION_TEST=1 uv run pytest tests/test_live_session.py` — opt-in live test, requires running server and active glasses session

## Build order (per architecture_v2.md §"Priority order to build")

10 phases, ~15 dev-days total. Phase 1 = scaffold + HUD. Phase 2 = raw Camera2 + ML Kit face detection. Phase 3 = OkHttp + FastAPI wired end-to-end. Do not skip phase order — each phase's deliverable de-risks the next.

## What NOT to do

- Don't reopen the v1 vs v2 architecture debate. User confirmed v2.
- Don't add a phone app to MVP. Laptop is the backend.
- Don't fork RokidAIAssistant (proprietary).
- Don't copy OverlayRec source files (GPL-3.0 viral) — only reuse intent string facts.
- Don't add CXR SDK / BLE / BT SPP transports. Plain HTTP only.
