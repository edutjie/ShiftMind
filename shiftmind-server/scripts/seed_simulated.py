"""Seed the DB with LLM-generated simulated interactions (consumer-electronics store).

Populates a prefix-isolated `sim-*` cohort — locations, employees, customers
(with placeholder face embeddings), and ~150 ended customer sessions whose
transcripts are LLM-generated and then run through the REAL summarizer
(`_summarize_session`) + memory-card rewrite (`_update_memory_card_with_llm`)
+ semantic-memory upsert (`session_memories`). The result is indistinguishable
from live pipeline output, giving the Q&A agent realistic data to query.

Best Buy was only an example of the shop TYPE — this script uses a generic
big-box consumer-electronics store framing and no real brand names.

Usage:
    uv run python -m scripts.seed_simulated --dry-run      # keyless: print planned distribution
    uv run python -m scripts.seed_simulated                # needs OPENAI_API_KEY in .env
    uv run python -m scripts.seed_simulated --sessions 50 --no-reset

Prerequisite: shiftmind-server/.env with OPENAI_API_KEY (or SHIFTMIND_LLM_API_KEY).
Re-runs wipe only the `sim-*` cohort first (SQLite + both Milvus collections);
non-sim rows (e.g. Maya/Joan) are untouched.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from collections import Counter, defaultdict

# Make the server package importable when run via `python -m scripts.seed_simulated`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import numpy as np  # noqa: E402

from face_recognition_service import EMBED_DIM as FACE_DIM, get_face_service  # noqa: E402
from session_memory_service import get_memory_service  # noqa: E402
from session_service import (  # noqa: E402
    _openai_client,
    _summarize_session,
    _update_memory_card_with_llm,
)
from storage import _duration_seconds, _now_iso, storage  # noqa: E402

SIM = "sim"
FACE_COLLECTION = "face_embeddings"
MEM_COLLECTION = "session_memories"

# Weighted outcome distribution for a big-box tech store. Not everyone buys.
OUTCOMES = {
    "purchased": 0.35,
    "inquiry": 0.18,
    "support": 0.12,
    "complaint": 0.10,
    "returned": 0.08,
    "price_match": 0.06,
    "warranty": 0.05,
    "browsed": 0.06,
}
PRODUCTS = [
    "laptops", "phones", "TVs", "audio", "smart-home",
    "appliances", "gaming", "accessories", "cameras", "networking",
]
# Typical interaction length (minutes) by outcome — drives realistic durations.
DURATION_MIN = {
    "purchased": (6, 18),
    "inquiry": (4, 12),
    "support": (10, 30),
    "complaint": (10, 25),
    "returned": (6, 16),
    "price_match": (8, 18),
    "warranty": (6, 14),
    "browsed": (2, 7),
}


# ── helpers ───────────────────────────────────────────────────────────────

def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _pick_weighted(rng: np.random.Generator, weighted: dict[str, float]) -> str:
    keys = list(weighted.keys())
    probs = np.array([weighted[k] for k in keys], dtype=float)
    probs = probs / probs.sum()
    return str(rng.choice(keys, p=probs))


def _norm_vec(rng: np.random.Generator, dim: int) -> list[float]:
    v = rng.normal(size=dim).astype(np.float32)
    n = float(np.linalg.norm(v)) or 1.0
    return (v / n).tolist()


def _ts_pair(rng: np.random.Generator, months: int = 6) -> tuple[str, str, float]:
    """A random (started, ended, duration_seconds) within the last ~`months` months."""
    now = time.time()
    started = now - rng.integers(0, months * 30 * 24 * 3600)
    return int(started), None, 0.0


def _llm_model() -> str:
    import os
    return os.getenv("SHIFTMIND_LLM_MODEL", "gpt-4o-mini")


def _chat_json(client, system: str, user: str) -> dict:
    """One chat-completions call returning parsed JSON (response_format=json_object)."""
    resp = client.chat.completions.create(
        model=_llm_model(),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=0.8,
    )
    return json.loads(resp.choices[0].message.content or "{}")


# ── cohort reset ──────────────────────────────────────────────────────────

def reset_cohort() -> None:
    """Wipe the sim-* cohort from SQLite + both Milvus collections (FK-safe)."""
    print("[reset] wiping sim-* cohort …")
    with storage.connect() as conn:
        conn.execute("DELETE FROM customer_sessions WHERE id LIKE 'sim-%'")
        conn.execute("DELETE FROM face_images WHERE customer_id LIKE 'sim-%'")
        conn.execute("DELETE FROM customers WHERE id LIKE 'sim-%'")
        conn.execute("DELETE FROM employees WHERE id LIKE 'sim-%'")
        conn.execute("DELETE FROM locations WHERE id LIKE 'sim-%'")
    try:
        get_face_service().client.delete(
            collection_name=FACE_COLLECTION, filter='customer_id like "sim-%"'
        )
    except Exception as exc:
        print(f"[reset] face_embeddings delete skipped: {type(exc).__name__}: {exc}")
    try:
        get_memory_service().client.delete(
            collection_name=MEM_COLLECTION, filter='session_id like "sim-%"'
        )
    except Exception as exc:
        print(f"[reset] session_memories delete skipped: {type(exc).__name__}: {exc}")


# ── name + transcript generation ─────────────────────────────────────────

def gen_names(client, kind: str, n: int) -> list[str]:
    system = (
        "You generate realistic names for a simulated consumer-electronics "
        "(big-box tech) store scenario. Use NO real brand names. Return ONLY JSON."
    )
    user = (
        f"Generate {n} realistic {kind}. Vary demographics. "
        'Return JSON like {"names": ["...", "..."]}.'
    )
    return _chat_json(client, system, user).get("names", [])[:n]


def gen_transcripts(client, profiles: list[dict]) -> list[str]:
    """Batch-generate realistic dialogue for a list of session profiles."""
    system = (
        "You write realistic customer<->salesperson dialogue for a big-box "
        "consumer-electronics store (no real brand names). Each interaction must "
        "clearly reflect its given outcome and product. A 'complaint' sounds "
        "frustrated, 'browsed' is short and non-committal, 'support' is "
        "troubleshooting, 'purchased' ends with a sale, etc. 6-14 lines each. "
        "Return ONLY JSON."
    )
    user = (
        "Write one dialogue per profile, in order. Profiles:\n"
        + json.dumps(profiles, ensure_ascii=False)
        + '\n\nReturn JSON like {"items":[{"transcript":"..."},...]}.'
    )
    items = _chat_json(client, system, user).get("items", [])
    return [str(it.get("transcript", "")).strip() for it in items]


# ── build phases ─────────────────────────────────────────────────────────

def build_locations(client, n: int) -> list[dict]:
    names = gen_names(client, "store location names (e.g. 'Downtown Electronics')", n)
    out = []
    now = _now_iso()
    with storage.connect() as conn:
        for i in range(n):
            name = (names[i] if i < len(names) else f"Store #{i+1}").strip() or f"Store #{i+1}"
            lid = f"{SIM}-loc-{i+1:03d}"
            conn.execute(
                "INSERT INTO locations (id, name, created_at) VALUES (?, ?, ?)", (lid, name, now)
            )
            out.append({"id": lid, "name": name})
    return out


def build_employees(client, n: int, locations: list[dict]) -> list[dict]:
    names = gen_names(client, "salesperson first and last names", n)
    out = []
    now = _now_iso()
    with storage.connect() as conn:
        for i in range(n):
            name = (names[i] if i < len(names) else f"Employee {i+1}").strip() or f"Employee {i+1}"
            eid = f"{SIM}-emp-{i+1:03d}"
            home = locations[i % len(locations)]["id"]
            conn.execute(
                "INSERT INTO employees (id, name, pin_hash, home_location_id, created_at) "
                "VALUES (?, ?, NULL, ?, ?)",
                (eid, name, home, now),
            )
            out.append({"id": eid, "name": name, "home_location_id": home})
    return out


def build_customers(client, n: int, rng: np.random.Generator) -> list[dict]:
    names = gen_names(client, "customer first and last names with varied demographics", n)
    face = get_face_service()
    out = []
    now = int(time.time())
    for i in range(n):
        name = names[i] if i < len(names) else f"Customer {i+1}"
        cid = f"{SIM}-cust-{i+1:03d}"
        storage.upsert_customer(customer_id=cid, name=name, memory_card="", preferences=[])
        face.client.upsert(
            collection_name=FACE_COLLECTION,
            data=[{
                "embedding_id": f"{SIM}-emb-{i+1:03d}",
                "customer_id": cid,
                "vector": _norm_vec(rng, FACE_DIM),
                "model_name": "sim-placeholder",
                "quality": 0.0,
                "created_at": now,
            }],
        )
        out.append({"id": cid, "name": name, "memory_card": ""})
    return out


def _sample_profiles(rng, n_sessions, customers, employees, locations, months=6):
    """Sample n_sessions session profiles; customer visits follow a long tail.

    `started` is spread uniformly over the last `months` months so trend
    questions (monthly conversion, busiest month) have something to chew on.
    """
    # weight customers so some are regulars, many are one-timers
    cust_ids = [c["id"] for c in customers]
    cust_weights = rng.dirichlet(np.ones(len(cust_ids)) * 0.6).tolist()
    span_seconds = int(months * 30 * 24 * 3600)
    profiles = []
    for _ in range(n_sessions):
        cust_id = str(rng.choice(cust_ids, p=cust_weights))
        profiles.append({
            "customer_id": cust_id,
            "employee": rng.choice(employees),
            "location": rng.choice(locations),
            "outcome": _pick_weighted(rng, OUTCOMES),
            "product": str(rng.choice(PRODUCTS)),
            "started": int(time.time()) - int(rng.integers(0, span_seconds)),
        })
    return profiles


def build_sessions(client, profiles, customers_by_id, rng, batch=5):
    face_done = {c["id"] for c in customers_by_id.values()}
    cust_names = {cid: c["name"] for cid, c in customers_by_id.items()}
    # order so each customer's sessions are chronological (for cumulative memory card)
    profiles.sort(key=lambda p: (p["customer_id"], p["started"]))

    results = []
    i = 0
    while i < len(profiles):
        chunk = profiles[i:i + batch]
        # build LLM prompt profiles (include customer name + current memory card)
        llm_profiles = []
        for p in chunk:
            c = customers_by_id[p["customer_id"]]
            llm_profiles.append({
                "outcome": p["outcome"],
                "product": p["product"],
                "employee_name": p["employee"]["name"],
                "customer_name": c["name"],
                "customer_memory": c["memory_card"] or "(first visit)",
            })
        try:
            transcripts = gen_transcripts(client, llm_profiles)
        except Exception as exc:
            print(f"[gen] transcript batch failed ({type(exc).__name__}: {exc}); skipping {len(chunk)}")
            transcripts = [""] * len(chunk)

        for p, transcript in zip(chunk, transcripts):
            if not transcript:
                i += 1
                continue
            c = customers_by_id[p["customer_id"]]
            is_new = c["memory_card"] == ""  # first recorded interaction for this customer
            # 1) real summarizer → summary / important_points / topics
            try:
                summ = _summarize_session(
                    customer_id=p["customer_id"],
                    session_id=None,
                    existing_name=c["name"],
                    existing_memory=c["memory_card"],
                    is_new_customer=is_new,
                    transcript=transcript,
                )
            except Exception as exc:
                print(f"[gen] summarize failed: {type(exc).__name__}: {exc}")
                summ = {"summary": transcript[:200], "important_points": [], "topics": []}
            summary = str(summ.get("summary") or "").strip() or transcript[:200]
            points = [str(x).strip() for x in summ.get("important_points", []) if str(x).strip()]
            topics = [str(x).strip() for x in summ.get("topics", []) if str(x).strip()]
            if not topics:
                topics = [p["product"], p["outcome"]]
            outcome = p["outcome"]  # ground-truth outcome (sampled), overrides summarizer

            # 2) timestamps + duration
            lo, hi = DURATION_MIN.get(outcome, (5, 15))
            mins = int(rng.integers(lo, hi + 1))
            started = p["started"]
            ended = started + mins * 60
            duration = _duration_seconds(_iso(started), _iso(ended)) or float(mins * 60)

            # 3) raw-insert ended session
            sid = f"{SIM}-sess-{i+1:04d}"
            with storage.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO customer_sessions
                      (id, customer_id, status, is_new_customer, transcript, summary,
                       important_points_json, topics_json, outcome, duration_seconds,
                       audio_path, employee_id, location_id, started_at, ended_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        sid, p["customer_id"], "ended", 1 if is_new else 0,
                        transcript, summary,
                        json.dumps(points, ensure_ascii=False),
                        json.dumps(topics, ensure_ascii=False),
                        outcome, duration,
                        p["employee"]["id"], p["location"]["id"],
                        _iso(started), _iso(ended),
                    ),
                )

            # 4) cumulative memory card (real LLM rewrite, uses prior sessions)
            try:
                history = storage.get_customer_session_history(p["customer_id"], limit=5)
                card = _update_memory_card_with_llm(
                    p["customer_id"], c["memory_card"], summary, points, session_history=history,
                )
            except Exception as exc:
                print(f"[gen] memory card failed: {type(exc).__name__}: {exc}")
                card = c["memory_card"] or summary[:120]
            storage.upsert_customer(
                customer_id=p["customer_id"], name=c["name"], memory_card=card, preferences=points,
            )
            customers_by_id[p["customer_id"]]["memory_card"] = card

            # 5) semantic memory (real 1536-D embed)
            content = (summary + "\n" + "; ".join(points)).strip()
            try:
                get_memory_service().upsert_memory(
                    memory_id=f"{SIM}-mem-{sid}",
                    session_id=sid,
                    customer_id=p["customer_id"],
                    employee_id=p["employee"]["id"],
                    location_id=p["location"]["id"],
                    content=content,
                    outcome=outcome,
                    started_at_iso=_iso(started),
                )
            except Exception as exc:
                print(f"[gen] session_memories upsert failed: {type(exc).__name__}: {exc}")

            results.append({
                "session_id": sid, "outcome": outcome, "product": p["product"],
                "employee": p["employee"]["name"], "location": p["location"]["name"],
                "duration": duration, "topics": topics,
            })
            i += 1
        print(f"[gen] {i}/{len(profiles)} sessions")
    return results


# ── reporting ─────────────────────────────────────────────────────────────

def print_summary(results, customers, employees, locations):
    print("\n=== seed summary ===")
    print(f"locations: {len(locations)}  employees: {len(employees)}  customers: {len(customers)}  sessions: {len(results)}")
    by_outcome = Counter(r["outcome"] for r in results)
    print("\nsessions by outcome:")
    for k, v in by_outcome.most_common():
        print(f"  {k:<12} {v:>4}  ({v/len(results)*100:4.1f}%)")
    by_emp = Counter(r["employee"] for r in results)
    print("\nsessions by employee (top 5):")
    for k, v in by_emp.most_common(5):
        print(f"  {k:<22} {v:>4}")
    topic_counter = Counter()
    for r in results:
        for t in r["topics"]:
            topic_counter[t] += 1
    print("\ntop topics:")
    for k, v in topic_counter.most_common(10):
        print(f"  {k:<14} {v:>4}")
    # conversion rate by employee (purchased / total)
    totals = defaultdict(int); bought = defaultdict(int)
    for r in results:
        totals[r["employee"]] += 1
        if r["outcome"] == "purchased":
            bought[r["employee"]] += 1
    print("\nconversion rate by employee:")
    for emp, tot in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {emp:<22} {bought[emp]}/{tot}  ({bought[emp]/tot*100:4.1f}%)")


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Seed simulated ShiftMind interactions.")
    ap.add_argument("--sessions", type=int, default=200)
    ap.add_argument("--customers", type=int, default=50)
    ap.add_argument("--employees", type=int, default=8)
    ap.add_argument("--locations", type=int, default=3)
    ap.add_argument("--months", type=int, default=6, help="span sessions over the last N months (trend data)")
    ap.add_argument("--batch", type=int, default=5, help="transcripts per LLM call")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--reset", dest="reset", action="store_true", default=True,
                    help="wipe sim-* cohort first (default on)")
    ap.add_argument("--no-reset", dest="reset", action="store_false",
                    help="keep existing sim-* cohort")
    ap.add_argument("--dry-run", action="store_true", help="no LLM, no writes; print planned distribution")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)

    if args.dry_run:
        print("[dry-run] sampling planned distribution (no LLM, no writes) …")
        fake_emps = [{"id": f"{SIM}-emp-{i+1:03d}", "name": f"Employee{i+1}", "home_location_id": ""} for i in range(args.employees)]
        fake_locs = [{"id": f"{SIM}-loc-{i+1:03d}", "name": f"Store{i+1}"} for i in range(args.locations)]
        fake_custs = [{"id": f"{SIM}-cust-{i+1:03d}", "name": f"Customer{i+1}", "memory_card": ""} for i in range(args.customers)]
        profiles = _sample_profiles(rng, args.sessions, fake_custs, fake_emps, fake_locs, months=args.months)
        results = [{
            "outcome": p["outcome"], "product": p["product"],
            "employee": p["employee"]["name"], "location": p["location"]["name"],
            "duration": float(rng.integers(*DURATION_MIN[p["outcome"]]) * 60),
            "topics": [p["product"], p["outcome"]],
        } for p in profiles]
        print_summary(results, fake_custs, fake_emps, fake_locs)
        print("\n[dry-run] add OPENAI_API_KEY to .env, then run without --dry-run to seed for real.")
        return 0

    client = _openai_client()
    if client is None:
        print("[seed] OPENAI_API_KEY (or SHIFTMIND_LLM_API_KEY) not set — cannot generate.")
        print("[seed] create shiftmind-server/.env with the key, then re-run. Use --dry-run for a keyless preview.")
        return 2

    if args.reset:
        reset_cohort()

    print(f"[seed] generating {args.locations} locations / {args.employees} employees …")
    locations = build_locations(client, args.locations)
    employees = build_employees(client, args.employees, locations)
    print(f"[seed] generating {args.customers} customers (+ placeholder face embeddings) …")
    customers = build_customers(client, args.customers, rng)
    customers_by_id = {c["id"]: c for c in customers}

    print(f"[seed] generating {args.sessions} sessions (batch={args.batch}) …")
    profiles = _sample_profiles(rng, args.sessions, customers, employees, locations, months=args.months)
    results = build_sessions(client, profiles, customers_by_id, rng, batch=args.batch)

    print_summary(results, customers, employees, locations)
    print("\n[seed] done. Try the Q&A agent at http://localhost:8000/api/qa.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
