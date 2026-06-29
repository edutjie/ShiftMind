"""Integration test for the session_memories Milvus collection.

Uses a tmp Milvus Lite DB and stubs embed_text (no network). Verifies
upsert → search round-trip and that the employee/customer filter expr
correctly narrows results.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from session_memory_service import EMBED_DIM, SessionMemoryService


@pytest.fixture
def service(tmp_path: Path, monkeypatch) -> SessionMemoryService:
    svc = SessionMemoryService(uri=str(tmp_path / "mem.db"))
    # Deterministic embedding: hash the text into a fixed-dim unit-ish vector.
    import hashlib

    def _fake_embed(text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        vals = [(b - 128) / 128.0 for b in h]
        vec = vals * (EMBED_DIM // len(vals) + 1)
        vec = vec[:EMBED_DIM]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    monkeypatch.setattr(svc, "embed_text", _fake_embed)
    return svc


def test_upsert_then_search_finds_session(service):
    service.upsert_memory(
        memory_id="mem-session-a",
        session_id="session-a",
        customer_id="person-001",
        employee_id="emp-001",
        location_id="loc-001",
        content="Customer asked about returning a faulty football boot.",
        outcome="not_converted",
        started_at_iso="2026-06-27T10:00:00Z",
    )

    hits = service.search(query="return policy for boots", limit=5)
    assert hits, "expected at least one hit"
    assert hits[0]["session_id"] == "session-a"
    assert hits[0]["customer_id"] == "person-001"
    assert hits[0]["outcome"] == "not_converted"


def test_employee_filter_excludes_other_employee(service):
    service.upsert_memory(
        memory_id="mem-session-a",
        session_id="session-a",
        customer_id="person-001",
        employee_id="emp-001",
        location_id=None,
        content="Customer asked about returning a faulty football boot.",
        outcome="not_converted",
        started_at_iso="2026-06-27T10:00:00Z",
    )

    hits = service.search(query="returns", limit=5, employee_id="emp-999")
    assert hits == [], "non-matching employee filter should return nothing"

    hits = service.search(query="returns", limit=5, employee_id="emp-001")
    assert hits and hits[0]["session_id"] == "session-a"


def test_has_session_and_count(service):
    assert service.has_session("session-x") is False
    service.upsert_memory(
        memory_id="mem-session-x",
        session_id="session-x",
        customer_id="person-002",
        employee_id=None,
        location_id=None,
        content="Bought a jersey.",
        outcome="converted",
        started_at_iso="2026-06-27T11:00:00Z",
    )
    assert service.has_session("session-x") is True
    assert service.count() >= 1
