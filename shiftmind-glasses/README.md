# shiftmind-glasses

Single-module Kotlin/Compose Android app for Rokid AR glasses (`RG-glasses` / Max Pro). Production features: ML Kit on-device face detection with cursor overlap + frontal filtering, Camera2 multi-output recording (cam+audio + HUD), four-mic array channel selection for balanced wearer/customer speech, Doubao streaming ASR, automatic customer session lifecycle, and Q&A mode (swipe-forward gesture) with conversation history for business intelligence queries.

## Prerequisites

- Android Studio (latest stable) or Gradle 8.x
- Rokid glasses + USB-C cable
- `adb` on PATH (`brew install android-platform-tools` on macOS)
- Laptop server running (see `../shiftmind-server/README.md`)

## First-time setup

The Gradle wrapper jar is not checked in (binary). Generate it once:

**Option A — Android Studio:** open this directory as a project. Android Studio runs Gradle sync and generates `gradlew` automatically.

**Option B — system Gradle:** if you have Gradle 8.x installed (`brew install gradle`):
```bash
cd shiftmind-glasses
gradle wrapper --gradle-version 8.11.1
```

## Configure the server URL

Per-developer config lives in `local.properties` at the module root. Git-ignored (Android-standard). Copy the template:

```bash
cd shiftmind-glasses
cp local.properties.example local.properties   # if it doesn't exist yet
```

Two transport modes — pick one:

### Mode A — USB tunnel (recommended for dev)

Glasses plugged into laptop via USB. Use `adb reverse` to forward laptop port 8000 to the glasses' `localhost:8000`. No shared WiFi needed.

`local.properties`:
```properties
shiftmind.serverUrl=http://127.0.0.1:8000
shiftmind.previewFps=10
```

Set up the tunnel on the laptop (re-run after `adb kill-server`):
```bash
adb reverse tcp:8000 tcp:8000
```

### Mode B — same WiFi

Both laptop and glasses on the same WiFi network. The glasses' host (whatever device adb is talking to) needs WiFi connected.

```bash
# laptop IP
ipconfig getifaddr en0        # macOS
hostname -I | awk '{print $1}'  # linux
```

`local.properties`:
```properties
shiftmind.serverUrl=http://10.0.0.42:8000   # your laptop IP
shiftmind.previewFps=10
```

On the glasses, make sure WiFi is up:
```bash
adb shell dumpsys wifi | grep -E "Wi-Fi is|SSID" | head -5
adb shell ip addr show wlan0 | grep inet
adb shell am start -a android.settings.WIFI_SETTINGS    # opens WiFi UI on prism
```

`app/build.gradle.kts` reads these keys and bakes them into `BuildConfig.SERVER_URL` / `BuildConfig.PREVIEW_FPS` at build time.

> Android Studio also writes `sdk.dir=...` into `local.properties`. Coexists fine — just append the `shiftmind.*` lines.

Plain HTTP — cleartext is allowed via `android:usesCleartextTraffic="true"` in the manifest (dev only).

## Build + sideload

```bash
cd shiftmind-glasses

# verify the glasses are visible
adb devices

# (mode A only) bridge the laptop server
adb reverse tcp:8000 tcp:8000

# build + install in one step
./gradlew :app:installDebug

# launch
adb shell am start -n com.shiftmind.glasses/.MainActivity

# logs (filter to our tags)
adb logcat -s ShiftMind.Main:V ShiftMind.Camera:V ShiftMind.Preview:V *:E
```

Expected on the prism: green "ShiftMind" title at top, corner-bracket box cursor + crosshair at center. The camera-permission dialog appears on first launch; grant it.

Within ~1 second of grant, you should see in logcat:
```
ShiftMind.Camera: opening Camera2 id=0; streaming 10fps -> http://127.0.0.1:8000/api/preview-frame
ShiftMind.Camera: capture session configured; repeating preview active
```

Confirm frames land on the server: open `http://localhost:8000/api/preview.html` in a browser — you should see the live glasses POV.

## Architecture in this module

| File | Role |
| :--- | :--- |
| `GlassesApplication.kt` | Empty `Application` subclass. **Does NOT** start the foreground service — Android 12+ rejects FGS starts from `Application.onCreate`. |
| `MainActivity.kt` | Hosts the Compose HUD; requests CAMERA permission; starts `GlassesService` + screen-wake lock + `CameraPreviewStreamer`. `FLAG_KEEP_SCREEN_ON` + `setTurnScreenOn` + `SCREEN_BRIGHT_WAKE_LOCK \| ACQUIRE_CAUSES_WAKEUP` so the Rokid prism reports display-on to AOSP. Handles touchpad gestures (swipe-forward → Q&A mode toggle, single-tap → record/ask). Polls `/api/session-status` for HUD state updates. |
| `service/GlassesService.kt` | Foreground service: notification + wake lock. Passes only `FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE` to `startForeground` (Android 14 strict — `CAMERA` type would require perm grant at the exact moment of `startForeground`). |
| `hud/HudScreen.kt` | Title at TopCenter, BoxCursor at viewport center, customer card (memory + live transcript), Q&A screen. No prism mirror flag (see note below). |
| `hud/HudStateHolder.kt` | Global HUD state (scanning/confirmed/active session). Consumed by prism display and Rokid screenstream. |
| `hud/BoxCursor.kt` | Center face slot — 240dp, corner brackets + center crosshair. |
| `ui/theme/Theme.kt` | Bright-green-on-black dark scheme. Material3 light scheme washes out on AR optics. |
| `camera/CameraPreviewStreamer.kt` | Raw Camera2 (`CameraManager.openCamera("0", ...)`) → `ImageReader` YUV_420_888 @ 640×480 → NV21 → `YuvImage.compressToJpeg` → `PreviewClient.postFrame`. Throttled to `BuildConfig.PREVIEW_FPS`. Self-retries with backoff (500ms → 5s) on `CAMERA_IN_USE` / session-configure failures so the AR recorder can hand back the sensor cleanly. Integrates `FaceGateDetector` for on-device ML Kit face detection. |
| `camera/FaceGateDetector.kt` | ML Kit face detection with cursor overlap (35% threshold), frontal face filtering (max yaw ±25°), minimum-motion frame selection (luma diff), temporal stability (tolerates 3 missed detections). Produces JPEG crop with 28% padding for server recognition. |
| `net/PreviewClient.kt` | OkHttp wrapper for POST `/api/preview-frame`. Aggressive timeouts (1.5s connect, 2s write/read). Never throws — failures logged + counted. |
| `net/SessionClient.kt` | OkHttp wrapper for POST `/api/session/{id}/finish`. Uploads WAV audio + optional transcript. |
| `net/SessionAudioStreamClient.kt` | WebSocket client for live ASR during customer sessions. Streams 16kHz PCM → server → Doubao. |
| `session/CustomerSessionHolder.kt` | Global customer session state (session_id, customer name, memory card). Polled from server via `/api/session-status`. |
| `qa/QaController.kt` | Q&A mode state machine (Idle → Recording → Thinking → Answer/Error). Voice capture via selected array channel 4 + Doubao streaming ASR. |
| `qa/QAModeHolder.kt` | Global Q&A mode flag + conversation history (max 20 turns). Clears history on exit. |
| `qa/QAScreen.kt` | Dedicated AR HUD for Q&A mode with live transcript + short answer display. |
| `net/QaClient.kt` | OkHttp wrapper for POST `/api/qa/ask`. Sends question + conversation history for contextual follow-ups. |
| `net/QaAudioStreamClient.kt` | WebSocket client for Q&A voice streaming (decoupled from customer sessions). |
| `recording/ArRecorder.kt` | Orchestrates cam+audio+HUD recording. Uses Camera2 multi-output session (ImageReader + SurfaceTexture). |
| `recording/VideoEncoder.kt` | MediaCodec H.264 encoder. Rebases PTS via shared anchor to sync with audio. |
| `recording/RokidMicCapture.kt` | Shared production configuration: default `AudioSource.MIC`, four-channel input, extract channel 4 to raw 16 kHz mono PCM. |
| `recording/AudioEncoder.kt` | MediaCodec AAC encoder. Uses `RokidMicCapture` channel 4 with no fixed gain for balanced AR-recording audio. |
| `recording/VoiceSessionRecorder.kt` | Voice capture for customer sessions + Q&A. Uses the same selected channel 4 for live Doubao ASR and saved WAV audio. |
| `recording/HudCompositor.kt` | OpenGL ES compositor that reads SurfaceTexture (camera) and renders to encoder surface. `setPtsAnchor()` rebases video PTS to match audio. |
| `recording/RokidHudRecorder.kt` | Broadcasts to Rokid screenstream service for HUD-only MP4. Requires `intent.setPackage("com.rokid.os.master.screenstream")`. |
| `recording/MicConfigProbe.kt` | Diagnostic tool (gated by `BuildConfig.AUDIO_PROBE`). Tests multiple AudioSource + channel mask combinations, logs per-config/per-channel dBFS. Runs at app start before Camera2 opens. |
| `recording/RecordingStateReceiver.kt` | Diagnostic listener for external Rokid `mix_record` and screenstream broadcasts. ShiftMind no longer releases camera for external recorders; it logs conflicts only. |

### Live microphone monitor (debug builds)

Use the laptop-side monitor to audition the Rokid inputs in real time without
running Camera2, AR recording, or live ASR:

```bash
cd shiftmind-glasses
./tools/live-mic.sh back mono
./tools/live-mic.sh bottom mono
./tools/live-mic.sh default quad1  # repeat with quad2, quad3, quad4
```

The script uses `adb reverse` to stream raw 16 kHz mono PCM from the debug-only
`MicMonitorActivity` into `ffplay` on the laptop. `bottom` targets logical input
ID 21, `back` targets ID 23, and `quad1`–`quad4` select one channel from the
four-channel array capture. Set `GAIN_DB=12` only when extra laptop playback
volume is needed; it does not modify captured samples. Press Ctrl-C to stop.

The production setting selected on the real RG-glasses is `default quad4`.
Both live ASR and AR recording use that shared configuration.

## Non-obvious things baked in (don't accidentally undo)

These three are the Rokid quirks discovered during phase 1.5; removing any one breaks the camera pipeline silently (see `../CLAUDE.md` for the diagnostic logs).

- **No prism mirror flag on Rokid Max Pro.** The vault `rokid_android_dev_guide.md` (§5) recommends `graphicsLayer { scaleX = -1f }` on the HUD root. Tested on real RG-glasses hardware: the prism does NOT mirror, so the flag produces *reversed* text. Removed. If a different Rokid model needs it, gate on `Build.MODEL`.
- **Raw Camera2, not CameraX.** The Rokid HAL exposes one camera (id `"0"`) but reports `LENS_FACING=null` to CameraX. `CameraSelector.DEFAULT_BACK_CAMERA` then matches zero cameras — the analyzer silently never fires. We open Camera2 directly by id; don't filter by lens facing.
- **Display-wake forced from `MainActivity.onCreate`.** The Rokid prism doesn't report its wake state back to AOSP, so `dumpsys display` shows `state OFF` even with the prism lit. The activity gets stuck in `isSleeping=true`, proc state stays at `20` (NONEXISTENT), and `CameraService` rejects `openCamera` with `CAMERA_DISABLED`. Fix: `setShowWhenLocked(true)` + `setTurnScreenOn(true)` + `KeyguardManager.requestDismissKeyguard()` + `PowerManager.SCREEN_BRIGHT_WAKE_LOCK or ACQUIRE_CAUSES_WAKEUP` (deprecated but the only API that actually flips framework display state).
- **FGS started from MainActivity, not Application.** Android 12+ rejects `startForegroundService()` from `Application.onCreate` as background-context. Service ends up with `app=null`, `startForegroundCount=0`. Move the start to `MainActivity.onCreate`.
- **FGS type is only `CONNECTED_DEVICE`** at `startForeground()` time. Android 14 strict — adding `CAMERA` or `MICROPHONE` type throws SecurityException unless the matching runtime perm is already granted at that moment. Activity owns the camera while foreground anyway. Add the other types in phase 5+ once audio capture lands.
- **`foregroundServiceType` flags in the manifest** still list all three (`connectedDevice|camera|microphone`) — the actual flag set passed to `startForeground` must be a subset. Manifest stays as-is for future phases.
- **Notification channel created in service `onCreate()`** before first `startForeground()`. Android 14 crashes otherwise.
- **`com.rokid.permission.GLASS_CONTROL`** auto-grants on Rokid firmware, silently ignored elsewhere. Safe to declare.
- **`CHANGE_WIFI_MULTICAST_STATE`** declared up-front for mDNS use in later phases (easy to forget; mDNS fails silently without it).
- **`android:usesCleartextTraffic="true"`** lets the app POST to `http://` (no TLS in dev). Will need a more restrictive `network_security_config.xml` if we ever ship.
- **`buildConfig = true`** in `app/build.gradle.kts` — AGP 8+ disables BuildConfig generation by default; we need it for `SERVER_URL` and `PREVIEW_FPS`.

## Troubleshooting

| Symptom | Diagnosis + fix |
| :--- | :--- |
| Preview shows "no frames yet" placeholder | (a) `adb reverse --list` shows the tunnel (mode A) or glasses can ping laptop (mode B). (b) `curl http://localhost:8000/api/preview-status` from laptop → `have_frame:true`? (c) Camera perm granted: `adb shell dumpsys package com.shiftmind.glasses \| grep CAMERA` → `granted=true`. (d) Check `ShiftMind.Camera` log for `capture session configured`. |
| Logcat: `cannot open camera "0" from background ... proc state 20` | Rokid display-wake bug — see "Display-wake forced" bullet above. Manual smoke test: `adb shell input keyevent KEYCODE_WAKEUP` flips the display ON and unblocks camera. |
| Logcat: `CameraIdListIncorrectException: Expected camera missing` | You re-introduced CameraX. Revert to raw Camera2 — see `CameraPreviewStreamer.kt`. |
| Logcat: `Background start not allowed: ... startFg?=true` | `startForegroundService()` is being called from `Application.onCreate` or some other background context. Move to `MainActivity.onCreate`. |
| Camera-permission dialog never appears | App was installed before, permission already denied. `adb shell pm clear com.shiftmind.glasses` then re-launch. |
| Server unreachable from glasses (mode A) | `adb kill-server` wiped the reverse tunnel. Re-run `adb reverse tcp:8000 tcp:8000`. |
| HUD text shows reversed | Mirror flag re-added somewhere. Check `HudScreen.kt` for any `scaleX = -1f`. |
| `bindToLifecycle failed` in logcat | You re-introduced CameraX (and the Rokid HAL bug). See above. |
| Preview is rotated 90° | Sensor mounts at 270°. Server side fixes this in `api/preview.py` (`PREVIEW_ROTATE_CV2 = cv2.ROTATE_90_CLOCKWISE`). |

## Recording AR video (HUD + camera) — built into ShiftMind

**Phase 8.0 status:** working. Press the configured physical button (default `KEYCODE_ENTER = 66`) to start, press again to stop. ShiftMind writes two files: native camera+audio MP4 from the app, plus a HUD-only MP4 from Rokid screenstream.

ShiftMind uses Camera2's multi-output capture session: the **same camera** feeds both the face-detect pipeline (ImageReader → JPEG → server) AND a recording pipeline (SurfaceTexture → GL → MediaCodec H.264 → MediaMuxer MP4). The face-detect pipeline keeps running while recording — no interruption.

```
                   ┌─→ ImageReader (640x480 YUV) → JPEG → server         (face detect)
Camera2 session ───┤
                   └─→ SurfaceTexture (640x480 raw) → GL copy → MediaCodec H.264 (480x640)
                                                              → MediaMuxer cam+audio MP4

Rokid screenstream broadcast ───────────────────────────────→ HUD-only MP4
```

### Use it

1. Launch ShiftMind on the glasses.
2. Press the configured button. A red `● REC` indicator appears in the HUD.
3. Worker interaction goes here. ShiftMind continues face-detect during recording.
4. Press the button again. Indicator clears. MP4 is finalized.

### Output

Cam+audio: `Movies/ShiftMind/shiftmind_YYYYMMDD_HHMMSS_cam.mp4` in MediaStore. Settings: 480x640 portrait, raw 640x480 camera input with `shiftmind.recordingRotation=0`, 24 fps, H.264 baseline @ 2 Mbps.

HUD: `ScreenRecorder/ScreenRecorder_YYYYMMDDHHMMSS.mp4`, produced by Rokid's screenstream service. Pull both with `/pull-videos`; the skill auto-creates `shiftmind_<ts>_hud_keyed_opacity65.mp4` by black-keying the HUD MP4 and overlaying it over cam+audio at 65% opacity.

### Picking your physical button

**Don't use `KEYCODE_MENU` (82)** — on Rokid that's the **power button**, and the firmware intercepts it for the native photo shortcut before the app sees it. Symptom: pressing it takes a photo via the system camera and never reaches `dispatchKeyEvent`.

Recommended: **touchpad single-tap = `KEYCODE_ENTER` (66)**. Already set as the default in `local.properties.example`.

To discover other buttons, get the input-device list and live-capture events:
```bash
adb shell getevent -lp                                 # list input devices
adb shell "timeout 12 getevent -lt /dev/input/event1"  # touchpad (ROKID,PSOC-TP-R)
adb shell "timeout 12 getevent -lt /dev/input/event0"  # power button (qpnp_pon)
```

Tap each button. The lines that show up at the linux input layer (`KEY_*`) may differ from what the app sees — system shortcuts can swallow some events. Cross-check with logcat:
```bash
adb logcat -s ShiftMind.Main:V -v time
```
Every key MainActivity actually receives is logged as `keyEvent: code=NN`. Pick a keycode that appears in logcat (not just getevent) and set it:
```properties
shiftmind.recordingKey=66
```
Rebuild + reinstall.

Touchpad reference (observed on RG-glasses firmware June 2026):

| Linux KEY | Android keycode | Behavior |
| :--- | :--- | :--- |
| `KEY_ENTER` | 66 `KEYCODE_ENTER` | Single tap. Reaches app. **Use this.** |
| `KEY_DASHBOARD` | (system) | Fires alongside ENTER; system-grabbed. Harmless when app foreground. |
| `KEY_UP/DOWN/LEFT/RIGHT` | 19/20/21/22 | Swipe gestures. |
| `KEY_BACK` | 4 `KEYCODE_BACK` | Long-press; closes app — avoid. |
| `KEY_F13/F14`, `PROG1/2/3` | varies | Programmable. Test individually. |

### Coexisting with external AR recorders (OverlayRec)

We still listen for the Rokid `mix_record` broadcast (`com.rokid.os.master.assist.server.cmd`) in `RecordingStateReceiver`, but only **log** these events — we no longer release the camera. If the user triggers OverlayRec's AR Record while ShiftMind is running, both will fight for the camera (avoid).



## Q&A Mode (Business Intelligence)

**Status:** Production-ready. Swipe-forward gesture (touchpad back→front) enters Q&A mode for voice-driven business insights over collected session history.

### How it works

1. **Enter:** Swipe forward on touchpad while no customer session is active
2. **Ask:** Single-tap to start recording → speak your question → tap again to stop
3. **Live transcript:** Question appears on HUD as you speak (Doubao streaming ASR)
4. **Answer:** Short answer (≤140 chars) displays on HUD; full answer + tool trace visible on server dashboard
5. **Follow-ups:** Conversation history maintained (20 turns) so "what did he buy?" references prior "who was the last customer?"
6. **Exit:** Swipe forward again to exit Q&A mode and return to face scanning

### Example questions

- "Who was the last customer that came here?"
- "What did he want to buy?" ← contextual follow-up
- "Show me conversion rate for last week"
- "Which employees have the longest conversations?"
- "What topics do customers ask about most?"

Server-side agent has 6 tools: SQL queries, vector search over session memories, customer profile lookup, employee/location comparison, outlier detection. Web dashboard at `http://localhost:8000/api/qa.html` shows full answers + tool traces + sources.

### Config

Q&A toggle key defaults to swipe-forward. Customize in `local.properties`:
```properties
shiftmind.qaToggleKey=115   # KEYCODE_FORWARD (swipe forward)
```

Requires `DOUBAO_ASR_API_KEY` on server for voice transcription (see `../shiftmind-server/README.md`).

## Not in this phase

- Trigger classifier (phase 6)
- VLM relay (`/api/chat`) for product queries (phase 6)

See `../CLAUDE.md` and vault `Projects/ShiftMind/architecture_v2.md` for the full plan.
