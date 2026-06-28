"""Tests for multi-employee / multi-location + shared-memory behavior.

Covers: PIN hashing, last-employee lookup (drives the summary prompt), and
session stamping from the active login. Uses fixed test ids + idempotent
upserts so re-runs don't accumulate rows in the dev DB.
"""
from __future__ import annotations

import sqlite3

import pytest

from auth import active_login, hash_pin, verify_pin
from face_recognition_service import RecognitionResult
from session_service import session_service
from storage import DATA_DIR, storage

_DB = str(DATA_DIR / "shiftmind.sqlite3")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _upsert_location(lid: str, name: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO locations (id, name, created_at) VALUES (?, ?, '2026-01-01T00:00:00Z') "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (lid, name),
        )


def _upsert_employee(eid: str, name: str, home: str | None = None, pin_hash: str | None = None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO employees (id, name, pin_hash, home_location_id, created_at) "
            "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z') "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, pin_hash=excluded.pin_hash, "
            "home_location_id=excluded.home_location_id",
            (eid, name, pin_hash, home),
        )


def _upsert_customer(cid: str, name: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO customers (id, name, memory_card, preferences_json, created_at, updated_at) "
            "VALUES (?, ?, '', '[]', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z') "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (cid, name),
        )


def _insert_ended_session(sid: str, customer_id: str, employee_id: str, location_id: str | None,
                          ended_at: str, summary: str = "s") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO customer_sessions "
            "(id, customer_id, status, is_new_customer, summary, started_at, ended_at, employee_id, location_id) "
            "VALUES (?, ?, 'ended', 0, ?, ?, ?, ?, ?)",
            (sid, customer_id, summary, ended_at, ended_at, employee_id, location_id),
        )


def _delete_test_sessions(prefix: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM customer_sessions WHERE id LIKE ?", (f"{prefix}%",))


# ── PIN hashing (pure) ────────────────────────────────────────────────────

def test_pin_hash_and_verify_roundtrip():
    h = hash_pin("1234")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_pin("1234", h) is True
    assert verify_pin("0000", h) is False


def test_pin_none_means_name_select_only():
    # No stored pin → any (or blank) pin accepted.
    assert verify_pin("", None) is True
    assert verify_pin("anything", None) is True
    # But a stored pin rejects a blank entry.
    assert verify_pin("", hash_pin("9999")) is False


# ── Last-employee lookup (drives the summary prompt) ─────────────────────

def test_get_last_interaction_returns_prior_employee_and_excludes_current():
    _upsert_location("test-loc-a", "Site A")
    _upsert_location("test-loc-b", "Site B")
    _upsert_employee("test-emp-a", "Maya", home="test-loc-a")
    _upsert_employee("test-emp-b", "Joan", home="test-loc-b")
    cid = "person-test-li"
    _upsert_customer(cid, "LI Person")
    try:
        _delete_test_sessions("li-")
        _insert_ended_session("li-old", cid, "test-emp-a", "test-loc-a", "2026-01-01T00:00:00Z", "old visit")
        _insert_ended_session("li-new", cid, "test-emp-b", "test-loc-b", "2026-06-01T00:00:00Z", "new visit")

        # Most recent excluding the current (li-new) → the Maya session.
        last = storage.get_last_interaction_by_customer(cid, exclude_session_id="li-new")
        assert last is not None
        assert last["employee_name"] == "Maya"
        assert last["location_name"] == "Site A"

        # Without exclusion → the Joan session (most recent).
        top = storage.get_last_interaction_by_customer(cid)
        assert top["employee_name"] == "Joan"
    finally:
        _delete_test_sessions("li-")


def test_get_last_interaction_none_when_no_attributed_session():
    cid = "person-test-none"
    _upsert_customer(cid, "None Person")
    try:
        _delete_test_sessions("ln-")
        # Session with no employee must NOT be returned (NULL, not "" — FK).
        _insert_ended_session("ln-1", cid, None, None, "2026-03-01T00:00:00Z")
        assert storage.get_last_interaction_by_customer(cid) is None
    finally:
        _delete_test_sessions("ln-")


# ── Session stamping from the active login ────────────────────────────────

@pytest.fixture
def clean_active_login():
    active_login.clear()
    session_service.reset()
    yield
    active_login.clear()
    session_service.reset()


def test_session_stamped_with_active_employee_and_location(clean_active_login):
    _upsert_location("test-loc-a", "Site A")
    _upsert_employee("test-emp-a", "Maya", home="test-loc-a")
    cid = "person-test-stamp"
    storage.upsert_customer(customer_id=cid, name="Stamp Person", memory_card="")
    # Abort any lingering active session for this customer from prior runs.
    with _conn() as c:
        c.execute(f"UPDATE customer_sessions SET status='aborted' WHERE customer_id='{cid}' AND status='active'")

    active_login.set(employee_id="test-emp-a", employee_name="Maya",
                     location_id="test-loc-a", location_name="Site A")
    res = session_service.ensure_from_recognition(
        RecognitionResult(status="confirmed", person_id=cid, name="Stamp Person"), None
    )
    assert res is not None
    row = storage.latest_active_session()
    assert row["employee_id"] == "test-emp-a"
    assert row["location_id"] == "test-loc-a"
    # Tidy up so the active session doesn't leak into other runs.
    with _conn() as c:
        c.execute(f"UPDATE customer_sessions SET status='aborted' WHERE id='{res.session_id}'")


def test_session_has_null_employee_when_no_login(clean_active_login):
    cid = "person-test-nologin"
    storage.upsert_customer(customer_id=cid, name="NoLogin Person", memory_card="")
    with _conn() as c:
        c.execute(f"UPDATE customer_sessions SET status='aborted' WHERE customer_id='{cid}' AND status='active'")

    # No active login → session still opens, just unattributed.
    res = session_service.ensure_from_recognition(
        RecognitionResult(status="confirmed", person_id=cid, name="NoLogin Person"), None
    )
    assert res is not None
    row = storage.latest_active_session()
    assert row["employee_id"] is None
    assert row["location_id"] is None
    with _conn() as c:
        c.execute(f"UPDATE customer_sessions SET status='aborted' WHERE id='{res.session_id}'")
