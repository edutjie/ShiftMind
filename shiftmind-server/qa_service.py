"""Agentic Q&A over collected conversation data.

An OpenAI-compatible tool-calling loop. Given a natural-language question
from an employee, the LLM picks between two tools to answer:

- run_sql:        read-only SELECT against the SQLite customer/session DB.
- vector_search:  semantic search over the Milvus session_memories collection.

The agent decides which tool(s) to call per question (often both). The loop
returns the final answer plus a tool-call trace and source session_ids for
the UI to render.

Reuses session_service._openai_client for chat and the independently configured
session_memory_service client for embeddings/vector search.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

from storage import storage

MAX_ITERATIONS = 6
TOOL_RESULT_MAX_CHARS = 4000
SQL_ROW_CAP = 50
_QA_TABLES = {"customer_sessions", "customers", "employees", "locations", "json_each"}

logger = logging.getLogger("shiftmind.qa")

# Glasses-side Q&A turns are persisted to the `qa_turns` SQLite table (see
# storage.insert_qa_turn / recent_qa_turns) so history survives server restarts.
# Surfaced via GET /api/qa/last-glasses and the /api/qa.html "recent glasses
# questions" panel.

# Keywords that have no business in a read-only SELECT. Belt-and-suspenders
# on top of the driver-level mode=ro connection.
_FORBIDDEN_SQL_KEYWORDS = (
    "PRAGMA",
    "ATTACH",
    "DETACH",
    "CREATE",
    "ALTER",
    "DROP",
    "INSERT",
    "UPDATE",
    "DELETE",
    "REPLACE",
    "VACUUM",
    "REINDEX",
)

_SCHEMA_HINT = (
    "Available SQLite tables and columns:\n"
    "- customers(id, name, memory_card, preferences_json, created_at, updated_at)\n"
    "- customer_sessions(id, customer_id, status, is_new_customer, transcript, summary,\n"
    "    important_points_json, topics_json, outcome, duration_seconds, employee_id,\n"
    "    location_id, started_at, ended_at, is_simulated)\n"
    "- customers(id, name, memory_card, preferences_json, created_at, updated_at, is_simulated)\n"
    "- employees(id, name, home_location_id, created_at, is_simulated)\n"
    "- locations(id, name, created_at)\n"
    "\n"
    "is_simulated = 1 marks seeded demo data, 0 marks real captures. Simulated data is\n"
    "legitimate analysis data and is the BULK of the dataset — INCLUDE IT BY DEFAULT in\n"
    "every query. Only add WHERE is_simulated = 0 if the user EXPLICITLY asks for 'real',\n"
    "'production', or 'non-simulated' data.\n"
    "\n"
    "CRITICAL — topics_json and important_points_json are JSON-array TEXT columns, NOT\n"
    "regular columns. There is NO column named 'topic' or 'point'. To count/rank their\n"
    "values you MUST expand with json_each() in the FROM clause:\n"
    "  -- most popular topics:\n"
    "  SELECT value AS topic, COUNT(*) AS n\n"
    "  FROM customer_sessions, json_each(topics_json)\n"
    "  WHERE status='ended'\n"
    "  GROUP BY value ORDER BY n DESC LIMIT 10;\n"
    "  -- sessions mentioning a topic:\n"
    "  SELECT id, customer_id FROM customer_sessions WHERE EXISTS\n"
    "    (SELECT 1 FROM json_each(topics_json) WHERE value LIKE '%return%');\n"
    "\n"
    "outcome is TEXT with canonical values 'converted','not_converted','unknown'. Compute\n"
    "conversion rate as SUM(outcome='converted') / COUNT(*). duration_seconds is the\n"
    "conversation length in seconds. employee_id/location_id are nullable.\n"
    "\n"
    "ALWAYS join to return human-readable names, never bare ids:\n"
    "  SELECT e.name AS employee, AVG(s.duration_seconds) AS avg_dur\n"
    "  FROM customer_sessions s JOIN employees e ON e.id = s.employee_id\n"
    "  WHERE s.status='ended' GROUP BY e.name ORDER BY avg_dur DESC;\n"
    "  SELECT c.name AS customer, s.duration_seconds FROM customer_sessions s\n"
    "  JOIN customers c ON c.id = s.customer_id ORDER BY s.duration_seconds DESC LIMIT 5;\n"
    "When you GROUP BY employee/customer, group by the name (or id) AND select the name\n"
    "so the answer can name them. Mention the id in parentheses only if useful.\n"
    "\n"
    "TIME TRENDS — data spans months. For trend / over-time questions, group by month:\n"
    "  SELECT strftime('%Y-%m', started_at) AS month,\n"
    "         SUM(outcome='converted')*1.0/COUNT(*) AS conv, COUNT(*) AS n\n"
    "  FROM customer_sessions WHERE status='ended'\n"
    "  GROUP BY month ORDER BY month;\n"
    "Use this for 'is conversion improving?', 'busiest month', 'volume over time', etc."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Run a READ-ONLY SELECT query against the SQLite customer/session database. "
                "Use for counts, sums, averages, ranking, grouping, and filtering by exact "
                "values (employee, location, outcome, date range). Rejects any non-SELECT. "
                + _SCHEMA_HINT
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "A single SELECT (or WITH ... SELECT) statement.",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vector_search",
            "description": (
                "Semantic search over past session summaries + important points. Use for "
                "free-form or topical recall: 'who talked about returns', 'what do clients "
                "usually ask about', 'find sessions like X'. Returns ranked session_ids with "
                "scores and the matched text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language query to embed and search."},
                    "limit": {"type": "integer", "description": "Top-K matches (default 5, max 20).", "default": 5},
                    "employee_id": {"type": "string", "description": "Optional: restrict to sessions by this employee."},
                    "customer_id": {"type": "string", "description": "Optional: restrict to sessions by this customer."},
                    "location_id": {"type": "string", "description": "Optional: restrict to sessions at this location/store."},
                    "date_from": {"type": "string", "description": "Optional ISO date YYYY-MM-DD; only sessions started on/after it."},
                    "date_to": {"type": "string", "description": "Optional ISO date YYYY-MM-DD; only sessions started on/before it."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "customer_profile",
            "description": (
                "Full profile for ONE customer: session count, first/last seen, conversion rate, "
                "outcome breakdown, top topics, preferences, memory_card, and recent sessions. Use "
                "after search_customers to answer 'tell me about customer X' / 'what does this "
                "customer usually buy / discuss'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "The customer id (e.g. sim-cust-007, person-001)."},
                },
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_customers",
            "description": (
                "Find customers by name OR by a topic discussed in their sessions. Returns matching "
                "customer_ids + session counts. Use to resolve 'the customer who asks about wifi' "
                "before pulling a full profile."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A name fragment or topic (e.g. 'wifi', 'Maya', 'returns')."},
                    "limit": {"type": "integer", "description": "Max matches (default 10, max 20).", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare",
            "description": (
                "Side-by-side comparison of two employees OR two locations: session count, "
                "conversion rate, avg duration, top topics each. Use for 'compare X vs Y'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "enum": ["employee", "location"]},
                    "a": {"type": "string", "description": "First entity id."},
                    "b": {"type": "string", "description": "Second entity id."},
                },
                "required": ["entity_type", "a", "b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_outliers",
            "description": (
                "Surface anomalies: kind='duration' (longest sessions), 'conversion' (employees "
                "whose conversion rate is farthest from the mean), or 'topic' (topics with "
                "abnormally long discussions). Use for 'any outliers?' / 'what's unusual'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["duration", "conversion", "topic"], "default": "duration"},
                },
                "required": [],
            },
        },
    },
]


# ── tool: run_sql ──────────────────────────────────────────────────────────


def _normalize_filters(filters: dict[str, Any] | None) -> dict[str, str]:
    """Return the supported UI scope in a stable, validated representation."""
    raw = filters or {}
    out: dict[str, str] = {}
    for key in ("location_id", "employee_id", "customer_id", "topic", "date_from", "date_to"):
        value = str(raw.get(key) or "").strip()
        if value:
            out[key] = value
    simulated = str(raw.get("simulated") or "all").strip().lower()
    if simulated in {"real", "sim"}:
        out["simulated"] = simulated
    return out


def _scope_clause(filters: dict[str, Any] | None, alias: str = "s") -> tuple[str, list[Any]]:
    """Parameterized WHERE conditions shared by snapshots and vector post-filtering."""
    f = _normalize_filters(filters)
    clauses: list[str] = []
    params: list[Any] = []
    if f.get("location_id"):
        clauses.append(f"{alias}.location_id = ?")
        params.append(f["location_id"])
    if f.get("employee_id"):
        clauses.append(f"{alias}.employee_id = ?")
        params.append(f["employee_id"])
    if f.get("customer_id"):
        clauses.append(f"{alias}.customer_id = ?")
        params.append(f["customer_id"])
    if f.get("date_from"):
        clauses.append(f"{alias}.started_at >= ?")
        params.append(f"{f['date_from']}T00:00:00Z")
    if f.get("date_to"):
        clauses.append(f"{alias}.started_at <= ?")
        params.append(f"{f['date_to']}T23:59:59Z")
    if f.get("simulated"):
        clauses.append(f"{alias}.is_simulated = ?")
        params.append(0 if f["simulated"] == "real" else 1)
    if f.get("topic"):
        clauses.append(
            f"EXISTS (SELECT 1 FROM json_each({alias}.topics_json) "
            "WHERE LOWER(value) LIKE LOWER(?))"
        )
        params.append(f"%{f['topic']}%")
    return (" AND ".join(clauses) or "1=1"), params


def _scoped_session_ids(filters: dict[str, Any] | None) -> set[str] | None:
    """Allowed session ids, or None when no UI scope was requested."""
    normalized = _normalize_filters(filters)
    if not normalized:
        return None
    where, params = _scope_clause(normalized)
    with storage.connect() as conn:
        rows = conn.execute(
            f"SELECT s.id FROM customer_sessions s WHERE {where}", params
        ).fetchall()
    return {str(row["id"]) for row in rows}


@contextmanager
def _scoped_connection(filters: dict[str, Any] | None = None) -> Iterator[sqlite3.Connection]:
    """Yield an isolated SQLite snapshot containing only rows inside the UI scope.

    The agent can issue flexible SELECTs, but the connection contains only the four
    Q&A tables and only sessions (plus related dimension rows) allowed by the scope.
    """
    source = storage.connect()
    snapshot = sqlite3.connect(":memory:")
    snapshot.row_factory = sqlite3.Row
    try:
        source.backup(snapshot)
        snapshot.execute("PRAGMA foreign_keys = OFF")
        for table in ("face_images", "recognition_events"):
            snapshot.execute(f"DROP TABLE IF EXISTS {table}")

        where, params = _scope_clause(filters)
        snapshot.execute(
            f"DELETE FROM customer_sessions WHERE id NOT IN "
            f"(SELECT s.id FROM customer_sessions s WHERE {where})",
            params,
        )
        snapshot.execute(
            "DELETE FROM customers WHERE id NOT IN "
            "(SELECT DISTINCT customer_id FROM customer_sessions)"
        )
        snapshot.execute(
            "DELETE FROM employees WHERE id NOT IN "
            "(SELECT DISTINCT employee_id FROM customer_sessions WHERE employee_id IS NOT NULL)"
        )
        snapshot.execute(
            "DELETE FROM locations WHERE id NOT IN "
            "(SELECT DISTINCT location_id FROM customer_sessions WHERE location_id IS NOT NULL)"
        )
        snapshot.commit()

        def authorize(action: int, arg1: str | None, _arg2: str | None, _db: str | None, _trigger: str | None) -> int:
            if action == sqlite3.SQLITE_READ and arg1 not in _QA_TABLES:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        snapshot.set_authorizer(authorize)
        yield snapshot
    finally:
        source.close()
        snapshot.close()


def _tool_run_sql(sql: str, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a read-only SELECT. Always returns a dict, never raises."""
    # Strip leading line comments (-- ...) so the first token is the statement verb.
    stripped = re.sub(r"^\s*--[^\n]*\n", "", sql or "", flags=re.MULTILINE).strip()
    if not stripped:
        return {"error": "empty sql", "_rejected": True}

    first_token = stripped.split(None, 1)[0].upper()
    if first_token not in {"SELECT", "WITH"}:
        return {"error": f"only SELECT/WITH allowed (got {first_token})", "_rejected": True}

    # Reject anything beyond a single statement (drop trailing ';' then check for any remaining).
    body = stripped.rstrip()
    while body.endswith(";"):
        body = body[:-1].rstrip()
    if ";" in body:
        return {"error": "multi-statement queries are not allowed", "_rejected": True}

    for bad in _FORBIDDEN_SQL_KEYWORDS:
        if re.search(rf"\b{bad}\b", body, re.IGNORECASE):
            return {"error": f"disallowed keyword: {bad}", "_rejected": True}

    if re.search(r"\b(?:main|temp)\s*\.", body, re.IGNORECASE) or re.search(
        r"\bsqlite_", body, re.IGNORECASE
    ):
        return {"error": "schema-qualified and internal SQLite access is not allowed", "_rejected": True}

    try:
        with _scoped_connection(filters) as scoped:
            rows = scoped.execute(body).fetchmany(SQL_ROW_CAP + 1)
    except sqlite3.Error as exc:
        rejected = "not authorized" in str(exc).lower() or "no such table" in str(exc).lower()
        return {"error": f"sqlite_{type(exc).__name__}: {exc}", "_rejected": rejected}

    truncated = len(rows) > SQL_ROW_CAP
    data = [dict(r) for r in rows[:SQL_ROW_CAP]]
    # Collect candidate session ids from `session_id` or `id` columns. `id` from
    # a non-session table (e.g. customers) is harmless: _sources_for filters to
    # rows that actually exist in customer_sessions.
    session_ids = sorted(
        {
            str(r[col])
            for r in data
            for col in ("session_id", "id")
            if col in r and r[col]
        }
    )
    return {
        "columns": list(data[0].keys()) if data else [],
        "rows": data,
        "rowcount": len(data),
        "truncated": truncated,
        "_session_ids": session_ids,
    }


# ── tool: vector_search ────────────────────────────────────────────────────


def _date_to_epoch(date_str: str | None, *, end: bool = False) -> int | None:
    """'YYYY-MM-DD' (or ISO) -> epoch seconds. end=True -> 23:59:59 of that day."""
    if not date_str:
        return None
    s = str(date_str).strip()
    if len(s) == 10:  # YYYY-MM-DD
        s = s + ("T23:59:59Z" if end else "T00:00:00Z")
    try:
        return int(time.mktime(time.strptime(s[:20], "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def _tool_vector_search(
    query: str,
    limit: int = 5,
    employee_id: str | None = None,
    customer_id: str | None = None,
    location_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    simulated: str = "all",
    topic: str | None = None,
) -> dict[str, Any]:
    """Semantic search over session_memories. Always returns a dict, never raises.

    location/date are applied as Milvus expr (collection has those fields).
    `simulated` is post-filtered against SQLite (the collection has no such field):
    over-fetch limit*3 (capped at 20), drop non-matching session_ids, re-trim.
    """
    if not (query or "").strip():
        return {"error": "empty query", "_rejected": True}
    scope = {
        "employee_id": employee_id,
        "customer_id": customer_id,
        "location_id": location_id,
        "date_from": date_from,
        "date_to": date_to,
        "simulated": simulated,
        "topic": topic,
    }
    allowed_ids = _scoped_session_ids(scope)
    # Topic/simulation filters are not Milvus fields. Fetch the maximum small-MVP
    # result window, then intersect with SQLite's authoritative scoped ids.
    fetch_limit = 20 if allowed_ids is not None else (limit or 5)
    try:
        from session_memory_service import get_memory_service

        hits = get_memory_service().search(
            query=query,
            limit=fetch_limit,
            employee_id=employee_id,
            customer_id=customer_id,
            location_id=location_id,
            created_at_min=_date_to_epoch(date_from, end=False),
            created_at_max=_date_to_epoch(date_to, end=True),
        )
    except Exception as exc:
        return {"error": f"vector_search_failed: {type(exc).__name__}: {exc}", "_rejected": False}

    if allowed_ids is not None:
        hits = [h for h in hits if h.get("session_id") in allowed_ids]
    hits = hits[: max(limit, 1)]

    names = _customer_names_for([h.get("customer_id") for h in hits if h.get("customer_id")])
    enriched = []
    session_ids = []
    for h in hits:
        cid = h.get("customer_id")
        h["customer_name"] = names.get(cid)
        enriched.append(h)
        sid = h.get("session_id")
        if sid:
            session_ids.append(str(sid))
    return {"hits": enriched, "rowcount": len(enriched), "_session_ids": session_ids}


def _customer_names_for(customer_ids: list[str]) -> dict[str, str]:
    if not customer_ids:
        return {}
    out: dict[str, str] = {}
    try:
        with storage.connect() as conn:
            placeholders = ",".join(["?"] * len(customer_ids))
            rows = conn.execute(
                f"SELECT id, name FROM customers WHERE id IN ({placeholders})",
                customer_ids,
            ).fetchall()
        out = {str(r["id"]): str(r["name"]) for r in rows}
    except sqlite3.Error:
        pass
    return out


# ── tool: customer_profile + search_customers ──────────────────────────────


def _tool_customer_profile(
    customer_id: str, filters: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Full profile for one customer across all their ended sessions."""
    cid = (customer_id or "").strip()
    if not cid:
        return {"error": "empty customer_id", "_rejected": True}
    try:
        with _scoped_connection(filters) as conn:
            cust = conn.execute(
                "SELECT id, name, memory_card, preferences_json FROM customers WHERE id = ?", (cid,)
            ).fetchone()
            if not cust:
                return {"error": f"no customer with id {cid}", "_rejected": True}
            sess = conn.execute(
                "SELECT COUNT(*) n, MIN(started_at) first, MAX(started_at) last, "
                "SUM(outcome='converted')*1.0/COUNT(*) conv, AVG(duration_seconds) avgdur "
                "FROM customer_sessions WHERE customer_id = ? AND status='ended'",
                (cid,),
            ).fetchone()
            outcome = {
                r["outcome"] or "unknown": int(r["n"])
                for r in conn.execute(
                    "SELECT outcome, COUNT(*) n FROM customer_sessions "
                    "WHERE customer_id = ? AND status='ended' GROUP BY outcome",
                    (cid,),
                ).fetchall()
            }
            topics = [
                {"topic": r["value"], "n": int(r["n"])}
                for r in conn.execute(
                    "SELECT value, COUNT(*) n FROM customer_sessions, json_each(topics_json) "
                    "WHERE customer_id = ? AND status='ended' GROUP BY value ORDER BY n DESC LIMIT 10",
                    (cid,),
                ).fetchall()
            ]
            recent = [
                {
                    "session_id": r["id"], "started_at": r["started_at"], "ended_at": r["ended_at"],
                    "outcome": r["outcome"], "duration_seconds": r["duration_seconds"],
                    "summary": (r["summary"] or "")[:160],
                    "employee_name": r["employee_name"], "location_name": r["location_name"],
                }
                for r in conn.execute(
                    "SELECT s.id, s.started_at, s.ended_at, s.outcome, s.duration_seconds, s.summary, "
                    "e.name employee_name, l.name location_name "
                    "FROM customer_sessions s "
                    "LEFT JOIN employees e ON e.id=s.employee_id "
                    "LEFT JOIN locations l ON l.id=s.location_id "
                    "WHERE s.customer_id = ? AND s.status='ended' "
                    "ORDER BY s.started_at DESC LIMIT 5",
                    (cid,),
                ).fetchall()
            ]
            prefs = json.loads(cust["preferences_json"] or "[]")
        return {
            "customer_id": cid, "name": cust["name"], "memory_card": cust["memory_card"],
            "preferences": prefs,
            "session_count": int(sess["n"] or 0),
            "first_seen": sess["first"], "last_seen": sess["last"],
            "conversion_rate": round(float(sess["conv"] or 0), 3),
            "avg_duration_seconds": round(float(sess["avgdur"] or 0), 1),
            "outcome_breakdown": outcome, "top_topics": topics,
            "recent_sessions": recent,
            "_session_ids": [r["session_id"] for r in recent],
        }
    except sqlite3.Error as exc:
        return {"error": f"sqlite_{type(exc).__name__}: {exc}", "_rejected": False}


def _tool_search_customers(
    query: str, limit: int = 10, filters: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Find customers by name OR by topic discussed in their sessions."""
    q = (query or "").strip()
    if not q:
        return {"error": "empty query", "_rejected": True}
    like = f"%{q.lower()}%"
    try:
        with _scoped_connection(filters) as conn:
            rows = conn.execute(
                "SELECT c.id id, c.name name, COUNT(s.id) n "
                "FROM customers c "
                "LEFT JOIN customer_sessions s ON s.customer_id=c.id AND s.status='ended' "
                "WHERE LOWER(c.name) LIKE ? OR EXISTS ("
                "  SELECT 1 FROM customer_sessions s2, json_each(s2.topics_json) "
                "  WHERE s2.customer_id=c.id AND s2.status='ended' AND LOWER(value) LIKE ?) "
                "GROUP BY c.id ORDER BY n DESC LIMIT ?",
                (like, like, min(max(limit, 1), 20)),
            ).fetchall()
        out = [{"customer_id": r["id"], "name": r["name"], "session_count": int(r["n"] or 0)} for r in rows]
        return {"customers": out, "rowcount": len(out), "_session_ids": []}
    except sqlite3.Error as exc:
        return {"error": f"sqlite_{type(exc).__name__}: {exc}", "_rejected": False}


# ── tool: compare + find_outliers ─────────────────────────────────────────


def _compare_entity(conn, entity_type: str, eid: str) -> dict[str, Any]:
    if entity_type == "employee":
        join = "JOIN employees x ON x.id=s.employee_id"
        col = "s.employee_id"
    else:
        join = "JOIN locations x ON x.id=s.location_id"
        col = "s.location_id"
    r = conn.execute(
        f"SELECT x.name name, COUNT(*) n, "
        f"SUM(s.outcome='converted')*1.0/COUNT(*) conv, AVG(s.duration_seconds) avgdur "
        f"FROM customer_sessions s {join} WHERE s.status='ended' AND {col} = ?",
        (eid,),
    ).fetchone()
    topics = [
        {"topic": t["value"], "n": int(t["n"])}
        for t in conn.execute(
            f"SELECT value, COUNT(*) n FROM customer_sessions s, json_each(s.topics_json) "
            f"WHERE s.status='ended' AND {col} = ? GROUP BY value ORDER BY n DESC LIMIT 5",
            (eid,),
        ).fetchall()
    ]
    return {
        "id": eid, "name": r["name"] if r else None,
        "sessions": int(r["n"] or 0) if r else 0,
        "conversion_rate": round(float(r["conv"] or 0), 3) if r else 0.0,
        "avg_duration_seconds": round(float(r["avgdur"] or 0), 1) if r else 0.0,
        "top_topics": topics,
    }


def _tool_compare(
    entity_type: str, a: str, b: str, filters: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Side-by-side stats for two employees OR two locations."""
    et = (entity_type or "").strip().lower()
    if et not in ("employee", "location"):
        return {"error": "entity_type must be 'employee' or 'location'", "_rejected": True}
    if not a or not b:
        return {"error": "provide two ids (a and b)", "_rejected": True}
    try:
        with _scoped_connection(filters) as conn:
            return {
                "entity_type": et,
                "a": _compare_entity(conn, et, a),
                "b": _compare_entity(conn, et, b),
            }
    except sqlite3.Error as exc:
        return {"error": f"sqlite_{type(exc).__name__}: {exc}", "_rejected": False}


def _tool_find_outliers(
    kind: str, filters: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Surface anomalies: unusually long sessions, employees far from mean
    conversion, topics with abnormally long discussions."""
    k = (kind or "duration").strip().lower()
    try:
        with _scoped_connection(filters) as conn:
            if k == "conversion":
                rows = conn.execute(
                    "WITH per AS ("
                    "  SELECT e.name name, COUNT(*) n, "
                    "  SUM(s.outcome='converted')*1.0/COUNT(*) conv "
                    "  FROM customer_sessions s JOIN employees e ON e.id=s.employee_id "
                    "  WHERE s.status='ended' GROUP BY e.id HAVING COUNT(*) >= 3),"
                    " mean AS (SELECT AVG(conv) mu FROM per) "
                    "SELECT per.name name, per.conv conv, mean.mu mu "
                    "FROM per, mean ORDER BY ABS(per.conv - mean.mu) DESC LIMIT 5"
                ).fetchall()
                out = [{"name": r["name"], "conversion_rate": round(float(r["conv"] or 0), 3),
                        "mean": round(float(r["mu"] or 0), 3)} for r in rows]
            elif k == "topic":
                rows = conn.execute(
                    "SELECT value topic, COUNT(*) n, AVG(s.duration_seconds) avgdur "
                    "FROM customer_sessions s, json_each(s.topics_json) "
                    "WHERE s.status='ended' GROUP BY value HAVING COUNT(*) >= 2 "
                    "ORDER BY avgdur DESC LIMIT 5"
                ).fetchall()
                out = [{"topic": r["topic"], "sessions": int(r["n"]),
                        "avg_duration_seconds": round(float(r["avgdur"] or 0), 1)} for r in rows]
            else:  # duration
                rows = conn.execute(
                    "SELECT s.id sid, c.name customer, s.outcome outcome, "
                    "s.duration_seconds dur, substr(s.summary,1,120) summary "
                    "FROM customer_sessions s LEFT JOIN customers c ON c.id=s.customer_id "
                    "WHERE s.status='ended' ORDER BY s.duration_seconds DESC LIMIT 5"
                ).fetchall()
                out = [{"session_id": r["sid"], "customer": r["customer"],
                        "outcome": r["outcome"], "duration_seconds": int(r["dur"] or 0),
                        "summary": r["summary"]} for r in rows]
        return {"kind": k, "outliers": out, "rowcount": len(out), "_session_ids": []}
    except sqlite3.Error as exc:
        return {"error": f"sqlite_{type(exc).__name__}: {exc}", "_rejected": False}


# ── dispatch ───────────────────────────────────────────────────────────────


def _dispatch(name: str, args: dict[str, Any], ui_filters: dict[str, Any] | None = None) -> dict[str, Any]:
    ui = ui_filters or {}

    def pick(key: str) -> str | None:
        # UI filter wins when set; otherwise fall back to what the LLM passed.
        v = ui.get(key)
        return v if v not in (None, "", "all") else args.get(key)

    def scoped(result: dict[str, Any]) -> dict[str, Any]:
        allowed = _scoped_session_ids(ui)
        if allowed == set():
            result["scope_empty"] = True
        return result

    try:
        if name == "run_sql":
            return scoped(_tool_run_sql(str(args.get("sql", "")), filters=ui))
        if name == "vector_search":
            return scoped(_tool_vector_search(
                query=str(args.get("query", "")),
                limit=int(args.get("limit", 5) or 5),
                employee_id=pick("employee_id"),
                customer_id=args.get("customer_id"),
                location_id=pick("location_id"),
                date_from=pick("date_from"),
                date_to=pick("date_to"),
                simulated=ui.get("simulated", "all"),
                topic=pick("topic"),
            ))
        if name == "customer_profile":
            return scoped(_tool_customer_profile(str(args.get("customer_id", "")), filters=ui))
        if name == "search_customers":
            return scoped(_tool_search_customers(
                str(args.get("query", "")), int(args.get("limit", 10) or 10), filters=ui
            ))
        if name == "compare":
            return scoped(_tool_compare(
                str(args.get("entity_type", "")), str(args.get("a", "")),
                str(args.get("b", "")), filters=ui,
            ))
        if name == "find_outliers":
            return scoped(_tool_find_outliers(str(args.get("kind", "duration")), filters=ui))
        return {"error": f"unknown tool: {name}", "_rejected": True}
    except Exception as exc:  # never let a tool crash break the loop
        return {"error": f"dispatch_{type(exc).__name__}: {exc}", "_rejected": False}


# ── agent loop ─────────────────────────────────────────────────────────────


def answer_question(
    question: str,
    *,
    employee_id: str | None = None,
    filters: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the tool-calling loop. Returns answer + trace + sources + short_answer.

    `history` (prior {role,content} turns) is prepended so follow-up questions
    like 'and for last week?' resolve against the prior context.
    """
    started = time.monotonic()
    from session_service import _openai_client

    client = _openai_client()
    if client is None:
        return _finalize(
            "Q&A agent is not configured (missing SHIFTMIND_LLM_API_KEY or OPENAI_API_KEY). "
            "Set one and restart the server.",
            _empty_trace(),
            0,
            started,
            configured=False,
        )

    model = os.getenv("SHIFTMIND_LLM_MODEL", "gpt-4o-mini")
    messages: list[dict[str, Any]] = [{"role": "system", "content": _system_prompt(employee_id, filters)}]
    # Bounded conversation memory: last ~8 prior turns.
    for h in (history or [])[-8:]:
        role = h.get("role")
        content = (h.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    collected = _empty_trace()

    for i in range(MAX_ITERATIONS):
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.2,
        )
        msg = resp.choices[0].message
        messages.append(_message_to_dict(msg))

        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            answer = msg.content or ""
            short = _shorten_answer(question, answer)
            return _finalize(
                answer, collected, i + 1, started,
                configured=True, question=question, short_answer=short,
            )

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw_arguments": tc.function.arguments}
            t0 = time.monotonic()
            result = _dispatch(name, args, ui_filters=filters)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            collected["trace"].append(
                {
                    "tool": name,
                    "args": _summarize_args(name, args),
                    "summary": _summarize_result(result),
                    "rows": result.get("rowcount"),
                    "ms": elapsed_ms,
                }
            )
            for sid in result.get("_session_ids", []):
                collected["session_ids"].add(sid)
            # Cap size before re-injecting into the context window.
            content = json.dumps(result, ensure_ascii=False, default=str)
            if len(content) > TOOL_RESULT_MAX_CHARS:
                content = content[:TOOL_RESULT_MAX_CHARS] + "…(truncated)"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

    fallback = (
        "I couldn't fully answer that within the tool-call budget. Here's what I found so far:\n"
        + _last_assistant_text(messages)
    )
    short = _shorten_answer(question, fallback)
    return _finalize(
        fallback, collected, MAX_ITERATIONS, started,
        configured=True, question=question, short_answer=short,
    )


def answer_question_from_audio(
    audio_bytes: bytes,
    *,
    employee_id: str | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Glasses-side Q&A entry: transcribe a spoken WAV, then answer_question.

    Returns the answer_question result augmented with `question` (the transcribed
    text, so the glasses can echo what it heard) and `transcribe_status`. The
    full `answer` is logged server-side via _log_glasses_qa; the glasses render
    only `short_answer`.
    """
    from session_service import _transcribe_audio

    question, transcribe_status = _transcribe_audio(audio_bytes)
    question = (question or "").strip()
    if not question:
        if transcribe_status == "missing_openai_config":
            return {
                "answer": "Voice transcription is not configured (missing SHIFTMIND_LLM_API_KEY or OPENAI_API_KEY). Set one and restart the server.",
                "short_answer": "transcription not configured",
                "trace": [], "sources": [], "iterations": 0, "elapsed_ms": 0,
                "configured": False, "question": "", "transcribe_status": transcribe_status,
            }
        short = (
            "(no speech heard)"
            if transcribe_status == "transcribed"
            else f"(transcribe failed: {transcribe_status})"
        )
        return {
            "answer": "", "short_answer": short,
            "trace": [], "sources": [], "iterations": 0, "elapsed_ms": 0,
            "configured": True, "question": "", "transcribe_status": transcribe_status,
        }

    result = answer_question(question, employee_id=employee_id, filters=filters)
    result["question"] = question
    result["transcribe_status"] = transcribe_status
    _log_glasses_qa(question, result, employee_id)
    return result


def _log_glasses_qa(
    question: str, result: dict[str, Any], employee_id: str | None
) -> None:
    """Server-side full-answer channel: uvicorn console log + SQLite persist."""
    log_glasses_qa(question, result, employee_id)


def log_glasses_qa(
    question: str, result: dict[str, Any], employee_id: str | None
) -> None:
    """Persist a glasses Q&A turn: uvicorn console log + the qa_turns table.

    Used by both the WAV path (answer_question_from_audio) and the streaming-ASR
    path (/api/qa/ask with an OkHttp User-Agent). Persistence is best-effort —
    a DB error must not break the answer returned to the glasses.
    """
    answer = result.get("answer", "")
    short = result.get("short_answer", "")
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info(
        "[glasses-qa] employee=%s | Q: %s | A: %s | short: %s",
        employee_id, question, answer, short,
    )
    try:
        storage.insert_qa_turn(
            ts=ts,
            employee_id=employee_id,
            question=question,
            answer=answer,
            short_answer=short,
            elapsed_ms=result.get("elapsed_ms"),
            iterations=result.get("iterations"),
        )
    except sqlite3.Error as exc:
        logger.warning("[glasses-qa] failed to persist turn: %s", exc)


def recent_glasses_qa(limit: int = 20) -> list[dict[str, Any]]:
    """Newest-first snapshot of recent glasses Q&A turns (persisted)."""
    rows = storage.recent_qa_turns(limit)
    return [
        {
            "ts": row["ts"],
            "employee_id": row["employee_id"],
            "question": row["question"],
            "answer": row["answer"],
            "short_answer": row["short_answer"],
            "elapsed_ms": row["elapsed_ms"],
            "iterations": row["iterations"],
        }
        for row in rows
    ]


def _empty_trace() -> dict[str, Any]:
    return {"trace": [], "session_ids": set()}


def _message_to_dict(msg: Any) -> dict[str, Any]:
    """Normalize a ChatCompletionMessage into a plain dict, preserving tool_calls.

    Built explicitly (not model_dump) so it works with the official SDK message
    object AND with OpenAI-compatible proxies that reject re-sending the
    pydantic object. tool_calls must be carried so the tool result messages
    that follow are valid per the API contract.
    """
    content = getattr(msg, "content", None) or ""
    tool_calls = getattr(msg, "tool_calls", None) or []
    d: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "",
                },
            }
            for tc in tool_calls
        ]
    return d


def _filter_context(filters: dict[str, Any] | None) -> str:
    """MANDATORY SCOPE block for run_sql (prompt-level; vector_search filters are applied in code)."""
    if not filters:
        return ""
    f = {k: v for k, v in filters.items() if v not in (None, "", "all")}
    if not f:
        return ""
    lines = [
        "",
        "",
        "MANDATORY SCOPE — apply to EVERY run_sql query and every vector_search. Do NOT broaden:",
    ]
    if f.get("location_id"):
        lines.append(f"  location_id == '{f['location_id']}'")
    if f.get("employee_id"):
        lines.append(f"  employee_id == '{f['employee_id']}'")
    if f.get("topic"):
        lines.append(
            f"  topics_json must contain '{f['topic']}' "
            f"(e.g. WHERE EXISTS (SELECT 1 FROM json_each(topics_json) WHERE value LIKE '%{f['topic']}%'))"
        )
    if f.get("date_from"):
        lines.append(f"  started_at >= '{f['date_from']}T00:00:00Z'")
    if f.get("date_to"):
        lines.append(f"  started_at <= '{f['date_to']}T23:59:59Z'")
    if f.get("simulated") == "real":
        lines.append("  is_simulated == 0  (real data only)")
    elif f.get("simulated") == "sim":
        lines.append("  is_simulated == 1  (simulated data only)")
    lines.append("If the result would be empty under this scope, say so — do NOT widen the scope.")
    return "\n".join(lines)


def _system_prompt(employee_id: str | None, filters: dict[str, Any] | None = None) -> str:
    prompt = (
        "You are ShiftMind's retail business-insight assistant. Employees ask you questions "
        "about past customer interactions collected by the AR-glasses system. Answer ONLY from "
        "the tools' returned data — do not invent customers, sessions, numbers, or outcomes.\n\n"
        "Strategy + tool selection:\n"
        "- run_sql: counts, rankings, rates, averages, time trends, filters by employee/location/date/outcome.\n"
        "- vector_search: semantic recall — 'who talked about X', 'find sessions like Y'.\n"
        "- search_customers → customer_profile: 'tell me about a customer who…' / 'what does customer Z usually buy'. Resolve name/topic to an id, then profile.\n"
        "- compare: 'compare X vs Y' (two employees or two locations).\n"
        "- find_outliers: 'what's unusual / any outliers' (duration, conversion, topic).\n"
        "- Hybrid questions: combine tools across turns.\n"
        "- Write focused SQL (limit columns/rows). outcome values: 'converted','not_converted','unknown'.\n"
        "- After tools, give a concise answer in plain language with the key numbers; name people, never bare ids.\n\n"
        + _SCHEMA_HINT
    )
    if employee_id:
        prompt += f"\n\nThe asking employee is {employee_id}. Treat questions like 'my customers' as scoped to this employee_id unless the question says otherwise."
    prompt += _filter_context(filters)
    return prompt


def _summarize_args(name: str, args: dict[str, Any]) -> str:
    if name == "run_sql":
        sql = str(args.get("sql", ""))
        return sql if len(sql) <= 160 else sql[:157] + "…"
    if name == "vector_search":
        q = str(args.get("query", ""))
        extra = [k for k in ("employee_id", "customer_id") if args.get(k)]
        suffix = f" [{','.join(extra)}]" if extra else ""
        return f'"{q}"{suffix}'
    return json.dumps(args, ensure_ascii=False, default=str)[:160]


def _summarize_result(result: dict[str, Any]) -> str:
    if result.get("_rejected"):
        return f"rejected: {result.get('error')}"
    if "error" in result:
        return f"error: {result['error']}"
    if "rows" in result:
        flag = " (truncated)" if result.get("truncated") else ""
        return f"{result.get('rowcount', 0)} rows{flag}"
    if "hits" in result:
        return f"{result.get('rowcount', 0)} hits"
    return "ok"


def _last_assistant_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content"):
            return str(m["content"])
    return ""


def _truncate_plain(text: str, n: int = 140) -> str:
    """Strip markdown + collapse whitespace + word-boundary truncate. For the
    glasses short_answer fallback (matches the liveTranscript 140-char budget)."""
    s = re.sub(r"`+", "", str(text or ""))
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*[-*]\s+", "", s, flags=re.MULTILINE)
    s = s.replace("*", "").replace("#", "")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= n:
        return s
    cut = s.rfind(" ", 0, n)
    return (s[:cut] if cut > 0 else s[:n]).rstrip(",;.:") + "…"


def _shorten_answer(question: str, full_answer: str) -> str:
    """Distill the full answer into a ≤140-char plain-text line for the glasses HUD.

    One LLM call; falls back to _truncate_plain on any failure or missing config.
    """
    full_answer = full_answer or ""
    from session_service import _openai_client

    client = _openai_client()
    if client is None:
        return _truncate_plain(full_answer, 140)
    model = os.getenv("SHIFTMIND_LLM_MODEL", "gpt-4o-mini")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Compress the answer into at most 140 characters of plain text for an AR "
                        "glasses heads-up display. No markdown, no ellipsis padding, straight to "
                        "the point. Include the single most important number or name. Answer the "
                        "user's specific question directly."
                    ),
                },
                {"role": "user", "content": f"Question: {question}\n\nAnswer:\n{full_answer}"},
            ],
            temperature=0.1,
            max_tokens=60,
        )
        line = (resp.choices[0].message.content or "").replace("\n", " ").strip()
        return _truncate_plain(line, 140)
    except Exception:
        return _truncate_plain(full_answer, 140)


def _finalize(
    answer: str,
    collected: dict[str, Any],
    iterations: int,
    started: float,
    *,
    configured: bool,
    question: str = "",
    short_answer: str = "",
) -> dict[str, Any]:
    answer = answer.strip()
    return {
        "answer": answer,
        # glasses-ready line; compute lazily if the caller didn't precompute one
        "short_answer": short_answer or _truncate_plain(answer, 140),
        "trace": collected.get("trace", []),
        "sources": _sources_for(list(collected.get("session_ids", set()))),
        "iterations": iterations,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "configured": configured,
    }


def _sources_for(session_ids: list[str]) -> list[dict[str, Any]]:
    if not session_ids:
        return []
    out: list[dict[str, Any]] = []
    try:
        with storage.connect() as conn:
            placeholders = ",".join(["?"] * len(session_ids))
            rows = conn.execute(
                f"""
                SELECT s.id, s.customer_id, c.name AS customer_name,
                       s.started_at, s.outcome, s.duration_seconds, s.summary
                FROM customer_sessions s
                LEFT JOIN customers c ON c.id = s.customer_id
                WHERE s.id IN ({placeholders})
                """,
                session_ids,
            ).fetchall()
        seen = set()
        for r in rows:
            sid = str(r["id"])
            if sid in seen:
                continue
            seen.add(sid)
            out.append(
                {
                    "session_id": sid,
                    "customer_id": r["customer_id"],
                    "customer_name": r["customer_name"],
                    "started_at": r["started_at"],
                    "outcome": r["outcome"],
                    "duration_seconds": r["duration_seconds"],
                    "summary": (r["summary"] or "")[:160],
                }
            )
    except sqlite3.Error:
        pass
    return out


# ── stats / suggestions / snapshot ─────────────────────────────────────────


def _scoped_and(filters: dict[str, Any] | None, alias: str = "s") -> tuple[str, list]:
    """Build an `AND <alias>.col = ? …` fragment (empty string when no filters).

    Appended after a base `WHERE …` clause. Used by snapshot/suggestions stat
    queries so the Overview + surprise-me buttons respect the active scope.
    """
    f = {k: v for k, v in (filters or {}).items() if v not in (None, "", "all")}
    if not f:
        return "", []
    clauses: list[str] = []
    params: list = []
    a = alias
    if f.get("location_id"):
        clauses.append(f"{a}.location_id = ?")
        params.append(f["location_id"])
    if f.get("employee_id"):
        clauses.append(f"{a}.employee_id = ?")
        params.append(f["employee_id"])
    if f.get("date_from"):
        clauses.append(f"{a}.started_at >= ?")
        params.append(f"{f['date_from']}T00:00:00Z")
    if f.get("date_to"):
        clauses.append(f"{a}.started_at <= ?")
        params.append(f"{f['date_to']}T23:59:59Z")
    sim = f.get("simulated")
    if sim in ("real", "sim"):
        clauses.append(f"{a}.is_simulated = ?")
        params.append(0 if sim == "real" else 1)
    if f.get("topic"):
        clauses.append(f"EXISTS (SELECT 1 FROM json_each({a}.topics_json) WHERE value LIKE ?)")
        params.append(f"%{f['topic']}%")
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _collect_stats(*, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """One SQLite connection, multiple executes. Powers suggestions + snapshot.
    All queries are scoped by `filters` when provided."""
    sc, scp = _scoped_and(filters)
    stats: dict[str, Any] = {}
    try:
        with storage.connect() as conn:
            tot = conn.execute(
                f"SELECT COUNT(*) total, SUM(s.status='ended') ended, SUM(s.status='active') active "
                f"FROM customer_sessions s WHERE 1=1{sc}",
                scp,
            ).fetchone()
            stats["total_sessions"] = int(tot["total"] or 0)
            stats["ended_sessions"] = int(tot["ended"] or 0)
            stats["active_sessions"] = int(tot["active"] or 0)

            cr = conn.execute(
                f"SELECT SUM(s.outcome='converted')*1.0/COUNT(*) conv, AVG(s.duration_seconds) avg_dur "
                f"FROM customer_sessions s WHERE s.status='ended'{sc}",
                scp,
            ).fetchone()
            stats["conversion_rate"] = round(float(cr["conv"] or 0), 4)
            stats["avg_duration_seconds"] = round(float(cr["avg_dur"] or 0), 1)

            stats["outcome_breakdown"] = {
                r["outcome"] or "unknown": int(r["n"])
                for r in conn.execute(
                    f"SELECT s.outcome outcome, COUNT(*) n FROM customer_sessions s "
                    f"WHERE s.status='ended'{sc} GROUP BY s.outcome",
                    scp,
                ).fetchall()
            }
            stats["top_topics"] = [
                {"topic": r["value"], "n": int(r["n"])}
                for r in conn.execute(
                    f"SELECT value, COUNT(*) n FROM customer_sessions s, json_each(s.topics_json) "
                    f"WHERE s.status='ended'{sc} GROUP BY value ORDER BY n DESC LIMIT 8",
                    scp,
                ).fetchall()
            ]
            stats["top_employees"] = [
                {"name": r["name"], "n": int(r["n"]), "conv": round(float(r["conv"] or 0), 3)}
                for r in conn.execute(
                    f"SELECT e.name name, COUNT(*) n, "
                    f"SUM(s.outcome='converted')*1.0/COUNT(*) conv "
                    f"FROM customer_sessions s JOIN employees e ON e.id=s.employee_id "
                    f"WHERE s.status='ended'{sc} GROUP BY e.name ORDER BY n DESC LIMIT 5",
                    scp,
                ).fetchall()
            ]
            loc_rows = conn.execute(
                f"SELECT l.name name, COUNT(*) n, "
                f"SUM(s.outcome='converted')*1.0/COUNT(*) conv "
                f"FROM customer_sessions s JOIN locations l ON l.id=s.location_id "
                f"WHERE s.status='ended'{sc} GROUP BY l.name HAVING COUNT(*)>1 ORDER BY n DESC",
                scp,
            ).fetchall()
            stats["top_locations"] = [
                {"name": r["name"], "n": int(r["n"]), "conv": round(float(r["conv"] or 0), 3)}
                for r in loc_rows
            ]
            if loc_rows:
                stats["busiest_location"] = {"name": loc_rows[0]["name"], "n": int(loc_rows[0]["n"])}
                worst = min(loc_rows, key=lambda r: r["conv"] if r["conv"] is not None else 1.0)
                stats["worst_location"] = {"name": worst["name"], "conv": round(float(worst["conv"] or 0), 3)}
            sim = conn.execute(
                f"SELECT s.is_simulated isim, COUNT(*) n FROM customer_sessions s "
                f"WHERE s.status='ended'{sc} GROUP BY s.is_simulated",
                scp,
            ).fetchall()
            stats["simulated_breakdown"] = {
                "real": int(next((r["n"] for r in sim if r["isim"] == 0), 0)),
                "sim": int(next((r["n"] for r in sim if r["isim"] == 1), 0)),
            }
            dr = conn.execute(
                f"SELECT MIN(s.started_at) min, MAX(s.started_at) max "
                f"FROM customer_sessions s WHERE s.status='ended'{sc}",
                scp,
            ).fetchone()
            stats["date_range"] = {"min": dr["min"], "max": dr["max"]}
    except sqlite3.Error:
        pass
    return stats


def _stats_summary(stats: dict[str, Any]) -> str:
    """Compact text summary of the dataset, fed to the LLM for creative suggestions/narrative."""
    parts = [
        f"{stats.get('ended_sessions', 0)} ended sessions",
        f"conversion rate {stats.get('conversion_rate', 0):.1%}",
        f"avg duration {stats.get('avg_duration_seconds', 0):.0f}s",
    ]
    if stats.get("top_topics"):
        parts.append("top topics: " + ", ".join(f"{t['topic']}({t['n']})" for t in stats["top_topics"][:5]))
    if stats.get("top_employees"):
        parts.append("busiest employees: " + ", ".join(f"{e['name']}({e['n']})" for e in stats["top_employees"][:3]))
    if stats.get("top_locations"):
        parts.append("locations: " + ", ".join(f"{l['name']}({l['n']},conv {l['conv']:.0%})" for l in stats["top_locations"][:3]))
    sb = stats.get("simulated_breakdown", {})
    parts.append(f"real/sim: {sb.get('real',0)}/{sb.get('sim',0)}")
    return "; ".join(parts) + "."


def _template_suggestions(stats: dict[str, Any]) -> list[str]:
    # These remain useful on a brand-new database and are also the deterministic
    # fallback when creative generation is unavailable.
    out = [
        "What is the overall conversion rate?",
        "What are the most common conversation topics?",
        "Which customer conversations lasted the longest?",
    ]
    topics = stats.get("top_topics") or []
    if topics:
        out.append(f"Why is {topics[0]['topic']} the most common topic?")
        if len(topics) > 1:
            out.append(f"Compare {topics[0]['topic']} vs {topics[1]['topic']} conversations")
    emps = stats.get("top_employees") or []
    if emps:
        out.append(f"What does {emps[0]['name']} discuss most?")
        out.append(f"Which employee has the best conversion rate?")
    if stats.get("busiest_location"):
        out.append(f"How does {stats['busiest_location']['name']} compare to other stores?")
    if stats.get("worst_location"):
        out.append(f"Why does {stats['worst_location']['name']} have the lowest conversion?")
    sb = stats.get("simulated_breakdown") or {}
    if sb.get("real", 0) and sb.get("sim", 0):
        out.append("Compare real vs simulated conversion rates")
    if stats.get("date_range", {}).get("min"):
        out.append(f"What changed since {stats['date_range']['min'][:10]}?")
    return out[:8]


def _creative_suggestions(stats: dict[str, Any]) -> list[str]:
    """LLM-generated question suggestions from the stats summary. Falls back to templates."""
    from session_service import _openai_client

    client = _openai_client()
    if client is None:
        return _template_suggestions(stats)
    model = os.getenv("SHIFTMIND_LLM_MODEL", "gpt-4o-mini")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You suggest insightful questions a retail employee might ask about THIS dataset. "
                        "Given the stats, propose 4-6 specific, creative, non-generic questions tied to the "
                        "actual numbers/names. Each ≤80 chars, plain text, no numbering, no quotes, no "
                        "markdown. Return one question per line."
                    ),
                },
                {"role": "user", "content": _stats_summary(stats)},
            ],
            temperature=0.6,
            max_tokens=200,
        )
        lines = [
            ln.strip().lstrip("0123456789.-) ").strip('"').strip()
            for ln in (resp.choices[0].message.content or "").splitlines()
        ]
        lines = [ln for ln in lines if 5 <= len(ln) <= 80]
        return lines[:6] or _template_suggestions(stats)
    except Exception:
        return _template_suggestions(stats)


def get_suggestions(*, creative: bool = False, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    stats = _collect_stats(filters=filters)
    suggestions = _creative_suggestions(stats) if creative else _template_suggestions(stats)
    return {"suggestions": suggestions, "creative": creative, "stats_summary": _stats_summary(stats)}


def _narrative(stats: dict[str, Any]) -> str:
    """1 LLM call → 2-3 plain sentences summarizing the stats. Template fallback."""
    from session_service import _openai_client

    client = _openai_client()
    if client is None:
        return _narrative_fallback(stats)
    model = os.getenv("SHIFTMIND_LLM_MODEL", "gpt-4o-mini")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize these retail session stats in 2-3 plain sentences for a store employee. "
                        "Lead with the most notable number. Plain text, no markdown."
                    ),
                },
                {"role": "user", "content": _stats_summary(stats)},
            ],
            temperature=0.3,
            max_tokens=160,
        )
        return (resp.choices[0].message.content or "").strip() or _narrative_fallback(stats)
    except Exception:
        return _narrative_fallback(stats)


def _narrative_fallback(stats: dict[str, Any]) -> str:
    return (
        f"{stats.get('ended_sessions',0)} sessions recorded "
        f"with a {stats.get('conversion_rate',0):.0%} conversion rate. "
        f"Top topic: {(stats.get('top_topics') or [{}])[0].get('topic','n/a')}."
    )


def get_snapshot(*, narrative: bool = False, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    stats = _collect_stats(filters=filters)
    stats["narrative"] = _narrative(stats) if narrative else None
    return stats


def get_filter_options() -> dict[str, Any]:
    """Dropdown options for the Q&A filter bar. Pure SQL."""
    opts: dict[str, Any] = {"locations": [], "employees": [], "topics": [], "date_range": None}
    try:
        with storage.connect() as conn:
            opts["locations"] = [
                {"id": r["id"], "name": r["name"]}
                for r in conn.execute("SELECT id, name FROM locations ORDER BY name").fetchall()
            ]
            opts["employees"] = [
                {"id": r["id"], "name": r["name"]}
                for r in conn.execute("SELECT id, name FROM employees ORDER BY name").fetchall()
            ]
            opts["topics"] = [
                {"topic": r["value"], "n": int(r["n"])}
                for r in conn.execute(
                    "SELECT value, COUNT(*) n FROM customer_sessions, json_each(topics_json) "
                    "WHERE status='ended' GROUP BY value ORDER BY n DESC LIMIT 15"
                ).fetchall()
            ]
            dr = conn.execute(
                "SELECT MIN(started_at) min, MAX(started_at) max FROM customer_sessions WHERE status='ended'"
            ).fetchone()
            if dr and dr["min"]:
                opts["date_range"] = {"min": str(dr["min"])[:10], "max": str(dr["max"])[:10]}
    except sqlite3.Error:
        pass
    return opts
