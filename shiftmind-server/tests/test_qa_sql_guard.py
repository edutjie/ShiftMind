"""Unit tests for the read-only SQL guard in qa_service._tool_run_sql.

Points a tmp SQLite file (via monkeypatch on storage.path) so the guard is
exercised against a real driver, not mocked. Verifies: SELECT/WITH work,
every write/DDL/PRAGMA is rejected, multi-statement is rejected, and the
50-row cap flags truncation.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import qa_service
from qa_service import _tool_run_sql


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "qa.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE customer_sessions (
            id TEXT, customer_id TEXT, employee_id TEXT, location_id TEXT,
            topics_json TEXT DEFAULT '[]', started_at TEXT, is_simulated INTEGER DEFAULT 0,
            outcome TEXT, duration_seconds REAL
        );
        CREATE TABLE customers (id TEXT, name TEXT);
        CREATE TABLE employees (id TEXT, name TEXT);
        CREATE TABLE locations (id TEXT, name TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO customer_sessions (id, customer_id, outcome, duration_seconds) VALUES (?,?,?,?)",
        [(f"session-{i:03d}", f"person-{i:03d}", "converted" if i % 2 else "not_converted", float(i * 60)) for i in range(60)],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(qa_service.storage, "path", db, raising=False)
    return db


def test_select_returns_rows(tmp_db):
    res = _tool_run_sql("SELECT COUNT(*) AS n FROM customer_sessions")
    assert res["rowcount"] == 1
    assert res["rows"][0]["n"] == 60
    assert res["truncated"] is False


def test_with_select_works(tmp_db):
    res = _tool_run_sql("WITH x AS (SELECT * FROM customer_sessions) SELECT COUNT(*) AS n FROM x")
    assert res["rows"][0]["n"] == 60


def test_collects_session_ids(tmp_db):
    res = _tool_run_sql("SELECT id FROM customer_sessions WHERE outcome='converted' LIMIT 3")
    assert res["rowcount"] == 3
    assert len(res["_session_ids"]) == 3
    assert all(s.startswith("session-") for s in res["_session_ids"])


def test_row_cap_truncation_flag(tmp_db):
    res = _tool_run_sql("SELECT id FROM customer_sessions")
    assert res["rowcount"] == 50  # capped
    assert res["truncated"] is True


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO customer_sessions VALUES ('x','y','converted',1)",
        "UPDATE customer_sessions SET outcome='converted'",
        "DELETE FROM customer_sessions",
        "DROP TABLE customers",
        "CREATE TABLE evil (x TEXT)",
        "ALTER TABLE customers ADD COLUMN x TEXT",
        "PRAGMA database_list",
        "ATTACH 'file:/tmp/x' AS evil",
        "VACUUM",
    ],
)
def test_writes_and_ddl_rejected(tmp_db, sql):
    res = _tool_run_sql(sql)
    assert res.get("_rejected") is True, f"expected rejection for: {sql}"
    assert "error" in res


def test_multistatement_rejected(tmp_db):
    res = _tool_run_sql("SELECT 1; DROP TABLE customers;")
    assert res.get("_rejected") is True


def test_bad_sql_returns_error_not_raise(tmp_db):
    res = _tool_run_sql("SELECT nonexistent_column FROM customer_sessions")
    assert "error" in res
    # Execution error (not a guard rejection) but still safe.
    assert res.get("_rejected") is False


def test_unsupported_table_is_rejected(tmp_db):
    res = _tool_run_sql("SELECT * FROM sqlite_master")
    assert res.get("_rejected") is True


def test_filters_limit_free_form_sql(tmp_db):
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("UPDATE customer_sessions SET employee_id='emp-a', is_simulated=0 WHERE id='session-001'")
        conn.execute("UPDATE customer_sessions SET employee_id='emp-b', is_simulated=1 WHERE id='session-002'")
    res = _tool_run_sql(
        "SELECT id FROM customer_sessions ORDER BY id",
        filters={"employee_id": "emp-a", "simulated": "real"},
    )
    assert [row["id"] for row in res["rows"]] == ["session-001"]
