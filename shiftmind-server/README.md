# shiftmind-server

Laptop FastAPI backend for ShiftMind. Current dev server: health endpoint,
live MJPEG preview, ML Kit face-crop ingestion, InsightFace `buffalo_l`
recognition, SQLite customer storage, Milvus Lite embedding search, and
customer interaction session summarization.

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

Wrapper that runs `adb reverse tcp:8000 tcp:8000` (so the glasses can reach the laptop at `127.0.0.1:8000` over USB) and then starts uvicorn. Tunnel is torn down on Ctrl+C.

Overrides via env: `PORT=9000 ./run.sh`, `RELOAD=1 ./run.sh`, `NO_ADB=1 ./run.sh` (skip tunnel — e.g. when running over WiFi).

`RELOAD=1` is off by default because Milvus Lite uses a local DB file lock;
uvicorn reload can briefly overlap processes and trigger `DataDirLockedError`.

Manual equivalent if you don't want the wrapper:

```bash
adb reverse tcp:8000 tcp:8000          # so glasses can hit 127.0.0.1:8000
uv run uvicorn server:app --host 0.0.0.0 --port 8000
```

Binds `0.0.0.0` so the glasses (or any device on the same WiFi) can reach it. Local dev only — no auth.

## LLM / transcription config

Live ASR uses Doubao Seed ASR 2.0 through the server-side WebSocket relay.
Add these to `shiftmind-server/.env`:

```bash
DOUBAO_ASR_API_KEY=...
DOUBAO_ASR_BASE_URL=wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_async
DOUBAO_ASR_RESOURCE_ID=volc.seedasr.sauc.duration
DOUBAO_ASR_MODEL=bigmodel
```

Session finish works without credentials, but only stores a fallback "no transcript"
summary. Add these for OpenAI-compatible summary/fallback transcription:

```bash
OPENAI_API_KEY=...
OPENAI_BASE_URL=...              # optional OpenAI-compatible base URL
SHIFTMIND_TRANSCRIBE_MODEL=whisper-large-v3-turbo
SHIFTMIND_LLM_MODEL="gpt-5.4-mini"
```

## Endpoints

| Endpoint | Method | Purpose |
| :--- | :--- | :--- |
| `/api/health` | GET | Liveness check. Returns `{"status":"ok","version":"0.1.0"}`. |
| `/api/detect-face` | POST | Multipart `image=<jpeg>`. Runs InsightFace `buffalo_l`, searches Milvus Lite, and returns current vote/recognition state. |
| `/api/enroll-face` | POST | Debug endpoint. Form `person_id`, `name`, `memory_card`; embeds latest face crop and stores customer in SQLite + Milvus Lite. |
| `/api/faces.html` | GET | Debug page for latest face crop, vote state, customers, Milvus count, and enrollment. |
| `/api/preview-frame` | POST | Dev-only. Multipart `image=<jpeg>` plus optional `face_crop=<jpeg>`. Stores preview frame and recognizes face crops. |
| `/api/session-status` | GET | Current active customer session, if any. |
| `/api/session/{session_id}/audio-stream` | WS | Glasses stream 16 kHz PCM chunks; server relays to Doubao ASR and returns live transcript JSON. |
| `/api/session/{session_id}/finish` | POST | Multipart `audio=<wav>` plus optional `transcript`. Transcribes/summarizes, updates SQLite memory, ends the session. |
| `/api/sessions` | GET | JSON debug list of active/recent sessions. |
| `/api/preview` | GET | Live MJPEG stream (`multipart/x-mixed-replace`). Rotated 90° clockwise to compensate for the Rokid 270° sensor mount — see `PREVIEW_ROTATE_CV2` in `api/preview.py`. Open in any browser. |
| `/api/preview-face` | GET | Live MJPEG stream of the latest face crop. |
| `/api/preview.html` | GET | Convenience page showing HUD preview + latest face crop. |
| `/api/preview-status` | GET | Preview, face crop, and recognition status JSON. |

## Face recognition dev flow

1. Launch server with `./run.sh`.
2. Open `http://localhost:8000/api/preview.html`.
3. Align a face inside the glasses cursor until the face crop appears.
4. Open `http://localhost:8000/api/faces.html`.
5. Enroll the latest crop with `person_id`, `name`, and `memory_card`.
6. Re-show the same face. Recognition confirms after 3 of the last 5 valid crops.
7. Confirmed known/unknown customers automatically start a session. Glasses record
   voice until the configured physical button is pressed, then upload WAV audio
   to `/api/session/{session_id}/finish`.

Data lives under `shiftmind-server/data/` and is gitignored:

- `shiftmind.sqlite3` — customers, face images, recognition events.
- `milvus/shiftmind.db` — Milvus Lite vector store.
- `faces/*.jpg` — saved enrollment crops.
- `audio/*.wav` — uploaded customer-session voice recordings.

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
curl http://localhost:8000/api/preview-status

# preview
uv run python tools/feed_webcam.py
open http://localhost:8000/api/preview.html      # macOS; or paste into browser
```

## What's NOT here yet

- Streaming / low-latency STT; session audio uploads after button stop.
- Trigger classifier — phase 6
- VLM relay (`/api/chat`) — phase 6

See `../CLAUDE.md` and vault `Projects/ShiftMind/architecture_v2.md` for the full design.
