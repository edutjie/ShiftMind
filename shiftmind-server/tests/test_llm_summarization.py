"""Test LLM summarization with a sample transcript."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

# Import the session service
from session_service import _summarize_session, _openai_client


def test_llm_client():
    """Test if OpenAI client is properly configured."""
    print("="*60)
    print("TEST 1: OpenAI Client Configuration")
    print("="*60)

    client = _openai_client()
    if client is None:
        print("❌ FAILED: OpenAI client not configured")
        print("   Check OPENAI_API_KEY in .env")
        return False

    print("✅ OpenAI client configured")
    api_key = os.getenv("SHIFTMIND_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = os.getenv("SHIFTMIND_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "default"
    print(f"   API Key: {api_key[:20]}...")
    print(f"   Base URL: {base_url}")
    print(f"   Model: {os.getenv('SHIFTMIND_LLM_MODEL', 'gpt-4o-mini')}")
    return True


def test_summarization():
    """Test summarization with a sample transcript."""
    print("\n" + "="*60)
    print("TEST 2: LLM Summarization")
    print("="*60)

    # Test case 1: New customer introducing themselves
    print("\nTest Case 1: New Customer Introduction")
    print("-" * 40)

    transcript1 = "Hello, my name is Messi. I like to play football and my favorite color is blue. I'm from Argentina. I play with my left foot."

    result1 = _summarize_session(
        customer_id="person-001",
        existing_name="",
        existing_memory="",
        is_new_customer=True,
        transcript=transcript1,
    )

    print(f"Transcript: {transcript1}")
    print(f"\nLLM Status: {result1.get('_llm_status')}")
    print(f"Summary: {result1.get('summary')}")
    print(f"Important Points: {result1.get('important_points')}")
    print(f"Extracted Name: {result1.get('name')}")

    # Check if it's just copying the transcript
    if result1.get('summary') == transcript1[:500]:
        print("\n❌ FAILED: Summary is identical to transcript (LLM not working)")
        return False

    if result1.get('_llm_status') == 'summarized':
        print("\n✅ PASSED: LLM generated proper summary")
    else:
        print(f"\n❌ FAILED: LLM status = {result1.get('_llm_status')}")
        return False

    # Test case 2: Returning customer with existing memory
    print("\n\nTest Case 2: Returning Customer")
    print("-" * 40)

    transcript2 = "Hi, I'm back. Today I need some new football boots. I prefer Adidas."
    existing_memory = "Likes football, from Argentina, plays with left foot"

    result2 = _summarize_session(
        customer_id="person-001",
        existing_name="Messi",
        existing_memory=existing_memory,
        is_new_customer=False,
        transcript=transcript2,
    )

    print(f"Existing Memory: {existing_memory}")
    print(f"Transcript: {transcript2}")
    print(f"\nLLM Status: {result2.get('_llm_status')}")
    print(f"Summary: {result2.get('summary')}")
    print(f"Important Points: {result2.get('important_points')}")

    if result2.get('_llm_status') == 'summarized':
        print("\n✅ PASSED: LLM generated summary for returning customer")
    else:
        print(f"\n❌ FAILED: LLM status = {result2.get('_llm_status')}")
        return False

    return True


def test_memory_composition():
    """Test memory card composition across sessions."""
    print("\n" + "="*60)
    print("TEST 3: Memory Card Composition")
    print("="*60)

    from session_service import _compose_memory_card

    # Session 1
    summary1 = "Customer introduced as Messi, likes football"
    points1 = ["name: Messi", "likes: football", "from: Argentina"]
    existing1 = ""

    memory1 = _compose_memory_card(summary1, points1, existing1)
    print(f"\nSession 1 Memory Card:")
    print(f"{memory1}")

    # Session 2
    summary2 = "Customer needs football boots"
    points2 = ["needs: football boots", "brand preference: Adidas"]

    memory2 = _compose_memory_card(summary2, points2, memory1)
    print(f"\nSession 2 Memory Card (accumulated):")
    print(f"{memory2}")

    # Check if accumulation works
    if "Messi" in memory2 and "football boots" in memory2:
        print("\n✅ PASSED: Memory card accumulates across sessions")
        return True
    else:
        print("\n❌ FAILED: Memory card not accumulating properly")
        return False


if __name__ == "__main__":
    print("\n🧪 LLM SUMMARIZATION TEST SUITE\n")

    try:
        result1 = test_llm_client()
        if not result1:
            print("\n❌ Cannot proceed - OpenAI client not configured")
            exit(1)

        result2 = test_summarization()
        result3 = test_memory_composition()

        print("\n" + "="*60)
        print("FINAL RESULTS")
        print("="*60)
        print(f"OpenAI Client: {'✅ PASS' if result1 else '❌ FAIL'}")
        print(f"LLM Summarization: {'✅ PASS' if result2 else '❌ FAIL'}")
        print(f"Memory Composition: {'✅ PASS' if result3 else '❌ FAIL'}")

        if result1 and result2 and result3:
            print("\n🎉 ALL TESTS PASSED!")
            exit(0)
        else:
            print("\n❌ SOME TESTS FAILED")
            exit(1)

    except Exception as e:
        print(f"\n❌ TEST SUITE ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
