---
name: pull-videos
description: Pull ShiftMind recording pairs (cam + HUD MP4) from glasses to local recordings/ dir, then auto-combine HUD over cam
trigger: /pull-videos
---

# /pull-videos

Pulls ShiftMind recording pairs off the Rokid glasses into the repo's `recordings/` folder, then automatically combines each matched pair into a review-ready MP4. ShiftMind records two parallel MP4s per session (see [project AGENTS.md](../../AGENTS.md) — "HUD recording uses Rokid's screenstream broadcast"):

- **cam+audio:** `/sdcard/Movies/ShiftMind/shiftmind_<ts>_cam.mp4` (our Camera2 recorder)
- **HUD screen:** `/sdcard/ScreenRecorder/ScreenRecorder_<ts>.mp4` (Rokid's screenstream service)

Filenames don't share a basename — Rokid names its own file with its own clock (~+1s offset). Pair by wall-clock time.

Combined output uses the cam+audio MP4 as base and overlays the HUD screen MP4 after black-keying its background. HUD opacity is `65%` so the AR UI remains readable without washing out the camera view:

- **combined:** `recordings/shiftmind_<ts>_hud_keyed_opacity65.mp4`

## Usage

```
/pull-videos                       # latest cam + latest HUD (default)
/pull-videos latest                # alias for above
/pull-videos all                   # pull every cam+HUD pair not already local
/pull-videos since <minutes>       # everything modified in last <minutes>
/pull-videos cam <basename>        # specific cam file by basename
/pull-videos clean                 # rm -i recordings/*.mp4 (prompt before delete)
```

## Behavior

1. Resolve `adb` binary. Try `which adb`, then fall back to `~/Library/Android/sdk/platform-tools/adb`. Fail with a clear message if neither works.
2. `adb devices` — must list at least one device. If none, tell user to plug in glasses + enable USB debug.
3. For `latest`:
   - `adb shell ls -t /sdcard/Movies/ShiftMind/ | head -1` → cam filename
   - `adb shell ls -t /sdcard/ScreenRecorder/ | head -1` → HUD filename
   - `adb pull` each into `<repo>/recordings/`
   - Skip if local copy already exists with same byte size (no clobber)
   - Combine the pair with FFmpeg if both files are available locally
4. For `all`:
   - List both source dirs, diff against local, pull missing files only
   - Combine all local cam/HUD pairs with timestamps within ±5s
5. For `since <minutes>`:
   - `adb shell find /sdcard/Movies/ShiftMind /sdcard/ScreenRecorder -name '*.mp4' -mmin -<n>`
   - Pull each
   - Combine all local cam/HUD pairs with timestamps within ±5s
6. For `cam <basename>`:
   - Pull `Movies/ShiftMind/<basename>` directly
   - If basename has a `_cam.mp4` suffix and a `<ts>` stem, also find any `ScreenRecorder_<ts*>.mp4` within ±5s of that wall-clock — best-effort pair lookup using `adb shell stat`.
   - Combine the pair if a matching HUD file is available locally
7. For `clean`:
   - Run `rm -i recordings/*.mp4` (interactive — user confirms each).

## Output format

After pulling, print one row per pulled file:

```
✓ shiftmind_20260624_002454_cam.mp4   1.86 MB   00:24:54
✓ ScreenRecorder_20260624002455.mp4   144 KB    00:24:55  (+1s from cam)
✓ shiftmind_20260624_002454_hud_keyed_opacity65.mp4   combined HUD@65%
```

And a one-line summary: `pulled 2 file(s), combined 1 pair(s) → recordings/` or `nothing new — all <N> file(s) already local`.

## Implementation notes

- The cam-side stem timestamp is parseable: `shiftmind_(\d{8}_\d{6})_cam.mp4`. The HUD stem is `ScreenRecorder_(\d{14}).mp4` (no underscore between date+time).
- Use `adb pull` not `adb shell cat` — `pull` preserves mtime and is binary-safe.
- Recordings stay on glasses; this skill is the only way to get them to laptop (over-HTTP upload is too expensive per [project AGENTS.md](../../AGENTS.md)).
- If the latest cam file is `<10KB` it's likely a corrupt session (camera didn't reach `outputs=2`). Warn but pull anyway.
- HUD file under `~50KB` warns: screenstream may have stopped early.
- Requires `ffmpeg` for auto-combine. If missing, pull still succeeds and prints `ffmpeg not found — skipped combine`.
- Combine command:

```bash
ffmpeg -y \
  -i "$cam_path" \
  -i "$hud_path" \
  -filter_complex "[0:v]setpts=PTS-STARTPTS,format=rgba[cam];[1:v]setpts=PTS-STARTPTS,scale=480:640,format=rgba,colorkey=0x000000:0.08:0.04,colorchannelmixer=aa=0.65[hud];[cam][hud]overlay=0:0:format=auto:shortest=1,format=yuv420p[v];[0:a]apad[a]" \
  -map "[v]" -map "[a]" \
  -c:v libx264 -preset medium -crf 18 \
  -c:a aac -b:a 48k -shortest \
  "$out_path"
```

## Reference inline script (preferred — paste-ready)

```bash
#!/usr/bin/env bash
# /pull-videos [latest|all|since N|cam BASENAME|clean]
set -euo pipefail

REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null \
        || echo /Users/edutjie/Documents/tsinghua/lab/hci/codes/ShiftMind/ShiftMind)"
DEST="$REPO/recordings"
mkdir -p "$DEST"

ADB="$(command -v adb 2>/dev/null || echo /Users/edutjie/Library/Android/sdk/platform-tools/adb)"
[ -x "$ADB" ] || { echo "adb not found"; exit 1; }
$ADB get-state 1>/dev/null 2>&1 || { echo "no glasses connected — run 'adb devices'"; exit 1; }

mode="${1:-latest}"
pulled_count=0
combined_count=0
already_count=0

pull_one() {
  local remote="$1" name="$(basename "$1")" local_path="$DEST/$1"
  local_path="$DEST/$name"
  if [ -f "$local_path" ]; then
    local rsize lsize
    rsize=$($ADB shell stat -c '%s' "$remote" 2>/dev/null | tr -d '\r')
    lsize=$(stat -f '%z' "$local_path" 2>/dev/null || stat -c '%s' "$local_path")
    [ "$rsize" = "$lsize" ] && { echo "= $name (already local)"; already_count=$((already_count + 1)); return; }
  fi
  $ADB pull "$remote" "$local_path" >/dev/null 2>&1
  local sz; sz=$(stat -f '%z' "$local_path" 2>/dev/null || stat -c '%s' "$local_path")
  printf '✓ %-44s %s\n' "$name" "$(numfmt --to=iec --suffix=B "$sz" 2>/dev/null || echo "$sz B")"
  pulled_count=$((pulled_count + 1))
}

combine_pair() {
  local cam_path="$1" hud_path="$2"
  local cam_name out_name out_path
  cam_name="$(basename "$cam_path")"
  out_name="${cam_name/_cam.mp4/_hud_keyed_opacity65.mp4}"
  out_path="$DEST/$out_name"

  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg not found — skipped combine"
    return
  fi
  if [ -f "$out_path" ] && [ "$out_path" -nt "$cam_path" ] && [ "$out_path" -nt "$hud_path" ]; then
    echo "= $out_name (already combined)"
    return
  fi

  ffmpeg -y \
    -i "$cam_path" \
    -i "$hud_path" \
    -filter_complex "[0:v]setpts=PTS-STARTPTS,format=rgba[cam];[1:v]setpts=PTS-STARTPTS,scale=480:640,format=rgba,colorkey=0x000000:0.08:0.04,colorchannelmixer=aa=0.65[hud];[cam][hud]overlay=0:0:format=auto:shortest=1,format=yuv420p[v];[0:a]apad[a]" \
    -map "[v]" -map "[a]" \
    -c:v libx264 -preset medium -crf 18 \
    -c:a aac -b:a 48k -shortest \
    "$out_path" >/dev/null 2>&1

  printf '✓ %-44s combined HUD@65%%\n' "$out_name"
  combined_count=$((combined_count + 1))
}

combine_local_pairs() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found — skipped combine pair matching"
    return
  fi
  while IFS="$(printf '\t')" read -r cam hud; do
    [ -n "${cam:-}" ] && [ -n "${hud:-}" ] || continue
    combine_pair "$cam" "$hud"
  done < <(python3 - "$DEST" <<'PY'
import datetime as dt
import pathlib
import re
import sys

dest = pathlib.Path(sys.argv[1])
cams = []
huds = []
for p in dest.glob("shiftmind_*_cam.mp4"):
    m = re.match(r"shiftmind_(\d{8})_(\d{6})_cam\.mp4$", p.name)
    if m:
        t = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        cams.append((p, t))
for p in dest.glob("ScreenRecorder_*.mp4"):
    m = re.match(r"ScreenRecorder_(\d{14})\.mp4$", p.name)
    if m:
        t = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
        huds.append((p, t))
for cam, cam_t in cams:
    if not huds:
        continue
    hud, delta = min(((p, abs((t - cam_t).total_seconds())) for p, t in huds), key=lambda x: x[1])
    if delta <= 5:
        print(f"{cam}\t{hud}")
PY
)
}

case "$mode" in
  latest|"")
    cam=$($ADB shell ls -t /sdcard/Movies/ShiftMind/ 2>/dev/null | head -1 | tr -d '\r')
    hud=$($ADB shell ls -t /sdcard/ScreenRecorder/ 2>/dev/null | head -1 | tr -d '\r')
    [ -n "$cam" ] && pull_one "/sdcard/Movies/ShiftMind/$cam"   || echo "no cam files on device"
    [ -n "$hud" ] && pull_one "/sdcard/ScreenRecorder/$hud"     || echo "no hud files on device"
    ;;
  all)
    for f in $($ADB shell ls /sdcard/Movies/ShiftMind/ 2>/dev/null | tr -d '\r'); do
      pull_one "/sdcard/Movies/ShiftMind/$f"
    done
    for f in $($ADB shell ls /sdcard/ScreenRecorder/ 2>/dev/null | tr -d '\r'); do
      pull_one "/sdcard/ScreenRecorder/$f"
    done
    ;;
  since)
    n="${2:?usage: /pull-videos since <minutes>}"
    for f in $($ADB shell "find /sdcard/Movies/ShiftMind /sdcard/ScreenRecorder -name '*.mp4' -mmin -$n" 2>/dev/null | tr -d '\r'); do
      pull_one "$f"
    done
    ;;
  cam)
    name="${2:?usage: /pull-videos cam <basename>}"
    pull_one "/sdcard/Movies/ShiftMind/$name"
    # best-effort HUD pair lookup: same date_HHMMSS, ±5s
    ts=$(echo "$name" | sed -E 's/shiftmind_([0-9]{8})_([0-9]{6})_cam\.mp4/\1\2/')
    if [ -n "$ts" ]; then
      for f in $($ADB shell ls /sdcard/ScreenRecorder/ 2>/dev/null | tr -d '\r' | grep "ScreenRecorder_${ts:0:12}"); do
        pull_one "/sdcard/ScreenRecorder/$f"
      done
    fi
    ;;
  clean)
    rm -i "$DEST"/*.mp4 2>/dev/null || echo "nothing to clean"
    echo "→ $DEST"
    exit 0
    ;;
  *) echo "unknown mode: $mode"; exit 2 ;;
esac

combine_local_pairs

echo "pulled ${pulled_count} file(s), combined ${combined_count} pair(s) → $DEST"
```

When the user invokes `/pull-videos`, run that script (or the equivalent inline `adb` calls) directly via the Bash tool and report results. Don't ask for confirmation on read-only `adb pull` — it just copies files into a gitignored `recordings/` dir, fully reversible.
