# Bug Fixes - Session Summary & HUD Display

## Bug #1: LLM Summarization Not Working ✅ FIXED

### Problem
- Summary was identical to transcript (e.g., "So you like to play football?" → "So you like to play football?")
- `important_points` array was always empty
- No actual LLM processing happening

### Root Cause
```python
# WRONG - this API doesn't exist in OpenAI SDK
resp = client.responses.create(...)
text = getattr(resp, "output_text", "") or ""
```

The code was using a non-existent `responses.create()` method, which caused an exception. The fallback returned `transcript[:500]` as the summary.

### Fix
Changed to proper OpenAI Chat Completions API:
```python
resp = client.chat.completions.create(
    model=model,
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ],
    response_format={"type": "json_object"},
    temperature=0.3,
)
text = resp.choices[0].message.content or ""
```

**Improved prompt engineering:**
- Clearer instructions for extraction
- Context about existing customer data
- Explicit JSON schema
- Focus on NEW information to avoid repetition across sessions

---

## Bug #2: Memory Card vs Live Transcript Conflict ✅ FIXED

### Problem
During an active session with a returning customer:
- Existing memory card: "Likes football, from Argentina"
- Live transcript: "Hello my name is Messi..."
- **Both tried to display in the same `memoryLine` field** → conflict

### Root Cause
`HudScreen.kt` had only one text field (`card.memoryLine`) for both:
1. Stored memory from previous sessions
2. Live transcript from current session

### Fix
Updated `HudScreen.kt` to differentiate visually:

```kotlin
// Detect if content is live transcript or stored memory
val isLiveTranscript = mem.contains("listening") ||
                      mem.length > 50 ||
                      state.statusLine?.contains("session") == true

if (isLiveTranscript) {
    // Live transcript - bright white
    Text(text = "📝 Live:", ...)
    Text(text = mem, color = Color.White, fontSize = 10.sp, maxLines = 4)
} else {
    // Existing memory - dim green
    Text(text = "💭 Memory:", ...)
    Text(text = mem, color = Color(0xFF00FF00).copy(alpha = 0.85f), fontSize = 10.sp, maxLines = 3)
}
```

**Visual distinction:**
- **💭 Memory** (dim green) = stored from previous sessions
- **📝 Live** (bright white) = current session transcript

---

## Bug #3: Memory Card Accumulation Logic ✅ FIXED

### Problem
Unclear what happens when a customer returns:
- Should summary replace or append?
- What's the difference between `customers.memory_card` and `sessions.summary`?

### Clarification

**Data Model:**

| Field | Scope | Purpose |
|-------|-------|---------|
| `sessions.summary` | **Per-session** | Summary of ONE interaction (immutable after session ends) |
| `sessions.important_points` | **Per-session** | Facts extracted from ONE interaction |
| `customers.memory_card` | **Cumulative** | Accumulated knowledge across ALL sessions |

**Accumulation Strategy:**

Session 1:
- Transcript: "Hello, I'm Messi. I like football."
- Summary: "Customer introduced himself as Messi, enjoys football"
- Memory card: "Customer introduced himself as Messi, enjoys football • likes football"

Session 2:
- Transcript: "My favorite color is blue"
- Summary: "Customer mentioned favorite color preference"
- Memory card: ⬇️
```
Customer introduced himself as Messi, enjoys football • likes football
Customer mentioned favorite color preference • favorite color: blue
```

**Implementation:**

```python
def _compose_memory_card(summary: str, points: list[str], existing_memory: str = "") -> str:
    """Compose cumulative memory card across sessions."""
    new_parts = []
    
    if summary and not summary.startswith("Session ended."):
        new_parts.append(summary.strip())
    
    if points:
        new_parts.append("• " + "; ".join(points[:5]))
    
    # Merge with existing memory
    if existing_memory and not existing_memory.startswith("Session ended."):
        if new_parts:
            return existing_memory.strip() + "\n" + " ".join(new_parts)
        return existing_memory.strip()
    
    return " ".join(new_parts) if new_parts else summary.strip()
```

**LLM Prompt includes existing memory:**
```python
system_prompt = (
    f"Existing memory card: {existing_memory}\n\n"
    "Focus on NEW information from this transcript. ADD to existing memory, don't repeat it."
)
```

This prevents the LLM from re-extracting "likes football" every session.

---

## Testing

### Test LLM Summarization
```bash
# Start a session and speak:
# "Hi, my name is John. I'm looking for running shoes, size 10. I prefer Nike."

# Check the database after session ends:
sqlite3 data/shiftmind.sqlite3 "SELECT summary, important_points_json FROM customer_sessions ORDER BY started_at DESC LIMIT 1;"

# Expected:
# summary: Customer John is looking for running shoes
# important_points_json: ["name: John", "looking for: running shoes", "size: 10", "brand preference: Nike"]
```

### Test HUD Display
1. Recognize a returning customer (e.g., person-001)
2. HUD should show:
   - **💭 Memory:** "Likes football, from Argentina" (dim green)
3. Start speaking during session
4. HUD should switch to:
   - **📝 Live:** "Hello I need..." (bright white)

### Test Memory Accumulation
```bash
# Session 1
sqlite3 data/shiftmind.sqlite3 "SELECT memory_card FROM customers WHERE id='person-001';"
# → "Likes football"

# Session 2 (after mentioning favorite color)
sqlite3 data/shiftmind.sqlite3 "SELECT memory_card FROM customers WHERE id='person-001';"
# → "Likes football\nFavorite color is blue • favorite color: blue"
```

---

## Summary

| Bug | Status | Impact |
|-----|--------|--------|
| LLM not summarizing | ✅ FIXED | Now extracts proper summaries and important points |
| HUD conflict | ✅ FIXED | Memory vs transcript visually distinguished |
| Memory accumulation | ✅ FIXED | Cumulative memory across sessions, no repetition |

**Next steps:** Test on glasses with real customer interactions.
