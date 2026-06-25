# ShiftMind Memory

Durable implementation lessons for this repo. Keep high-signal decisions here;
do not use one-off bug reports as permanent docs.

## Session Summaries And Memory Cards

- `customer_sessions.summary` is per-session and immutable after that session
  ends.
- `customer_sessions.important_points_json` stores facts extracted from one
  interaction.
- `customers.memory_card` is the cumulative, user-facing HUD memory. Keep it
  concise and merge redundant facts instead of appending raw transcripts.
- LLM summarization uses OpenAI-compatible chat completions with JSON output.
  If credentials are missing or the LLM call fails, store bounded fallback text
  rather than an unbounded transcript dump.

## HUD Memory Versus Transcript

- `CustomerCard.memoryLine` is stored memory from previous sessions.
- `CustomerCard.liveTranscript` is current-session speech.
- Do not reuse `memoryLine` for live ASR updates; that causes the HUD to flicker
  between old memory and current transcript.

## Tests

- Server tests live under `shiftmind-server/tests/`.
- `uv run pytest` should pass without needing a running server.
- `tests/test_live_session.py` is opt-in behind `RUN_LIVE_SESSION_TEST=1`
  because it needs `localhost:8000` and an active glasses session.
