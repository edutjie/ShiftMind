"""Backfill Q&A-agent fields for old ended sessions.

Old sessions were summarized before the summarizer emitted `topics`/`outcome`,
and before the Milvus `session_memories` collection existed. This script
re-derives topics/outcome from the stored transcript and upserts a memory
vector, so the Q&A agent has data to query from day one.

Usage:
    uv run python -m scripts.backfill_qa --dry-run
    uv run python -m scripts.backfill_qa --limit 10
    uv run python -m scripts.backfill_qa

Idempotent: a session already in Milvus is not re-embedded; sessions with
non-empty topics_json + outcome are skipped entirely.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the server package importable when run via `python -m scripts.backfill_qa`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from session_memory_service import _embedding_client, get_memory_service  # noqa: E402
from session_service import _openai_client, _summarize_session  # noqa: E402
from storage import _duration_seconds, storage  # noqa: E402


def _needs_backfill(row) -> bool:
    topics = (row["topics_json"] or "").strip()
    outcome = (row["outcome"] or "").strip()
    return outcome == "" or topics in ("", "[]")


def _ended_sessions_needing_backfill(limit: int | None) -> list[dict]:
    sql = (
        "SELECT id, customer_id, employee_id, location_id, transcript, summary, "
        "important_points_json, started_at, ended_at, topics_json, outcome "
        "FROM customer_sessions WHERE status = 'ended'"
    )
    with storage.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
    needy = [r for r in rows if _needs_backfill(r)]
    return needy[:limit] if limit else needy


def _all_ended_sessions(limit: int | None) -> list[dict]:
    sql = (
        "SELECT id, customer_id, employee_id, location_id, transcript, summary, "
        "important_points_json, started_at, ended_at, topics_json, outcome "
        "FROM customer_sessions WHERE status = 'ended'"
    )
    with storage.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
    return rows[:limit] if limit else rows


def reindex_memories(*, limit: int | None = None, dry_run: bool = False) -> dict:
    """Re-embed every ended session into Milvus from stored summary+points.

    Use after normalizing outcomes (or changing embedding model) to bring the
    session_memories collection back in sync with SQLite. Does NOT touch SQL
    columns. Re-embeds unconditionally (ignores has_session).
    """
    if _embedding_client() is None:
        print("[reindex] SHIFTMIND_EMBED_API_KEY (or shared fallback) not set — cannot embed.")
        return {"embedded": 0, "errors": 0, "dry_run": dry_run, "no_key": True}

    sessions = _all_ended_sessions(limit)
    print(f"[reindex] {len(sessions)} session(s)" + (f" (limit {limit})" if limit else "") + (" [DRY RUN]" if dry_run else ""))

    embedded = errors = 0
    mem = get_memory_service()
    for i, row in enumerate(sessions, 1):
        sid = row["id"]
        content = _build_content(row)
        if not content or content.startswith("Session ended."):
            continue
        if dry_run:
            continue
        try:
            mem.upsert_memory(
                memory_id=f"mem-{sid}",
                session_id=sid,
                customer_id=str(row["customer_id"]),
                employee_id=row["employee_id"],
                location_id=row["location_id"],
                content=content,
                outcome=str(row["outcome"] or "unknown"),
                started_at_iso=str(row["started_at"] or ""),
            )
            embedded += 1
            if i % 25 == 0:
                print(f"  [{i}/{len(sessions)}] re-embedded")
        except Exception as exc:
            errors += 1
            print(f"  [{i}/{len(sessions)}] {sid}: ERROR {type(exc).__name__}: {exc}")
    summary = {"embedded": embedded, "errors": errors, "dry_run": dry_run}
    print(f"[reindex] done: {summary}")
    return summary


def backfill(*, limit: int | None = None, dry_run: bool = False) -> dict:
    if _openai_client() is None:
        print("[backfill] OPENAI_API_KEY not set — only SQL columns can be defaulted, no LLM re-extraction.")
        print("[backfill] set the key (or SHIFTMIND_LLM_API_KEY) and re-run for real topics/outcome.")
        return {"revisited": 0, "embedded": 0, "skipped": 0, "errors": 0, "dry_run": dry_run, "no_key": True}

    sessions = _ended_sessions_needing_backfill(limit)
    print(f"[backfill] {len(sessions)} session(s) need topics/outcome" + (f" (limit {limit})" if limit else "") + (" [DRY RUN]" if dry_run else ""))

    revisited = embedded = skipped = errors = 0
    for i, row in enumerate(sessions, 1):
        sid = row["id"]
        try:
            if dry_run:
                has_mem = get_memory_service().has_session(sid)
                print(f"  [{i}/{len(sessions)}] {sid}: would re-extract topics/outcome via LLM, "
                      f"{'milvus memory exists' if has_mem else 'would embed into milvus'}")
                continue

            summary_data = _summarize_session(
                customer_id=str(row["customer_id"]),
                session_id=sid,
                existing_name="",
                existing_memory="",
                is_new_customer=False,
                transcript=str(row["transcript"] or ""),
            )
            summary_data.pop("_llm_status", None)
            topics = [str(t).strip() for t in summary_data.get("topics", []) if str(t).strip()]
            outcome = str(summary_data.get("outcome") or "unknown")

            # Persist only the new columns + duration; leave existing summary/points untouched.
            duration = _duration_seconds(str(row["started_at"] or ""), str(row["ended_at"] or ""))
            with storage.connect() as conn:
                conn.execute(
                    "UPDATE customer_sessions SET topics_json = ?, outcome = ?, duration_seconds = COALESCE(duration_seconds, ?) WHERE id = ?",
                    (json.dumps(topics, ensure_ascii=False), outcome, duration, sid),
                )
            revisited += 1

            # Upsert Milvus memory from the STORED summary + points (what the UI shows).
            content = _build_content(row)
            if content and not get_memory_service().has_session(sid):
                get_memory_service().upsert_memory(
                    memory_id=f"mem-{sid}",
                    session_id=sid,
                    customer_id=str(row["customer_id"]),
                    employee_id=row["employee_id"],
                    location_id=row["location_id"],
                    content=content,
                    outcome=outcome,
                    started_at_iso=str(row["started_at"] or ""),
                )
                embedded += 1
            else:
                skipped += 1

            print(f"  [{i}/{len(sessions)}] {sid}: topics={topics} outcome={outcome}" + (" (mem exists)" if content and get_memory_service().has_session(sid) else ""))
        except Exception as exc:
            errors += 1
            print(f"  [{i}/{len(sessions)}] {sid}: ERROR {type(exc).__name__}: {exc}")

    summary = {"revisited": revisited, "embedded": embedded, "skipped": skipped, "errors": errors, "dry_run": dry_run}
    print(f"[backfill] done: {summary}")
    return summary


def _build_content(row: dict) -> str:
    summary = str(row.get("summary") or "").strip()
    try:
        points = json.loads(row.get("important_points_json") or "[]")
    except json.JSONDecodeError:
        points = []
    points = [str(p).strip() for p in points if str(p).strip()]
    return (summary + "\n" + "; ".join(points)).strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill Q&A topics/outcome + Milvus memories for ended sessions.")
    ap.add_argument("--limit", type=int, default=None, help="max sessions to process (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="report what would happen, write nothing")
    ap.add_argument("--reindex", action="store_true", help="re-embed ALL ended sessions into Milvus from stored data (sync outcomes)")
    args = ap.parse_args()
    if args.reindex:
        reindex_memories(limit=args.limit, dry_run=args.dry_run)
    else:
        backfill(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
