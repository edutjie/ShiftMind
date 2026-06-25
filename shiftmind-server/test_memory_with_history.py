"""Test memory card with historical session context."""
import json
from session_service import _update_memory_card_with_llm


def test_with_history():
    """Test memory card updates with full session history context."""
    print("="*60)
    print("TEST: Memory Card with Historical Context")
    print("="*60)

    # Simulate session history from database
    session_history = [
        {
            "summary": "Customer introduced himself as Messi and shared personal preferences. Likes football, blue color, from Argentina, left-footed.",
            "important_points_json": json.dumps([
                "Likes to play football",
                "Favorite color is blue",
                "From Argentina",
                "Plays with his left foot"
            ]),
            "started_at": "2026-06-25T10:00:00Z"
        },
        {
            "summary": "Customer returned and needs new football boots. Prefers Adidas brand.",
            "important_points_json": json.dumps([
                "Prefers Adidas football boots"
            ]),
            "started_at": "2026-06-25T11:30:00Z"
        },
    ]

    # Current memory card after 2 sessions
    existing_memory = "- Messi; from Argentina; left-footed; likes football\n- Prefers blue; wants Adidas football boots"

    print("\n--- Session 3: Asks About Jersey Sizes ---")
    print(f"\nExisting Memory Card:\n{existing_memory}\n")
    print(f"Session History Available: {len(session_history)} past sessions")
    for i, sess in enumerate(session_history, 1):
        summary = sess['summary'][:80] + "..." if len(sess['summary']) > 80 else sess['summary']
        points = json.loads(sess['important_points_json'])
        print(f"  Session {i}: {summary}")
        print(f"           Points: {points[:2]}")

    # New session
    new_summary = "Customer asked about jersey sizes and mentioned he wears size M."
    new_points = ["Jersey size: M"]

    print(f"\n📝 New Session:")
    print(f"   Summary: {new_summary}")
    print(f"   Points: {new_points}")

    # Update with history
    memory_card = _update_memory_card_with_llm(
        customer_id="person-001",
        existing_memory=existing_memory,
        new_summary=new_summary,
        new_points=new_points,
        session_history=session_history,
    )

    print(f"\n✅ Updated Memory Card ({len(memory_card)} chars):")
    print(f"   {memory_card}")

    # Test Session 4: Customer mentions a new preference
    print("\n\n--- Session 4: Mentions Favorite Team ---")

    session_history.append({
        "summary": new_summary,
        "important_points_json": json.dumps(new_points),
        "started_at": "2026-06-25T14:00:00Z"
    })

    new_summary4 = "Customer mentioned he supports FC Barcelona."
    new_points4 = ["Supports FC Barcelona"]

    print(f"\n📝 New Session:")
    print(f"   Summary: {new_summary4}")
    print(f"   Points: {new_points4}")
    print(f"\nPrevious Memory: {memory_card}")

    memory_card4 = _update_memory_card_with_llm(
        customer_id="person-001",
        existing_memory=memory_card,
        new_summary=new_summary4,
        new_points=new_points4,
        session_history=session_history,
    )

    print(f"\n✅ Updated Memory Card ({len(memory_card4)} chars):")
    print(f"   {memory_card4}")

    # Validation
    print("\n" + "="*60)
    print("VALIDATION")
    print("="*60)

    print(f"\nFinal memory length: {len(memory_card4)} chars (target: <150)")

    # Check if key facts are preserved
    key_facts = ["Messi", "Argentina", "football", "Adidas", "Barcelona"]
    preserved = [fact for fact in key_facts if fact.lower() in memory_card4.lower()]

    print(f"Key facts preserved: {preserved}")
    print(f"Coverage: {len(preserved)}/{len(key_facts)} facts")

    if len(memory_card4) <= 150:
        print("\n✅ PASSED: Memory card is concise (<150 chars)")
    else:
        print(f"\n⚠️  WARNING: Memory card is {len(memory_card4)} chars (target: <150)")

    if len(preserved) >= 4:
        print("✅ PASSED: Key facts preserved across 4 sessions")
    else:
        print(f"⚠️  WARNING: Only {len(preserved)} key facts preserved")


if __name__ == "__main__":
    try:
        test_with_history()
    except Exception as e:
        print(f"\n❌ TEST FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
