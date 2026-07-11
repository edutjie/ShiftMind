#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Stream a selectable Rokid microphone/channel to this laptop.

Usage:
  ./tools/live-mic.sh [default|bottom|back] [mono|quad1|quad2|quad3|quad4]

Examples:
  ./tools/live-mic.sh back mono       # current ShiftMind route (ID 23)
  ./tools/live-mic.sh bottom mono     # alternate logical input (ID 21)
  ./tools/live-mic.sh default quad1   # raw array channel 1
  ./tools/live-mic.sh default quad4   # raw array channel 4

Environment:
  GAIN_DB=12  laptop playback boost only; capture bytes remain unchanged
  PORT=18765  override the ADB-reversed TCP port

Press Ctrl-C to stop. The script temporarily stops the main ShiftMind app so
camera/session audio cannot compete with this diagnostic capture.
EOF
}

DEVICE="${1:-back}"
CHANNEL="${2:-mono}"
GAIN_DB="${GAIN_DB:-0}"
PORT="${PORT:-18765}"
PACKAGE="com.shiftmind.glasses"
ACTIVITY="$PACKAGE/.diagnostic.MicMonitorActivity"
ADB="$(command -v adb 2>/dev/null || true)"
FFPLAY="$(command -v ffplay 2>/dev/null || true)"
PLAYER_PID=""

case "$DEVICE" in default|bottom|back) ;; -h|--help|help) usage; exit 0 ;; *) usage; exit 2 ;; esac
case "$CHANNEL" in mono|quad1|quad2|quad3|quad4) ;; *) usage; exit 2 ;; esac
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "PORT must be numeric" >&2; exit 2; }
[[ "$GAIN_DB" =~ ^-?[0-9]+([.][0-9]+)?$ ]] || { echo "GAIN_DB must be numeric" >&2; exit 2; }

if [[ -z "$ADB" ]]; then
  fallback="$HOME/Library/Android/sdk/platform-tools/adb"
  [[ -x "$fallback" ]] && ADB="$fallback"
fi
[[ -n "$ADB" ]] || { echo "adb not found" >&2; exit 1; }
[[ -n "$FFPLAY" ]] || { echo "ffplay not found; install FFmpeg first" >&2; exit 1; }
"$ADB" get-state >/dev/null 2>&1 || { echo "no glasses connected" >&2; exit 1; }

cleanup() {
  set +e
  [[ -n "$PLAYER_PID" ]] && kill "$PLAYER_PID" >/dev/null 2>&1
  "$ADB" shell am force-stop "$PACKAGE" >/dev/null 2>&1
  "$ADB" reverse --remove "tcp:$PORT" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

"$ADB" shell am force-stop "$PACKAGE" >/dev/null
"$ADB" shell pm grant "$PACKAGE" android.permission.RECORD_AUDIO >/dev/null 2>&1 || true
"$ADB" reverse "tcp:$PORT" "tcp:$PORT" >/dev/null
"$ADB" logcat -c

echo "Listening: device=$DEVICE channel=$CHANNEL playbackGain=${GAIN_DB}dB"
echo "Speak from wearer and customer positions; press Ctrl-C to stop."

"$FFPLAY" \
  -hide_banner -loglevel warning -nodisp -autoexit \
  -fflags nobuffer -flags low_delay -probesize 32 -analyzeduration 0 \
  -f s16le -sample_rate 16000 -ch_layout mono -af "volume=${GAIN_DB}dB" \
  "tcp://127.0.0.1:${PORT}?listen=1" &
PLAYER_PID=$!

if command -v lsof >/dev/null 2>&1; then
  ready=0
  for _ in {1..30}; do
    kill -0 "$PLAYER_PID" >/dev/null 2>&1 || { echo "ffplay exited before listening" >&2; exit 1; }
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then ready=1; break; fi
    sleep 0.1
  done
  [[ "$ready" == 1 ]] || { echo "ffplay did not open port $PORT" >&2; exit 1; }
else
  sleep 0.5
fi

"$ADB" shell am start -W -n "$ACTIVITY" \
  --es device "$DEVICE" --es channel "$CHANNEL" --ei port "$PORT" >/dev/null

"$ADB" logcat -d -v brief -s ShiftMind.MicMonitor:I '*:S'
wait "$PLAYER_PID"
