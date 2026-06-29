from session_service import _extract_explicit_name, _name_after_interaction, _update_memory_card_with_llm


def test_later_interaction_fills_unknown_customer_name():
    assert _name_after_interaction(
        customer_id="person-001",
        stored_name="Unknown customer",
        extracted_name="Maya",
    ) == "Maya"


def test_customer_id_placeholder_is_treated_as_missing_name():
    assert _name_after_interaction(
        customer_id="person-001",
        stored_name="person-001",
        extracted_name="Maya",
    ) == "Maya"


def test_existing_customer_name_is_not_overwritten():
    assert _name_after_interaction(
        customer_id="person-001",
        stored_name="Maya",
        extracted_name="Maria",
    ) == "Maya"


def test_explicit_name_fallback_reads_transcript():
    assert _extract_explicit_name("Hello, my name is Maya, and I need running shoes.") == "Maya"


def test_memory_card_excludes_customer_name(monkeypatch):
    monkeypatch.setattr("session_service._openai_client", lambda: None)
    card = _update_memory_card_with_llm(
        customer_id="person-001",
        customer_name="Maya",
        existing_memory="Maya likes blue shoes",
        new_summary="Maya asked about running shoes",
        new_points=["name: Maya", "prefers lightweight shoes"],
    )
    assert "maya" not in card.casefold()
    assert "name:" not in card.casefold()
