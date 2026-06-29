"""Tests for Q&A filters: prompt context, Milvus expr, date conversion, dispatch forwarding."""
from __future__ import annotations

import qa_service
from session_memory_service import _build_filter


def test_filter_context_empty_when_no_filters():
    assert qa_service._filter_context(None) == ""
    assert qa_service._filter_context({}) == ""
    assert qa_service._filter_context({"simulated": "all"}) == ""


def test_filter_context_includes_all_clauses():
    ctx = qa_service._filter_context({
        "location_id": "loc-001",
        "employee_id": "sim-emp-006",
        "topic": "returns",
        "date_from": "2026-06-01",
        "date_to": "2026-06-28",
        "simulated": "real",
    })
    assert "loc-001" in ctx
    assert "sim-emp-006" in ctx
    assert "returns" in ctx
    assert "2026-06-01T00:00:00Z" in ctx
    assert "2026-06-28T23:59:59Z" in ctx
    assert "is_simulated == 0" in ctx
    assert "MANDATORY SCOPE" in ctx


def test_filter_context_sim_only():
    ctx = qa_service._filter_context({"simulated": "sim"})
    assert "is_simulated == 1" in ctx


def test_build_filter_employee_customer():
    assert _build_filter(employee_id="e1", customer_id="c1") == 'employee_id == "e1" and customer_id == "c1"'


def test_build_filter_location_and_dates():
    expr = _build_filter(
        employee_id="e1", customer_id=None, location_id="loc-1",
        created_at_min=1000, created_at_max=2000,
    )
    assert 'employee_id == "e1"' in expr
    assert 'location_id == "loc-1"' in expr
    assert "created_at >= 1000" in expr
    assert "created_at <= 2000" in expr


def test_build_filter_none_returns_none():
    assert _build_filter(employee_id=None, customer_id=None) is None


def test_date_to_epoch_handles_date_and_iso():
    assert qa_service._date_to_epoch(None) is None
    a = qa_service._date_to_epoch("2026-06-01", end=False)
    b = qa_service._date_to_epoch("2026-06-01", end=True)
    assert a is not None and b is not None and b > a
    # full ISO passthrough
    assert qa_service._date_to_epoch("2026-06-01T12:00:00Z") is not None
    assert qa_service._date_to_epoch("garbage") is None


def test_dispatch_merges_ui_filters_into_vector_search(monkeypatch):
    seen = {}

    def fake_vs(**kwargs):
        seen.update(kwargs)
        return {"hits": [], "rowcount": 0, "_session_ids": []}

    monkeypatch.setattr(qa_service, "_tool_vector_search", fake_vs)
    qa_service._dispatch(
        "vector_search",
        {"query": "returns", "limit": 5},
        ui_filters={"location_id": "loc-1", "simulated": "real", "date_from": "2026-06-01"},
    )
    assert seen["location_id"] == "loc-1"
    assert seen["simulated"] == "real"
    assert seen["date_from"] == "2026-06-01"
    assert seen["query"] == "returns"


def test_dispatch_ui_filter_overrides_llm_arg(monkeypatch):
    """UI filter wins when both UI and LLM set the same field."""
    seen = {}

    def fake_vs(**kwargs):
        seen.update(kwargs)
        return {"hits": [], "rowcount": 0, "_session_ids": []}

    monkeypatch.setattr(qa_service, "_tool_vector_search", fake_vs)
    qa_service._dispatch(
        "vector_search",
        {"query": "x", "location_id": "from-llm"},
        ui_filters={"location_id": "from-ui", "simulated": "all"},
    )
    assert seen["location_id"] == "from-ui"  # UI wins
