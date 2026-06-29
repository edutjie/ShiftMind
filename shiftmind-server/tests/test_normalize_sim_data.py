"""Tests for the simulated-data normalization (outcome mapping + is_simulated flag)."""
from __future__ import annotations

import sqlite3

import pytest

from scripts.normalize_sim_data import OUTCOME_MAP, _CANONICAL, flag_simulated, normalize_outcomes
from storage import storage


def test_outcome_map_only_targets_canonical_targets():
    """Every mapped value must land in the canonical enum."""
    for target in OUTCOME_MAP.values():
        assert target in _CANONICAL


def test_outcome_map_covers_known_legacy_values():
    """The seed generator's legacy outcomes all have a mapping."""
    for legacy in ("purchased", "price_match", "returned", "complaint", "browsed", "inquiry", "support", "warranty"):
        assert legacy in OUTCOME_MAP


def test_is_simulated_column_exists_after_migration():
    with storage.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(customer_sessions)").fetchall()}
    assert "is_simulated" in cols


def test_flag_and_normalize_on_tmp_db(tmp_path, monkeypatch):
    """flag_simulated + normalize_outcomes against an isolated DB (no real-data side effects)."""
    db = tmp_path / "n.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE customer_sessions (
            id TEXT, status TEXT, outcome TEXT, is_simulated INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE customers (id TEXT, is_simulated INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE employees (id TEXT, is_simulated INTEGER NOT NULL DEFAULT 0);
        """
    )
    conn.executemany(
        "INSERT INTO customer_sessions (id, status, outcome) VALUES (?,?,?)",
        [
            ("sim-sess-1", "ended", "purchased"),
            ("sim-sess-2", "ended", "returned"),
            ("sim-sess-3", "ended", "support"),
            ("real-sess-1", "ended", "converted"),
            ("real-sess-2", "ended", None),
            ("sim-sess-4", "active", "purchased"),  # active -> untouched
        ],
    )
    conn.executemany("INSERT INTO customers (id) VALUES (?)", [("sim-cust-1",), ("person-1",)])
    conn.executemany("INSERT INTO employees (id) VALUES (?)", [("sim-emp-1",), ("emp-1",)])
    conn.commit()
    conn.close()

    # Point storage at the tmp DB so the script's storage.connect() hits it.
    monkeypatch.setattr(storage, "path", db, raising=False)

    flagged = flag_simulated()
    assert flagged["customer_sessions"] == 4  # sim-sess-1..4 (incl. active)
    assert flagged["customers"] == 1
    assert flagged["employees"] == 1

    mapped = normalize_outcomes()
    assert mapped["converted"] == 1   # purchased
    assert mapped["not_converted"] == 1  # returned
    assert mapped["unknown"] == 2     # support + NULL

    conn = sqlite3.connect(db)
    got = {r[0]: r[1] for r in conn.execute("SELECT id, outcome, is_simulated FROM customer_sessions")}
    conn.close()
    # Active session unchanged.
    assert ("sim-sess-4", "purchased") in [(k, v) for k, v in got.items()]
