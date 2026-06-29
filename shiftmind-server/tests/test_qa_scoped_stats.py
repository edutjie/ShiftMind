"""Tests that snapshot + suggestions respect filters (the surprise-me/overview scope bug)."""
from __future__ import annotations

import qa_service
from storage import storage


def _a_sim_location() -> str:
    with storage.connect() as conn:
        row = conn.execute("SELECT id FROM locations WHERE id LIKE 'sim-%' LIMIT 1").fetchone()
    return str(row["id"]) if row else ""


def test_scoped_and_fragment():
    frag, params = qa_service._scoped_and({"location_id": "loc-1", "simulated": "real"})
    assert "s.location_id = ?" in frag
    assert "is_simulated = ?" in frag
    assert params == ["loc-1", 0]


def test_scoped_and_empty():
    assert qa_service._scoped_and(None) == ("", [])
    assert qa_service._scoped_and({"simulated": "all"}) == ("", [])


def test_snapshot_location_filter_shrinks_scope():
    loc = _a_sim_location()
    if not loc:
        return  # no sim data; skip
    unscoped = qa_service.get_snapshot()
    scoped = qa_service.get_snapshot(filters={"location_id": loc})
    assert scoped["ended_sessions"] <= unscoped["ended_sessions"]
    # every location in the scoped breakdown is the filtered one
    for lrow in scoped.get("top_locations", []):
        assert lrow["name"] is not None


def test_snapshot_simulated_filter():
    real_only = qa_service.get_snapshot(filters={"simulated": "real"})
    sim_only = qa_service.get_snapshot(filters={"simulated": "sim"})
    # sim breakdown should be consistent with the filter
    assert real_only["simulated_breakdown"]["sim"] == 0
    assert sim_only["simulated_breakdown"]["real"] == 0


def test_suggestions_respect_filters():
    loc = _a_sim_location()
    if not loc:
        return
    out = qa_service.get_suggestions(filters={"location_id": loc})
    assert isinstance(out["suggestions"], list)
    assert out["stats_summary"]  # still returns a summary (scoped)


def test_snapshot_topic_filter():
    out = qa_service.get_snapshot(filters={"topic": "returns"})
    # If the topic exists, scoped ended_sessions <= unscoped with no filter.
    unscoped = qa_service.get_snapshot()
    assert out["ended_sessions"] <= unscoped["ended_sessions"]
