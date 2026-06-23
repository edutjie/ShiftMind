# shiftmind-glasses

Single-module Kotlin/Compose Android app for Rokid AR glasses (`RG-glasses` / Max Pro). Phase 1.5: HUD bootstrap showing "ShiftMind" with a face-sized box cursor at screen center, plus a **Camera2 → JPEG → `/api/preview-frame`** streamer so the laptop can see what the glasses see.

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
| `MainActivity.kt` | Hosts the Compose HUD; requests CAMERA permission; starts `GlassesService` + screen-wake lock + `CameraPreviewStreamer`. `FLAG_KEEP_SCREEN_ON` + `setTurnScreenOn` + `SCREEN_BRIGHT_WAKE_LOCK \| ACQUIRE_CAUSES_WAKEUP` so the Rokid prism reports display-on to AOSP. |
| `service/GlassesService.kt` | Foreground service: notification + wake lock. Passes only `FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE` to `startForeground` (Android 14 strict — `CAMERA` type would require perm grant at the exact moment of `startForeground`). |
| `hud/HudScreen.kt` | Title at TopCenter, BoxCursor at viewport center. No prism mirror flag (see note below). |
| `hud/BoxCursor.kt` | Center face slot — 240dp, corner brackets + center crosshair. |
| `ui/theme/Theme.kt` | Bright-green-on-black dark scheme. Material3 light scheme washes out on AR optics. |
| `camera/CameraPreviewStreamer.kt` | Raw Camera2 (`CameraManager.openCamera("0", ...)`) → `ImageReader` YUV_420_888 @ 640×480 → NV21 → `YuvImage.compressToJpeg` → `PreviewClient.postFrame`. Throttled to `BuildConfig.PREVIEW_FPS`. |
| `net/PreviewClient.kt` | OkHttp wrapper for POST `/api/preview-frame`. Aggressive timeouts (1.5s connect, 2s write/read). Never throws — failures logged + counted. |

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

## Not in this phase

- ML Kit face detection (phase 2 — will hook into the existing CameraPreviewStreamer)
- `/api/detect-face` round-trip on face-in-cursor (phase 3)
- AudioRecord capture + `/api/transcribe` (phase 5)
- `mix_record` recording broadcast (phase 8)

See `../CLAUDE.md` and vault `Projects/ShiftMind/architecture_v2.md` for the full plan.
