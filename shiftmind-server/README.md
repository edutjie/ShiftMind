# shiftmind-server

Laptop FastAPI backend for ShiftMind. Production features: InsightFace `buffalo_l` face recognition with Milvus vector search, automatic customer session lifecycle, Doubao Seed ASR streaming, LLM summarization with memory management, session memory embeddings, Q&A agent with 6 tools (SQL, vector search, customer profile, compare, outliers), and employee/location login for scoped queries.

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

Session finish and Q&A agent require OpenAI-compatible LLM credentials. Without
them, sessions get fallback "no transcript" summaries and Q&A agent returns an
error. Add these for full functionality:

```bash
SHIFTMIND_LLM_API_KEY=...
SHIFTMIND_LLM_BASE_URL=...       # optional OpenAI-compatible base URL
SHIFTMIND_TRANSCRIBE_MODEL=whisper-large-v3-turbo   # fallback if Doubao unavailable
SHIFTMIND_LLM_MODEL="gpt-5.4-mini"
```

`OPENAI_API_KEY` / `OPENAI_BASE_URL` remain backward-compatible fallbacks.

The Q&A agent uses the LLM client above. Text embeddings (for session memory
vector search) can use a separate OpenAI-compatible provider and land in a
second Milvus Lite collection (`session_memories`); the dim is fixed at
collection creation, so set these before the first run (and don't change them
afterward without dropping the collection):

```bash
SHIFTMIND_EMBED_API_KEY=...
SHIFTMIND_EMBED_BASE_URL=...                    # optional; falls back to shared/LLM URL
SHIFTMIND_EMBED_MODEL=text-embedding-3-small   # any OpenAI-compatible embeddings model
SHIFTMIND_EMBED_DIM=1536                       # must match the model's output dim
```

## Endpoints

| Endpoint | Method | Purpose |
| :--- | :--- | :--- |
| `/api/health` | GET | Liveness check. Returns `{"status":"ok","version":"0.1.0"}`. |
| `/api/detect-face` | POST | Multipart `image=<jpeg>`. Runs InsightFace `buffalo_l`, searches Milvus Lite, and returns current vote/recognition state. |
| `/api/enroll-face` | POST | Debug endpoint. Form `person_id`, `name`, `memory_card`; embeds latest face crop and stores customer in SQLite + Milvus Lite. |
| `/api/faces.html` | GET | Debug page for latest face crop, vote state, customers, Milvus count, and enrollment. |
| `/api/preview-frame` | POST | Dev-only. Multipart `image=<jpeg>` plus optional `face_crop=<jpeg>`. Stores preview frame and recognizes face crops. |
| `/api/session-status` | GET | Current active customer session (session_id, customer, memory_card, live_transcript), if any. Polled by glasses for HUD updates. |
| `/api/session/{session_id}/audio-stream` | WS | Glasses stream 16 kHz PCM chunks; server relays to Doubao ASR and returns live transcript JSON. |
| `/api/session/{session_id}/finish` | POST | Multipart `audio=<wav>` plus optional `transcript`. Transcribes/summarizes, updates SQLite memory + Milvus session_memories, ends the session. |
| `/api/sessions` | GET | JSON debug list of active/recent sessions. |
| `/api/preview` | GET | Live MJPEG stream (`multipart/x-mixed-replace`). Rotated 90° clockwise to compensate for the Rokid 270° sensor mount — see `PREVIEW_ROTATE_CV2` in `api/preview.py`. Open in any browser. |
| `/api/preview-face` | GET | Live MJPEG stream of the latest face crop. |
| `/api/preview.html` | GET | Convenience page showing HUD preview + latest face crop. |
| `/api/preview-status` | GET | Preview, face crop, and recognition status JSON. |
| `/api/login.html` | GET | Employee/location login page with optional PIN. |
| `/api/login` | POST | Session login. Returns employee + location info. |
| `/api/logout` | POST | Clear active login session. |
| `/api/whoami` | GET | Current active employee/location (consumed by glasses via preview response). |
| `/api/admin/location` | POST | Dev seed: create location. |
| `/api/admin/employee` | POST | Dev seed: create employee with optional PIN. |
| `/api/qa.html` | GET | Q&A agent dashboard. Employee types a business-insight question; the agent calls 6 tools and renders the answer + tool trace + sources. Maintains conversation history for follow-ups. |
| `/api/qa/ask` | POST | JSON `{question, filters?, history?}`. Runs the tool-calling loop, returns `{answer, short_answer, trace, sources, iterations, elapsed_ms}`. |
| `/api/qa/ask-audio` | POST | Multipart `audio=<wav>`. Transcribes → agent. For glasses voice Q&A (not currently used — glasses use `/api/qa/audio-stream` + `/api/qa/ask` instead). |
| `/api/qa/audio-stream` | WS | Glasses Q&A live ASR stream (decoupled from customer sessions). Relays PCM to Doubao, returns partial/final transcripts. |
| `/api/qa/last-glasses` | GET | Recent glasses Q&A turns (newest first) for the dashboard panel. Auto-refreshes every 4s. |
| `/api/qa/filters` | GET | Dropdown options (locations, employees, top topics, date range) for the Q&A filter bar. |
| `/api/qa/suggestions` | GET | Suggested-question chips from real DB stats. `?creative=1` for LLM-generated. |
| `/api/qa/snapshot` | GET | Pre-baked stats dashboard (totals, conversion, top topics/employees/locations). `?narrative=1` adds an LLM summary. |

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

- `shiftmind.sqlite3` — customers, face images, recognition events, customer sessions.
- `milvus/shiftmind.db` — Milvus Lite vector store (`face_embeddings` + `session_memories` collections).
- `faces/*.jpg` — saved enrollment crops.
- `audio/*.wav` — uploaded customer-session voice recordings.

## Session memory model

ShiftMind keeps per-interaction notes separate from the long-lived memory card:

| Field | Scope | Purpose |
| :--- | :--- | :--- |
| `customer_sessions.summary` | One session | Immutable summary of the completed interaction. |
| `customer_sessions.important_points_json` | One session | Extracted facts from that interaction. |
| `customer_sessions.topics_json` | One session | 1–5 lowercase topic tags (e.g. `["returns","warranty"]`), used for "popular topic" queries. |
| `customer_sessions.outcome` | One session | `converted` / `not_converted` / `unknown` — drives conversion-rate questions. |
| `customer_sessions.duration_seconds` | One session | Conversation length, derived at finish — drives "longest conversation" queries. |
| `customers.memory_card` | Customer lifetime | Concise cumulative memory shown on the glasses HUD. |

At session finish, the server uses the configured OpenAI-compatible chat
completion API to summarize the transcript as JSON (summary, important
points, name, topics, outcome), then re-summarizes the cumulative
`customers.memory_card` so it stays short enough for AR display, and upserts
a `session_memories` vector (summary + points) into Milvus Lite for semantic
recall by the Q&A agent. If LLM credentials are missing or unavailable, the
fallback keeps a bounded summary rather than appending unbounded raw
transcripts.

## Q&A agent

`/api/qa.html` exposes a tool-calling agent over the collected interaction
history. An OpenAI-compatible chat loop picks between **six tools** per question:

1. **`run_sql`** — read-only SELECT against SQLite (counts, rates, rankings, time
   trends, filters by employee/location/outcome/date). Driver-level `mode=ro`
   plus a statement guard and a 50-row cap; writes/DDL/multi-statement rejected.
2. **`vector_search`** — semantic search over the `session_memories` Milvus
   collection (topical/free-form recall), filterable by employee/location/date.
3. **`search_customers`** — resolve a customer by name or topic keywords.
4. **`customer_profile`** — pull full customer details (sessions, topics, outcomes,
   memory_card, preferences, first/last seen). For "tell me about the customer who…".
5. **`compare`** — side-by-side stats for two employees or two locations.
6. **`find_outliers`** — anomalies: longest sessions, employees far from mean
   conversion, topics with abnormally long discussions.

Max 6 iterations per query. Tool results capped at 4000 chars. JSON mode output parsing.

The page renders the agent's markdown answer (lists, bold, code), shows a
tool-call trace + source sessions, and exposes:

- **Filters** (location, employee, topic, date range, simulated/real toggle) —
  passed to the agent as a MANDATORY SCOPE block for `run_sql` and applied
  programmatically to `vector_search`. Filters also scope the overview +
  suggestions. `GET /api/qa/filters` populates the dropdowns.
- **Suggested questions** — generated from real (scoped) DB stats; "🎲 surprise
  me" asks the LLM for creative dataset-specific questions.
  `GET /api/qa/suggestions?creative=1`.
- **Overview dashboard** — `GET /api/qa/snapshot?narrative=1` returns scoped
  totals, conversion rate, avg duration, top topics/employees/locations, and an
  LLM narrative; rendered as stat cards.
- **Conversation memory** — prior turns are sent with each ask so follow-ups
  ("and for last week?") resolve against context. "↺ clear chat" resets.
- **`short_answer`** — every `/api/qa/ask` response includes a ≤140-char
  plain-text distillation for the glasses AR HUD (matches the
  `liveTranscript` budget). Glasses render this in Q&A mode.
- **Glasses Q&A log** — bottom panel shows recent spoken questions from glasses
  with full answers. Auto-refreshes every 4s via `GET /api/qa/last-glasses`.

### Glasses integration

Swipe-forward gesture on touchpad enters Q&A mode:
1. Single-tap → record voice question (live ASR via `WS /api/qa/audio-stream`)
2. Tap again → send transcript + conversation history to `POST /api/qa/ask`
3. Display `short_answer` on AR HUD (≤140 chars); full answer visible on server dashboard
4. History maintained for 20 turns (10 Q&A pairs) for contextual follow-ups
5. Swipe-forward again → exit Q&A mode, clear history

Simulated dataset for paper testing (`scripts/seed_simulated.py`, prefix-isolated
`sim-*` cohort, non-destructive):

```bash
uv run python -m scripts.seed_simulated --months 6 --sessions 200   # spans ~6 months
uv run python -m scripts.normalize_sim_data                          # canonicalize outcomes
uv run python -m scripts.clean_test_artifacts                        # remove dev/test-fixture rows
```

Backfill older sessions (summarized before topics/outcome existed) with:

```bash
uv run python -m scripts.backfill_qa --dry-run
uv run python -m scripts.backfill_qa --limit 10
uv run python -m scripts.backfill_qa --reindex   # re-embed ALL sessions into Milvus
```

`customer_sessions` / `customers` / `employees` carry an `is_simulated` flag
(1 = seeded demo data, 0 = real capture). Simulated data is included in
queries by default; the agent filters to `is_simulated = 0` only when you
explicitly ask for "real"/"production" data. Normalize a legacy seed
dataset's free-form outcomes onto the canonical enum and flag `sim-*` rows
with:

```bash
uv run python -m scripts.normalize_sim_data
```

The glasses HUD treats stored memory and current speech as separate surfaces:
`CustomerCard.memoryLine` is previous-session memory, while
`CustomerCard.liveTranscript` is only the current session transcript. Do not
reuse one field for both, or the HUD will flicker between old memory and live
speech.

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

## Tests

```bash
uv run pytest
RUN_LIVE_SESSION_TEST=1 uv run pytest tests/test_live_session.py
```

The live-session test is opt-in because it needs a running server on
`localhost:8000` plus an active glasses customer session.

## What's NOT here yet

- Streaming / low-latency STT; session audio uploads after button stop.
- Trigger classifier — phase 6
- VLM relay (`/api/chat`) — phase 6

See `../CLAUDE.md` and vault `Projects/ShiftMind/architecture_v2.md` for the full design.
