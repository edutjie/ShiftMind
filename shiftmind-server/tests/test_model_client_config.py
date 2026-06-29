"""Environment precedence for separate LLM and embedding providers."""
from __future__ import annotations

from session_memory_service import _embedding_client
from session_service import _openai_client


def _clear_provider_env(monkeypatch) -> None:
    for name in (
        "SHIFTMIND_LLM_API_KEY", "SHIFTMIND_LLM_BASE_URL",
        "SHIFTMIND_EMBED_API_KEY", "SHIFTMIND_EMBED_BASE_URL",
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_llm_client_prefers_llm_specific_config(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("SHIFTMIND_LLM_API_KEY", "llm-key")
    monkeypatch.setenv("SHIFTMIND_LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "shared-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://shared.example/v1")

    client = _openai_client()

    assert client is not None
    assert client.api_key == "llm-key"
    assert str(client.base_url) == "https://llm.example/v1/"


def test_embedding_client_prefers_embedding_specific_config(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("SHIFTMIND_EMBED_API_KEY", "embed-key")
    monkeypatch.setenv("SHIFTMIND_EMBED_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "shared-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://shared.example/v1")

    client = _embedding_client()

    assert client is not None
    assert client.api_key == "embed-key"
    assert str(client.base_url) == "https://embed.example/v1/"


def test_embedding_client_falls_back_to_shared_config(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "shared-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://shared.example/v1")

    client = _embedding_client()

    assert client is not None
    assert client.api_key == "shared-key"
    assert str(client.base_url) == "https://shared.example/v1/"
