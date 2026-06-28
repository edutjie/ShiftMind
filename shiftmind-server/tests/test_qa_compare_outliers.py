"""Tests for compare + find_outliers agent tools."""
from __future__ import annotations

import qa_service
from storage import storage


def _two_sim_employees() -> tuple[str, str]:
    with storage.connect() as conn:
        rows = conn.execute("SELECT id FROM employees WHERE id LIKE 'sim-%' ORDER BY id LIMIT 2").fetchall()
    return (str(rows[0]["id"]), str(rows[1]["id"])) if len(rows) >= 2 else ("", "")


def _two_sim_locations() -> tuple[str, str]:
    with storage.connect() as conn:
        rows = conn.execute("SELECT id FROM locations WHERE id LIKE 'sim-%' ORDER BY id LIMIT 2").fetchall()
    return (str(rows[0]["id"]), str(rows[1]["id"])) if len(rows) >= 2 else ("", "")


def test_compare_employees():
    a, b = _two_sim_employees()
    if not a:
        return
    out = qa_service._tool_compare("employee", a, b)
    assert out["entity_type"] == "employee"
    for side in ("a", "b"):
        assert out[side]["id"] in (a, b)
        for k in ("name", "sessions", "conversion_rate", "avg_duration_seconds", "top_topics"):
            assert k in out[side]


def test_compare_locations():
    a, b = _two_sim_locations()
    if not a:
        return
    out = qa_service._tool_compare("location", a, b)
    assert out["entity_type"] == "location"
    assert out["a"]["id"] == a and out["b"]["id"] == b


def test_compare_bad_entity_rejected():
    out = qa_service._tool_compare("product", "x", "y")
    assert out.get("_rejected") is True


def test_compare_missing_ids_rejected():
    out = qa_service._tool_compare("employee", "", "")
    assert out.get("_rejected") is True


def test_find_outliers_duration():
    out = qa_service._tool_find_outliers("duration")
    assert out["kind"] == "duration"
    assert isinstance(out["outliers"], list)
    durs = [o["duration_seconds"] for o in out["outliers"]]
    assert durs == sorted(durs, reverse=True)


def test_find_outliers_conversion():
    out = qa_service._tool_find_outliers("conversion")
    assert out["kind"] == "conversion"
    assert "mean" in out["outliers"][0] if out["outliers"] else True


def test_find_outliers_default_kind():
    out = qa_service._tool_find_outliers("")
    assert out["kind"] == "duration"
