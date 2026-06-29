"""Tests for Q&A suggestion generation (template + creative fallback)."""
from __future__ import annotations

import qa_service


def test_template_suggestions_nonempty_and_strings():
    suggestions = qa_service._template_suggestions({})
    s = {"suggestions": suggestions, "creative": False, "stats_summary": "0 ended sessions"}
    assert isinstance(s["suggestions"], list)
    assert len(s["suggestions"]) >= 3
    assert all(isinstance(x, str) and x for x in s["suggestions"])
    assert s["creative"] is False
    assert s["stats_summary"]


def test_creative_falls_back_when_no_client(monkeypatch):
    monkeypatch.setattr("session_service._openai_client", lambda: None)
    monkeypatch.setattr(qa_service, "_collect_stats", lambda **_: {})
    s = qa_service.get_suggestions(creative=True)
    assert s["creative"] is True
    assert len(s["suggestions"]) >= 3  # fell back to templates
    assert all(isinstance(x, str) and x for x in s["suggestions"])


def test_creative_uses_llm_when_configured(monkeypatch):
    class _Completions:
        def create(self, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="Which topic drives the most returns?\nHow does Sofia Kim compare to Derek Nguyen?\n"
            ))])

    class _Chat:
        completions = _Completions()

    monkeypatch.setattr("session_service._openai_client",
                        lambda: __import__("types").SimpleNamespace(chat=_Chat()))
    s = qa_service.get_suggestions(creative=True)
    assert s["creative"] is True
    assert "Sofia Kim" in " ".join(s["suggestions"])


def test_template_suggestions_reference_real_data():
    """Template chips should mention at least one real topic/employee/location from the stats."""
    from qa_service import _collect_stats, _template_suggestions

    stats = _collect_stats()
    chips = " ".join(_template_suggestions(stats))
    topics = " ".join(t["topic"] for t in stats.get("top_topics", []))
    # At least one chip should reference a real topic name when topics exist.
    if topics:
        assert any(t["topic"] in chips for t in stats["top_topics"][:3])
