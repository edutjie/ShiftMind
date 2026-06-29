"""End-to-end row-scope tests for every Q&A tool family."""
from __future__ import annotations

import json

import pytest

import qa_service
from storage import Storage


@pytest.fixture
def scoped_data(tmp_path, monkeypatch):
    db = Storage(tmp_path / "scoped.sqlite3")
    monkeypatch.setattr(qa_service, "storage", db)

    loc_a = db.create_location(name="North")
    loc_b = db.create_location(name="South")
    emp_a = db.create_employee(name="Alice", home_location_id=loc_a["id"])
    emp_b = db.create_employee(name="Bob", home_location_id=loc_b["id"])
    db.upsert_customer(customer_id="cust-a", name="Ada", memory_card="", preferences=[])
    db.upsert_customer(customer_id="cust-b", name="Ben", memory_card="", preferences=[])
    sess_a = db.create_customer_session(
        customer_id="cust-a", is_new_customer=False,
        employee_id=emp_a["id"], location_id=loc_a["id"],
    )
    sess_b = db.create_customer_session(
        customer_id="cust-b", is_new_customer=False,
        employee_id=emp_b["id"], location_id=loc_b["id"],
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE customer_sessions SET status='ended', topics_json=?, outcome='converted', "
            "duration_seconds=30, started_at='2026-06-10T10:00:00Z', ended_at='2026-06-10T10:00:30Z' "
            "WHERE id=?",
            (json.dumps(["returns"]), sess_a["id"]),
        )
        conn.execute(
            "UPDATE customer_sessions SET status='ended', topics_json=?, outcome='not_converted', "
            "duration_seconds=300, started_at='2026-05-10T10:00:00Z', ended_at='2026-05-10T10:05:00Z', "
            "is_simulated=1 WHERE id=?",
            (json.dumps(["warranty"]), sess_b["id"]),
        )
    return {
        "db": db, "loc_a": loc_a, "loc_b": loc_b, "emp_a": emp_a, "emp_b": emp_b,
        "sess_a": sess_a, "sess_b": sess_b,
    }


def test_combined_scope_limits_arbitrary_sql(scoped_data):
    result = qa_service._tool_run_sql(
        "SELECT id FROM customer_sessions",
        filters={
            "employee_id": scoped_data["emp_a"]["id"],
            "location_id": scoped_data["loc_a"]["id"],
            "topic": "return",
            "date_from": "2026-06-01",
            "date_to": "2026-06-30",
            "simulated": "real",
        },
    )
    assert [row["id"] for row in result["rows"]] == [scoped_data["sess_a"]["id"]]


def test_customer_tools_cannot_cross_scope(scoped_data):
    filters = {"employee_id": scoped_data["emp_a"]["id"]}
    found = qa_service._tool_search_customers("a", filters=filters)
    assert [c["customer_id"] for c in found["customers"]] == ["cust-a"]
    hidden = qa_service._tool_customer_profile("cust-b", filters=filters)
    assert hidden.get("_rejected") is True


def test_compare_and_outliers_use_scoped_snapshot(scoped_data):
    filters = {"employee_id": scoped_data["emp_a"]["id"]}
    compared = qa_service._tool_compare(
        "employee", scoped_data["emp_a"]["id"], scoped_data["emp_b"]["id"], filters=filters
    )
    assert compared["a"]["sessions"] == 1
    assert compared["b"]["sessions"] == 0
    outliers = qa_service._tool_find_outliers("duration", filters=filters)
    assert [row["session_id"] for row in outliers["outliers"]] == [scoped_data["sess_a"]["id"]]


def test_vector_hits_are_intersected_with_topic_scope(scoped_data, monkeypatch):
    class FakeMemory:
        def search(self, **_):
            return [
                {"session_id": scoped_data["sess_a"]["id"], "customer_id": "cust-a"},
                {"session_id": scoped_data["sess_b"]["id"], "customer_id": "cust-b"},
            ]

    monkeypatch.setattr("session_memory_service.get_memory_service", lambda: FakeMemory())
    result = qa_service._tool_vector_search("product issue", topic="returns")
    assert [hit["session_id"] for hit in result["hits"]] == [scoped_data["sess_a"]["id"]]


def test_empty_scope_stays_empty(scoped_data):
    result = qa_service._dispatch(
        "run_sql",
        {"sql": "SELECT COUNT(*) n FROM customer_sessions"},
        ui_filters={"employee_id": "missing-employee"},
    )
    assert result["rows"] == [{"n": 0}]
    assert result["scope_empty"] is True
