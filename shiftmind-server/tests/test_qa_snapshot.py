"""Tests for the Q&A snapshot dashboard (pure-SQL stats)."""
from __future__ import annotations

import sqlite3

import qa_service
from storage import storage


def test_snapshot_has_expected_shape():
    snap = qa_service.get_snapshot(narrative=False)
    for k in (
        "total_sessions", "ended_sessions", "conversion_rate", "avg_duration_seconds",
        "outcome_breakdown", "top_topics", "top_employees", "top_locations",
        "simulated_breakdown", "date_range", "narrative",
    ):
        assert k in snap, f"missing {k}"
    assert snap["narrative"] is None  # narrative opt-in only


def test_snapshot_totals_match_direct_query():
    snap = qa_service.get_snapshot(narrative=False)
    with storage.connect() as conn:
        direct = conn.execute("SELECT COUNT(*) n FROM customer_sessions WHERE status='ended'").fetchone()["n"]
        conv = conn.execute(
            "SELECT SUM(outcome='converted')*1.0/COUNT(*) r FROM customer_sessions WHERE status='ended'"
        ).fetchone()["r"]
    assert snap["ended_sessions"] == int(direct)
    assert abs(snap["conversion_rate"] - round(float(conv or 0), 4)) < 1e-6


def test_snapshot_top_topics_match_json_each():
    snap = qa_service.get_snapshot(narrative=False)
    with storage.connect() as conn:
        top = conn.execute(
            "SELECT value, COUNT(*) n FROM customer_sessions, json_each(topics_json) "
            "WHERE status='ended' GROUP BY value ORDER BY n DESC LIMIT 1"
        ).fetchone()
    if snap["top_topics"] and top:
        assert snap["top_topics"][0]["topic"] == top["value"]
        assert snap["top_topics"][0]["n"] == int(top["n"])


def test_narrative_opt_in_returns_text(monkeypatch):
    # Force the LLM off so we get the fallback narrative (no network).
    monkeypatch.setattr("session_service._openai_client", lambda: None)
    snap = qa_service.get_snapshot(narrative=True)
    assert isinstance(snap["narrative"], str) and snap["narrative"]
