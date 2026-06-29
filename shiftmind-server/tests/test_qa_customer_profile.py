"""Tests for customer-profile + search agent tools."""
from __future__ import annotations

import qa_service
from storage import storage


def _a_sim_customer_with_sessions() -> str:
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT s.customer_id FROM customer_sessions s "
            "WHERE s.status='ended' AND s.customer_id LIKE 'sim-%' LIMIT 1"
        ).fetchone()
    return str(row["customer_id"]) if row else ""


def test_search_customers_by_topic():
    out = qa_service._tool_search_customers("returns", limit=5)
    assert out.get("rowcount", 0) >= 0
    assert isinstance(out.get("customers"), list)
    for c in out["customers"]:
        assert "customer_id" in c and "name" in c


def test_search_customers_empty_rejected():
    out = qa_service._tool_search_customers("")
    assert out.get("_rejected") is True


def test_customer_profile_shape():
    cid = _a_sim_customer_with_sessions()
    if not cid:
        return
    p = qa_service._tool_customer_profile(cid)
    assert p["customer_id"] == cid
    for k in ("name", "memory_card", "preferences", "session_count", "first_seen",
              "last_seen", "conversion_rate", "outcome_breakdown", "top_topics",
              "recent_sessions"):
        assert k in p
    assert p["session_count"] >= 1
    assert isinstance(p["recent_sessions"], list)


def test_customer_profile_not_found():
    p = qa_service._tool_customer_profile("does-not-exist-999")
    assert p.get("_rejected") is True
    assert "no customer" in p["error"]


def test_dispatch_routes_customer_tools():
    cid = _a_sim_customer_with_sessions()
    if not cid:
        return
    r1 = qa_service._dispatch("customer_profile", {"customer_id": cid})
    assert "customer_id" in r1
    r2 = qa_service._dispatch("search_customers", {"query": "returns"})
    assert "customers" in r2
