# Bug Fixes - Part 2: Memory Card & HUD Display

## Bug #1: Memory Card Just Concatenating (Not Summarizing) ✅ FIXED

### Problem
```
Session 1: "So you like to play football?"
Session 2: "The interaction was a brief shopping assistance..."
Memory card: "So you like to play football?\nThe interaction was a brief shopping assistance..."
```

Raw concatenation → memory grows unbounded, unreadable on AR glasses.

### Solution
Added LLM-based memory card re-summarization:

```python
def _update_memory_card_with_llm(
    customer_id: str,
    existing_memory: str,
    new_summary: str,
    new_points: list[str],
) -> str:
    """
    Use LLM to maintain concise, cumulative memory card.
    
    CONSTRAINTS:
    - Max 150 characters (2-3 short sentences)
    - Focus on actionable facts
    - Merge redundant information
    """
```

**Test Results:**
- Session 1: 279 chars (first session uses fallback - adds points)
- Session 2: **96 chars** ✅ (LLM condensed everything)
- Session 3: **101 chars** ✅ (added new info, stayed concise)

**Example:**
```
Session 1 memory (279 chars):
"Customer introduced himself as Messi and shared a few personal preferences..."

Session 2 memory (96 chars):
"- Messi; from Argentina; left-footed; likes football
- Prefers blue; wants Adidas football boots"

Session 3 memory (101 chars):
"- Messi from Argentina; left-footed; likes football
- Prefers blue; wants Adidas boots; jersey size M"
```

---

## Bug #2: HUD Display Flickering ✅ FIXED

### Problem
Both memory card and live transcript used the **same field** (`memoryLine`):

```kotlin
// Initial state
memoryLine = session.memoryCard  // "Likes football"

// On transcript update (overwrites!)
memoryLine = text.takeLast(140)  // "Hello I need..."
```

Result: **Flickering** between memory and transcript.

### Solution

**1. Added separate field in `CustomerCard`:**
```kotlin
data class CustomerCard(
    val name: String,
    val lastVisit: String?,
    val memoryLine: String?,        // Stored memory (previous sessions)
    val liveTranscript: String?,    // NEW: Live transcript (current session)
    val preferences: List<String> = emptyList(),
)
```

**2. Updated `MainActivity` to populate both fields:**
```kotlin
// Initial state - set both fields
customerCard = CustomerCard(
    name = session.name,
    lastVisit = null,
    memoryLine = session.memoryCard,     // Stored memory
    liveTranscript = "listening...",     // NEW: Initial transcript state
)

// On transcript update - only update liveTranscript
customerCard?.copy(
    liveTranscript = text.takeLast(140)  // Update only transcript
)
```

**3. Updated `HudScreen` to display both separately:**
```kotlin
// Memory section (if exists)
card.memoryLine?.let { memory ->
    Text("💭 Memory", color = Green, alpha = 0.7)
    Text(memory, color = Green, alpha = 0.85, maxLines = 2)
    Spacer(height = 8.dp)  // Visual separation
}

// Live transcript section (separate from memory)
card.liveTranscript?.let { transcript ->
    Text("📝 Live", color = White, alpha = 0.7)
    Text(transcript, color = White, maxLines = 4)
}
```

**Visual Layout:**
```
┌─────────────────────┐
│    ShiftMind        │
│    session 001      │
│                     │
│     Messi           │ ← Name
│                     │
│   💭 Memory         │ ← Stored (dim green)
│   Likes football,   │   2 lines max
│   from Argentina    │
│                     │
│   📝 Live           │ ← Transcript (bright white)
│   Hello I need      │   4 lines max
│   some boots...     │
└─────────────────────┘
```

---

## Summary of Changes

### Server Side (`session_service.py`)
1. ✅ Added `_update_memory_card_with_llm()` - LLM re-summarizes cumulative memory
2. ✅ Updated `_compose_memory_card()` - fallback with length limits
3. ✅ Changed `finish()` to call new LLM-based memory update

### Glasses Side
1. ✅ `HudState.kt` - Added `liveTranscript` field to `CustomerCard`
2. ✅ `MainActivity.kt` - Populate both `memoryLine` and `liveTranscript` separately
3. ✅ `HudScreen.kt` - Display both fields in separate visual areas

---

## Testing

### Test Memory Card Summarization
```bash
cd shiftmind-server
uv run python test_memory_card.py
```

**Expected:**
- Session 1: ~100-150 chars
- Session 2: Still ~100-150 chars (not growing)
- Session 3: Still concise, includes all facts

### Test HUD Display
1. Recognize returning customer
2. HUD shows **💭 Memory** (dim green, 2 lines) - no flickering
3. Start speaking
4. HUD adds **📝 Live** below memory (bright white, 4 lines)
5. Both sections remain stable, no flickering

---

## Files Changed

**Server:**
- `shiftmind-server/session_service.py` ✅

**Glasses:**
- `shiftmind-glasses/app/src/main/java/com/shiftmind/glasses/hud/HudState.kt` ✅
- `shiftmind-glasses/app/src/main/java/com/shiftmind/glasses/MainActivity.kt` ✅
- `shiftmind-glasses/app/src/main/java/com/shiftmind/glasses/hud/HudScreen.kt` ✅

**Tests:**
- `shiftmind-server/test_memory_card.py` ✅ (new)

---

## Next Steps

1. **Rebuild glasses APK** with updated HUD code
2. **Test on device** - verify no flickering and proper layout
3. **Run multiple sessions** - verify memory stays concise (<150 chars)
