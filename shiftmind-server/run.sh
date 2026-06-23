#!/usr/bin/env bash
# Dev runner: starts the FastAPI server and, if a glass is plugged in via USB,
# sets up `adb reverse tcp:8000 tcp:8000` so the device can hit the laptop at
# 127.0.0.1:8000. Idempotent — safe to run repeatedly. Tunnel is torn down on
# Ctrl+C.
#
# Usage:
#   ./run.sh                  # 0.0.0.0:8000 with --reload
#   PORT=9000 ./run.sh        # override port (both tunnel + uvicorn)
#   NO_RELOAD=1 ./run.sh      # disable autoreload
#   NO_ADB=1 ./run.sh         # skip adb reverse (e.g. running over WiFi)
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

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
  if [[ "$adb_reverse_set" == true ]]; then
    echo
    echo "[run.sh] removing adb reverse tcp:${PORT}"
    "$ADB_BIN" reverse --remove "tcp:${PORT}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# ── uvicorn ───────────────────────────────────────────────────────────────
reload_flag="--reload"
[[ -n "${NO_RELOAD:-}" ]] && reload_flag=""

echo "[run.sh] starting uvicorn on ${HOST}:${PORT}${reload_flag:+ (reload)}"
exec uv run uvicorn server:app --host "$HOST" --port "$PORT" $reload_flag
