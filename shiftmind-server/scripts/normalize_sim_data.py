"""Normalize the simulated dataset: flag sim rows + canonicalize outcomes.

Two jobs:
1. Set is_simulated=1 on every `sim-*` row (customers, employees,
   customer_sessions) so the Q&A agent can tell demo data from real data.
2. Map legacy free-form outcome values (from the seed generator) onto the
   canonical enum the summarizer + agent expect:
       purchased, price_match      -> converted
       returned, complaint,
       browsed, inquiry            -> not_converted
       support, warranty, NULL, *  -> unknown

Re-running is safe (idempotent). Does NOT touch Milvus — after this, re-run
`python -m scripts.backfill_qa` (drop the session_memories collection first
to force re-embed) to sync vectors with the new outcomes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from storage import storage  # noqa: E402

OUTCOME_MAP = {
    "purchased": "converted",
    "price_match": "converted",
    "returned": "not_converted",
    "complaint": "not_converted",
    "browsed": "not_converted",
    "inquiry": "not_converted",
    "support": "unknown",
    "warranty": "unknown",
}
_CANONICAL = {"converted", "not_converted", "unknown"}


def flag_simulated() -> dict[str, int]:
    counts: dict[str, int] = {}
    with storage.connect() as conn:
        for table, idcol in (("customers", "id"), ("employees", "id"), ("customer_sessions", "id")):
            cur = conn.execute(
                f"UPDATE {table} SET is_simulated = 1 WHERE {idcol} LIKE 'sim-%' AND is_simulated = 0"
            )
            counts[table] = cur.rowcount
            conn.commit()
    return counts


def normalize_outcomes() -> dict[str, int]:
    counts: dict[str, int] = {}
    with storage.connect() as conn:
        # Sessions not yet ended (active/aborted) — leave outcome alone.
        rows = conn.execute(
            "SELECT id, outcome FROM customer_sessions WHERE status = 'ended'"
        ).fetchall()
        for r in rows:
            current = (r["outcome"] or "").strip().lower()
            if current in _CANONICAL:
                continue
            mapped = OUTCOME_MAP.get(current, "unknown")
            conn.execute(
                "UPDATE customer_sessions SET outcome = ? WHERE id = ?",
                (mapped, r["id"]),
            )
            counts[mapped] = counts.get(mapped, 0) + 1
        conn.commit()
    return counts


def main() -> None:
    flagged = flag_simulated()
    print(f"[normalize] flagged is_simulated=1: {flagged}")
    mapped = normalize_outcomes()
    print(f"[normalize] outcomes mapped: {mapped}")
    with storage.connect() as conn:
        for r in conn.execute(
            "SELECT outcome, COUNT(*) n FROM customer_sessions WHERE status='ended' GROUP BY outcome ORDER BY n DESC"
        ):
            print(f"  outcome {r['outcome']!r}: {r['n']}")


if __name__ == "__main__":
    main()
