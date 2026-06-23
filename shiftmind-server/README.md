# shiftmind-server

Laptop FastAPI backend for ShiftMind. Phase 1: health endpoint, mock face-detect, live MJPEG preview of frames the glasses send.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12 (uv will fetch it if missing).

```bash
cd shiftmind-server
uv sync
```

## Run

```bash
./run.sh
```

Wrapper that runs `adb reverse tcp:8000 tcp:8000` (so the glasses can reach the laptop at `127.0.0.1:8000` over USB) and then starts uvicorn with `--reload`. Tunnel is torn down on Ctrl+C.

Overrides via env: `PORT=9000 ./run.sh`, `NO_RELOAD=1 ./run.sh`, `NO_ADB=1 ./run.sh` (skip tunnel — e.g. when running over WiFi).

Manual equivalent if you don't want the wrapper:

```bash
adb reverse tcp:8000 tcp:8000          # so glasses can hit 127.0.0.1:8000
uv run uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Binds `0.0.0.0` so the glasses (or any device on the same WiFi) can reach it. Local dev only — no auth.

## Endpoints

| Endpoint | Method | Purpose |
| :--- | :--- | :--- |
| `/api/health` | GET | Liveness check. Returns `{"status":"ok","version":"0.1.0"}`. |
| `/api/detect-face` | POST | Multipart `image=<jpeg>`. Phase-1 mock: returns a fixed known customer, or a new-customer payload when header `X-Mock-New: 1` is set. |
| `/api/preview-frame` | POST | Dev-only. Multipart `image=<jpeg>`. Writes to the in-memory slot the MJPEG stream reads from. |
| `/api/preview` | GET | Live MJPEG stream (`multipart/x-mixed-replace`). Rotated 90° clockwise to compensate for the Rokid 270° sensor mount — see `PREVIEW_ROTATE_CV2` in `api/preview.py`. Open in any browser. |
| `/api/preview.html` | GET | Convenience page: tiny `<img>` wrapping `/api/preview`. |
| `/api/preview-status` | GET | `{have_frame, seq, age_seconds}`. |

## Dev tool — feed webcam into the preview pipeline

Before the glasses Camera2 client is built, validate the preview end-to-end with the laptop webcam:

```bash
uv run python tools/feed_webcam.py            # localhost, 5 fps, device 0
uv run python tools/feed_webcam.py --fps 10
uv run python tools/feed_webcam.py --url http://192.168.1.50:8000
```

Then open `http://localhost:8000/api/preview.html` in a browser — live feed.

## Smoke test

```bash
# in one terminal
uv run uvicorn server:app --host 0.0.0.0 --port 8000

# in another
curl http://localhost:8000/api/health
curl -X POST -F "image=@/some/photo.jpg" http://localhost:8000/api/detect-face
curl -X POST -H "X-Mock-New: 1" -F "image=@/some/photo.jpg" http://localhost:8000/api/detect-face
curl http://localhost:8000/api/preview-status

# preview
uv run python tools/feed_webcam.py
open http://localhost:8000/api/preview.html      # macOS; or paste into browser
```

## What's NOT here yet

- Real face recognition (insightface + FAISS) — phase 3
- Audio STT (Whisper) — phase 5
- Trigger classifier — phase 6
- Conversation persistence (SQLite) — phase 7
- VLM relay (`/api/chat`) — phase 6

See `../CLAUDE.md` and vault `Projects/ShiftMind/architecture_v2.md` for the full design.
