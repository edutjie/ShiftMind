#!/usr/bin/env bash
# Dev runner: starts all server-side dev pieces and, if glasses are plugged in
# via USB, sets up `adb reverse tcp:8000 tcp:8000` so the device can hit the
# laptop at 127.0.0.1:8000. Idempotent — safe to run repeatedly. Child
# processes and tunnel are torn down on Ctrl+C.
#
# Usage:
#   ./run.sh                  # all: adb reverse + uvicorn
#   ./run.sh all              # same as above
#   ./run.sh server           # uvicorn only (still honors adb unless NO_ADB=1)
#   PORT=9000 ./run.sh        # override port (both tunnel + uvicorn)
#   RELOAD=1 ./run.sh         # enable autoreload (off by default; Milvus Lite locks local DB)
#   NO_ADB=1 ./run.sh         # skip adb reverse (e.g. running over WiFi)
#   OPEN_PREVIEW=1 ./run.sh   # open /api/preview.html after server starts
#   WEBCAM=1 ./run.sh         # also feed laptop webcam into /api/preview-frame
set -euo pipefail

cd "$(dirname "$0")"

MODE="${1:-all}"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
LOCAL_URL="http://127.0.0.1:${PORT}"
PIDS=()

if [[ "$MODE" != "all" && "$MODE" != "server" ]]; then
  echo "[run.sh] unknown mode: $MODE"
  echo "[run.sh] usage: ./run.sh [all|server]"
  exit 2
fi

# ── adb reverse tunnel ────────────────────────────────────────────────────
# Find adb. Prefer $PATH, else fall back to common Android SDK locations.
find_adb() {
  if command -v adb >/dev/null 2>&1; then command -v adb; return; fi
  local candidates=(
    "${ANDROID_HOME:-}/platform-tools/adb"
    "${ANDROID_SDK_ROOT:-}/platform-tools/adb"
    "$HOME/Library/Android/sdk/platform-tools/adb"           # macOS default
    "$HOME/Android/Sdk/platform-tools/adb"                   # Linux default
    "/usr/local/share/android-sdk/platform-tools/adb"        # brew cask
  )
  for c in "${candidates[@]}"; do
    [[ -n "$c" && -x "$c" ]] && { echo "$c"; return; }
  done
  return 1
}

adb_reverse_set=false
ADB_BIN=""
if [[ -z "${NO_ADB:-}" ]]; then
  if ADB_BIN="$(find_adb)"; then
    attached="$("$ADB_BIN" devices | awk 'NR>1 && $2=="device" {print $1}')"
    if [[ -n "$attached" ]]; then
      echo "[run.sh] adb=${ADB_BIN}"
      echo "[run.sh] adb reverse tcp:${PORT} tcp:${PORT}  (devices: $(echo "$attached" | tr '\n' ' '))"
      "$ADB_BIN" reverse "tcp:${PORT}" "tcp:${PORT}"
      adb_reverse_set=true
    else
      echo "[run.sh] adb found (${ADB_BIN}) but no device attached — skipping reverse tunnel"
    fi
  else
    echo "[run.sh] adb not found in PATH or common SDK locations — skipping reverse tunnel"
    echo "[run.sh]   set ANDROID_HOME or add platform-tools to PATH"
  fi
else
  echo "[run.sh] NO_ADB set — skipping reverse tunnel"
fi

# Tear down the tunnel on exit so a later WiFi-only session doesn't see a
# stale entry blocking the port forward.
cleanup() {
  if ((${#PIDS[@]})); then
    echo
    echo "[run.sh] stopping child processes"
    for pid in "${PIDS[@]}"; do
      kill "$pid" 2>/dev/null || true
    done
    wait "${PIDS[@]}" 2>/dev/null || true
  fi
  if [[ "$adb_reverse_set" == true ]]; then
    echo
    echo "[run.sh] removing adb reverse tcp:${PORT}"
    "$ADB_BIN" reverse --remove "tcp:${PORT}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# ── processes ─────────────────────────────────────────────────────────────
reload_flag=""
[[ -n "${RELOAD:-}" ]] && reload_flag="--reload"

echo "[run.sh] starting uvicorn on ${HOST}:${PORT}${reload_flag:+ (reload)}"
uv run uvicorn server:app --host "$HOST" --port "$PORT" $reload_flag &
PIDS+=("$!")

echo "[run.sh] preview:     ${LOCAL_URL}/api/preview.html"
echo "[run.sh] raw MJPEG:   ${LOCAL_URL}/api/preview"
echo "[run.sh] HUD MJPEG:   ${LOCAL_URL}/api/preview-hud"
echo "[run.sh] status:      ${LOCAL_URL}/api/preview-status"

if [[ -n "${OPEN_PREVIEW:-}" ]]; then
  if command -v open >/dev/null 2>&1; then
    (sleep 1; open "${LOCAL_URL}/api/preview.html") &
  elif command -v xdg-open >/dev/null 2>&1; then
    (sleep 1; xdg-open "${LOCAL_URL}/api/preview.html") &
  else
    echo "[run.sh] OPEN_PREVIEW set but no open/xdg-open found"
  fi
fi

if [[ -n "${WEBCAM:-}" ]]; then
  fps="${WEBCAM_FPS:-5}"
  device="${WEBCAM_DEVICE:-0}"
  echo "[run.sh] starting webcam feeder (device=${device}, fps=${fps})"
  uv run python tools/feed_webcam.py --url "${LOCAL_URL}" --fps "$fps" --device "$device" &
  PIDS+=("$!")
fi

wait "${PIDS[@]}"
