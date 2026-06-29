"""Unit tests for the qa_service tool-calling loop.

Mocks the OpenAI client (via session_service._openai_client) and stubs the
two tool executors so no DB/Milvus/network is touched. Verifies the loop
dispatches tools in the scripted order, terminates on a stop message, and
falls back gracefully when the model never stops.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import qa_service
from qa_service import answer_question


def _tc(tc_id: str, name: str, arguments: str):
    return SimpleNamespace(
        id=tc_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


class _ScriptedCompletions:
    """Returns messages from a script, recording calls."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if not self.script:
            # Keep returning a stop with the last content to avoid infinite loops.
            return SimpleNamespace(choices=[SimpleNamespace(message=_msg("(no more script)"))])
        msg = self.script.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _install_client(monkeypatch, script) -> _ScriptedCompletions:
    comps = _ScriptedCompletions(script)

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=comps))
    monkeypatch.setattr("session_service._openai_client", lambda: fake_client)
    # _shorten_answer would otherwise make an extra (unscripted) LLM call at finalize.
    monkeypatch.setattr(qa_service, "_shorten_answer", lambda q, a: "SHORT")
    return comps


def test_loop_dispatches_both_tools_then_stops(monkeypatch):
    seen = []

    def fake_vector(query, limit=5, employee_id=None, customer_id=None, **_):
        seen.append(("vector_search", query))
        return {"hits": [{"session_id": "session-a", "customer_id": "person-001"}], "rowcount": 1, "_session_ids": ["session-a"]}

    def fake_sql(sql, **_):
        seen.append(("run_sql", sql))
        return {"rows": [{"n": 5}], "rowcount": 1, "truncated": False, "_session_ids": []}

    monkeypatch.setattr(qa_service, "_tool_vector_search", fake_vector)
    monkeypatch.setattr(qa_service, "_tool_run_sql", fake_sql)
    # Source enrichment hits real storage; stub it for this loop-mechanics test.
    monkeypatch.setattr(qa_service, "_sources_for", lambda ids: [{"session_id": i} for i in ids])

    script = [
        _msg(tool_calls=[_tc("c1", "vector_search", '{"query":"returns"}')]),
        _msg(tool_calls=[_tc("c2", "run_sql", '{"sql":"SELECT COUNT(*) n FROM customer_sessions"}')]),
        _msg(content="Five sessions mention returns."),
    ]
    comps = _install_client(monkeypatch, script)

    result = answer_question("Who talked about returns?", employee_id="emp-001")

    assert result["configured"] is True
    assert result["iterations"] == 3
    assert result["answer"] == "Five sessions mention returns."
    assert result["short_answer"] == "SHORT"
    assert len(result["short_answer"]) <= 140
    assert [s[0] for s in seen] == ["vector_search", "run_sql"]
    assert "session-a" in [s["session_id"] for s in result["sources"]]
    assert len(result["trace"]) == 2
    assert comps.calls == 3


def test_loop_terminates_immediately_on_stop(monkeypatch):
    script = [_msg(content="I don't know yet.")]
    _install_client(monkeypatch, script)
    result = answer_question("anything")
    assert result["iterations"] == 1
    assert result["answer"] == "I don't know yet."
    assert result["trace"] == []


def test_loop_hits_max_iterations(monkeypatch):
    monkeypatch.setattr(qa_service, "_tool_run_sql", lambda sql, **_: {"rows": [], "rowcount": 0, "_session_ids": []})
    # Model never stops — always emits another tool call.
    script = [_msg(tool_calls=[_tc(f"c{i}", "run_sql", '{"sql":"SELECT 1"}')]) for i in range(qa_service.MAX_ITERATIONS + 3)]
    _install_client(monkeypatch, script)

    result = answer_question("loop forever")
    assert result["iterations"] == qa_service.MAX_ITERATIONS
    assert "couldn't fully answer" in result["answer"].lower()
    assert len(result["trace"]) == qa_service.MAX_ITERATIONS


def test_loop_bad_tool_args_do_not_crash(monkeypatch):
    # Arguments aren't valid JSON for the args we need -> dispatch handles gracefully.
    monkeypatch.setattr(qa_service, "_tool_run_sql", lambda sql, **_: {"error": f"bad sql: {sql}", "_rejected": False, "_session_ids": []})
    script = [
        _msg(tool_calls=[_tc("c1", "run_sql", '{"sql":"SELECT broken"}')]),
        _msg(content="Recovered after an error."),
    ]
    _install_client(monkeypatch, script)
    result = answer_question("q")
    assert result["iterations"] == 2
    assert result["answer"] == "Recovered after an error."


def test_not_configured(monkeypatch):
    monkeypatch.setattr("session_service._openai_client", lambda: None)
    result = answer_question("q")
    assert result["configured"] is False
    assert "not configured" in result["answer"].lower()


def test_truncate_plain_strips_markdown_and_caps():
    from qa_service import _truncate_plain

    md = "## Title\n\n- **bold** point one with many words here\n- `code` and *italic*"
    out = _truncate_plain(md, 30)
    assert "*" not in out and "#" not in out and "`" not in out
    assert len(out) <= 30
    assert out.endswith("…")


def test_shorten_answer_falls_back_when_no_client(monkeypatch):
    monkeypatch.setattr("session_service._openai_client", lambda: None)
    from qa_service import _shorten_answer

    out = _shorten_answer("q", "A long answer with **markdown** that must shrink to fit the HUD budget.")
    assert len(out) <= 140
    assert "*" not in out


def test_history_is_prepended_to_messages(monkeypatch):
    captured = []

    class _Completions:
        def create(self, **kwargs):
            captured.append([{"role": m["role"], "content": m.get("content")} for m in kwargs["messages"]])
            from types import SimpleNamespace
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="done"))])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setattr("session_service._openai_client", lambda: fake)
    monkeypatch.setattr(qa_service, "_shorten_answer", lambda q, a: "s")
    monkeypatch.setattr(qa_service, "_sources_for", lambda ids: [])

    answer_question(
        "follow up",
        history=[{"role": "user", "content": "who is best?"},
                 {"role": "assistant", "content": "Sofia Kim"}],
    )
    roles = [m["role"] for m in captured[0]]
    assert roles == ["system", "user", "assistant", "user"]
    assert captured[0][-1]["content"] == "follow up"
    assert captured[0][1]["content"] == "who is best?"
