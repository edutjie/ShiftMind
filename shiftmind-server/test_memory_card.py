"""Test updated memory card composition with LLM."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from session_service import _update_memory_card_with_llm


def test_memory_card_updates():
    """Test memory card updates across multiple sessions."""
    print("="*60)
    print("TEST: Memory Card Updates with LLM")
    print("="*60)

    # Session 1: Initial customer interaction
    print("\n--- Session 1: First Visit ---")
    existing_memory = ""
    new_summary = "Customer introduced himself as Messi and shared a few personal preferences and background details. He likes playing football, prefers blue, is from Argentina, and plays with his left foot."
    new_points = ['Likes to play football', 'Favorite color is blue', 'From Argentina', 'Plays with his left foot']

    memory1 = _update_memory_card_with_llm(
        customer_id="person-001",
        existing_memory=existing_memory,
        new_summary=new_summary,
        new_points=new_points,
    )

    print(f"Existing memory: (empty)")
    print(f"New summary: {new_summary}")
    print(f"New points: {new_points}")
    print(f"\n✅ Updated memory card ({len(memory1)} chars):")
    print(f"   {memory1}")

    # Session 2: Returning customer
    print("\n--- Session 2: Returns for Football Boots ---")
    new_summary2 = "Customer is returning and needs new football boots. They prefer Adidas."
    new_points2 = ['Prefers Adidas football boots']

    memory2 = _update_memory_card_with_llm(
        customer_id="person-001",
        existing_memory=memory1,
        new_summary=new_summary2,
        new_points=new_points2,
    )

    print(f"Existing memory: {memory1}")
    print(f"New summary: {new_summary2}")
    print(f"New points: {new_points2}")
    print(f"\n✅ Updated memory card ({len(memory2)} chars):")
    print(f"   {memory2}")

    # Session 3: Another visit
    print("\n--- Session 3: Asks About Jersey Sizes ---")
    new_summary3 = "Customer asked about jersey sizes and mentioned he wears size M."
    new_points3 = ['Jersey size: M']

    memory3 = _update_memory_card_with_llm(
        customer_id="person-001",
        existing_memory=memory2,
        new_summary=new_summary3,
        new_points=new_points3,
    )

    print(f"Existing memory: {memory2}")
    print(f"New summary: {new_summary3}")
    print(f"New points: {new_points3}")
    print(f"\n✅ Updated memory card ({len(memory3)} chars):")
    print(f"   {memory3}")

    # Check constraints
    print("\n" + "="*60)
    print("VALIDATION")
    print("="*60)

    print(f"\nSession 1 length: {len(memory1)} chars (target: <150)")
    print(f"Session 2 length: {len(memory2)} chars (target: <150)")
    print(f"Session 3 length: {len(memory3)} chars (target: <150)")

    if len(memory3) > 200:
        print("\n⚠️  WARNING: Memory card exceeds 200 chars - too long for AR display")
    elif len(memory3) > 150:
        print("\n⚠️  WARNING: Memory card exceeds 150 chars - might be hard to read")
    else:
        print("\n✅ PASSED: Memory card is concise and AR-display friendly")

    # Check if it's actually summarizing (not just concatenating)
    if "Session 1" in memory3 or "Session 2" in memory3:
        print("❌ FAILED: Memory contains session markers (not summarizing)")
    elif len(memory3) < len(memory1) + len(memory2):
        print("✅ PASSED: Memory is being summarized (not just concatenated)")
    else:
        print("⚠️  WARNING: Memory might be concatenating instead of summarizing")


if __name__ == "__main__":
    try:
        test_memory_card_updates()
    except Exception as e:
        print(f"\n❌ TEST FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
