"""Unit tests for the topics/outcome extraction added to _summarize_session.

Mocks the OpenAI client so no network is needed. Asserts the LLM path returns
the new fields and that every fallback path still includes sane defaults so
downstream code (finish() -> storage + Milvus upsert) can rely on the keys.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from session_service import _summarize_session


def _fake_client(payload_json: str):
    """A stand-in OpenAI client whose chat.completions.create returns payload_json."""

    class _Completions:
        def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload_json))])

    class _Chat:
        completions = _Completions()

    return SimpleNamespace(chat=_Chat())


def test_summarize_extracts_topics_and_outcome(monkeypatch):
    payload = (
        '{"summary":"Customer bought football boots.",'
        '"important_points":["prefers Adidas"],'
        '"name":"Messi",'
        '"topics":["football boots","sale"],'
        '"outcome":"converted"}'
    )
    monkeypatch.setattr("session_service._openai_client", lambda: _fake_client(payload))

    result = _summarize_session(
        customer_id="person-001",
        existing_name="",
        existing_memory="",
        is_new_customer=False,
        transcript="Hi, I need new football boots. I'll take the Adidas ones.",
    )

    assert result["_llm_status"] == "summarized"
    assert result["topics"] == ["football boots", "sale"]
    assert result["outcome"] == "converted"


def test_summarize_coerces_bad_topics_and_outcome(monkeypatch):
    payload = (
        '{"summary":"x","important_points":[],"name":"",'
        '"topics":"not-a-list",'
        '"outcome":"maybe"}'
    )
    monkeypatch.setattr("session_service._openai_client", lambda: _fake_client(payload))

    result = _summarize_session(
        customer_id="person-001",
        existing_name="",
        existing_memory="",
        is_new_customer=False,
        transcript="something",
    )

    assert result["_llm_status"] == "summarized"
    assert result["topics"] == []
    assert result["outcome"] == "unknown"


def test_summarize_lowercases_topics(monkeypatch):
    payload = (
        '{"summary":"x","important_points":[],"name":"",'
        '"topics":["Returns","  WARRANTY  "],'
        '"outcome":"not_converted"}'
    )
    monkeypatch.setattr("session_service._openai_client", lambda: _fake_client(payload))

    result = _summarize_session(
        customer_id="person-001",
        existing_name="",
        existing_memory="",
        is_new_customer=False,
        transcript="something",
    )

    assert result["topics"] == ["returns", "warranty"]
    assert result["outcome"] == "not_converted"


@pytest.mark.parametrize(
    "transcript",
    ["", "   "],
)
def test_summarize_no_transcript_defaults(transcript):
    result = _summarize_session(
        customer_id="person-001",
        existing_name="",
        existing_memory="",
        is_new_customer=False,
        transcript=transcript,
    )
    assert result["_llm_status"] == "no_transcript"
    assert result["topics"] == []
    assert result["outcome"] == "unknown"


def test_summarize_missing_openai_config_defaults(monkeypatch):
    monkeypatch.setattr("session_service._openai_client", lambda: None)

    result = _summarize_session(
        customer_id="person-001",
        existing_name="",
        existing_memory="",
        is_new_customer=False,
        transcript="a real transcript",
    )
    assert result["_llm_status"] == "missing_openai_config"
    assert result["topics"] == []
    assert result["outcome"] == "unknown"


def test_summarize_parse_failure_defaults(monkeypatch):
    # Valid JSON but not a dict -> parse_failed path.
    monkeypatch.setattr("session_service._openai_client", lambda: _fake_client("[1, 2, 3]"))

    result = _summarize_session(
        customer_id="person-001",
        existing_name="",
        existing_memory="",
        is_new_customer=False,
        transcript="a real transcript",
    )
    assert result["_llm_status"] == "summarize_parse_failed"
    assert result["topics"] == []
    assert result["outcome"] == "unknown"


def test_summarize_exception_defaults(monkeypatch):
    class _Boom:
        def create(self, **kwargs):
            raise RuntimeError("boom")

    class _Chat:
        completions = SimpleNamespace(create=_Boom().create)

    monkeypatch.setattr("session_service._openai_client", lambda: SimpleNamespace(chat=_Chat()))

    result = _summarize_session(
        customer_id="person-001",
        existing_name="",
        existing_memory="",
        is_new_customer=False,
        transcript="a real transcript",
    )
    assert result["_llm_status"].startswith("summarize_failed:")
    assert result["topics"] == []
    assert result["outcome"] == "unknown"
