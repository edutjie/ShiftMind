"""Agentic Q&A over collected conversation data.

An OpenAI-compatible tool-calling loop. Given a natural-language question
from an employee, the LLM picks between two tools to answer:

- run_sql:        read-only SELECT against the SQLite customer/session DB.
- vector_search:  semantic search over the Milvus session_memories collection.

The agent decides which tool(s) to call per question (often both). The loop
returns the final answer plus a tool-call trace and source session_ids for
the UI to render.

Reuses session_service._openai_client (same OPENAI_API_KEY / base URL the
summarizer uses) and session_memory_service.get_memory_service for vectors.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Any

from storage import storage

MAX_ITERATIONS = 6
TOOL_RESULT_MAX_CHARS = 4000
SQL_ROW_CAP = 50

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
    "conversation length in seconds. employee_id/location_id are nullable."
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
                },
                "required": ["query"],
            },
        },
    },
]


# ── tool: run_sql ──────────────────────────────────────────────────────────


def _tool_run_sql(sql: str) -> dict[str, Any]:
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

    uri = f"file:{storage.path.as_posix()}?mode=ro&uri=true"
    try:
        ro = sqlite3.connect(uri, uri=True)
        ro.row_factory = sqlite3.Row
        try:
            rows = ro.execute(body).fetchmany(SQL_ROW_CAP + 1)
        finally:
            ro.close()
    except sqlite3.Error as exc:
        return {"error": f"sqlite_{type(exc).__name__}: {exc}", "_rejected": False}

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


def _tool_vector_search(
    query: str,
    limit: int = 5,
    employee_id: str | None = None,
    customer_id: str | None = None,
) -> dict[str, Any]:
    """Semantic search over session_memories. Always returns a dict, never raises."""
    if not (query or "").strip():
        return {"error": "empty query", "_rejected": True}
    try:
        from session_memory_service import get_memory_service

        hits = get_memory_service().search(
            query=query,
            limit=limit or 5,
            employee_id=employee_id,
            customer_id=customer_id,
        )
    except Exception as exc:
        return {"error": f"vector_search_failed: {type(exc).__name__}: {exc}", "_rejected": False}

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


# ── dispatch ───────────────────────────────────────────────────────────────


def _dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "run_sql":
            return _tool_run_sql(str(args.get("sql", "")))
        if name == "vector_search":
            return _tool_vector_search(
                query=str(args.get("query", "")),
                limit=int(args.get("limit", 5) or 5),
                employee_id=args.get("employee_id"),
                customer_id=args.get("customer_id"),
            )
        return {"error": f"unknown tool: {name}", "_rejected": True}
    except Exception as exc:  # never let a tool crash break the loop
        return {"error": f"dispatch_{type(exc).__name__}: {exc}", "_rejected": False}


# ── agent loop ─────────────────────────────────────────────────────────────


def answer_question(question: str, *, employee_id: str | None = None) -> dict[str, Any]:
    """Run the tool-calling loop. Returns answer + trace + sources."""
    started = time.monotonic()
    from session_service import _openai_client

    client = _openai_client()
    if client is None:
        return _finalize(
            "Q&A agent is not configured (missing OPENAI_API_KEY). Set it and restart the server.",
            _empty_trace(),
            0,
            started,
            configured=False,
        )

    model = os.getenv("SHIFTMIND_LLM_MODEL", "gpt-4o-mini")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(employee_id)},
        {"role": "user", "content": question},
    ]
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
            return _finalize(msg.content or "", collected, i + 1, started, configured=True)

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw_arguments": tc.function.arguments}
            t0 = time.monotonic()
            result = _dispatch(name, args)
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

    return _finalize(
        "I couldn't fully answer that within the tool-call budget. Here's what I found so far:\n"
        + _last_assistant_text(messages),
        collected,
        MAX_ITERATIONS,
        started,
        configured=True,
    )


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


def _system_prompt(employee_id: str | None) -> str:
    prompt = (
        "You are ShiftMind's retail business-insight assistant. Employees ask you questions "
        "about past customer interactions collected by the AR-glasses system. Answer ONLY from "
        "the tools' returned data — do not invent customers, sessions, numbers, or outcomes.\n\n"
        "Strategy:\n"
        "- Counts, rankings, rates, averages, filters by employee/location/date/outcome → run_sql.\n"
        "- 'who talked about X', 'what do clients usually ask', semantic recall → vector_search.\n"
        "- Hybrid questions: call both.\n"
        "- Write focused SQL (limit columns/rows). outcome values: 'converted','not_converted','unknown'.\n"
        "- After tools, give a concise answer in plain language with the key numbers.\n\n"
        + _SCHEMA_HINT
    )
    if employee_id:
        prompt += f"\n\nThe asking employee is {employee_id}. Treat questions like 'my customers' as scoped to this employee_id unless the question says otherwise."
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


def _finalize(
    answer: str,
    collected: dict[str, Any],
    iterations: int,
    started: float,
    *,
    configured: bool,
) -> dict[str, Any]:
    return {
        "answer": answer.strip(),
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
