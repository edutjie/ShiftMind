# QA Mode Message History Implementation

## Summary

Modified the glasses-side QA mode to maintain and send conversation history with each query. This enables contextual follow-up questions like:
- User: "Who was the last customer that came here?"
- Assistant: "John Smith visited 30 minutes ago."
- User: "What did he buy or want to buy?" ← **Now works with context**

## Changes Made

### 1. **QAModeHolder.kt** - Added history storage

- Added `QaTurn` data class to represent a conversation turn (role + content)
- Added `_history` mutable list to store conversation turns
- Added `history` property to expose read-only copy
- Added `addTurn(role, content)` method to append turns
- Added `clearHistory()` method to manually clear
- Modified `exit()` to automatically clear history when leaving QA mode
- Limited history to 20 turns (10 Q&A pairs) to avoid excessive token usage

### 2. **QaClient.kt** - Modified request to include history

- Added `QaHistoryTurn` data class matching server's `Turn` model
- Modified `askText()` signature to accept optional `history` parameter
- Updated request body construction to include `history` JSON array when non-empty
- Each history turn includes `role` ("user" or "assistant") and `content`

### 3. **QaController.kt** - Integrated history into workflow

- Modified `stopAndAsk()` to:
  1. Retrieve current history from `QAModeHolder`
  2. Convert to `QaHistoryTurn` list for the API call
  3. Send history along with the current question
  4. Add both question (user) and answer (assistant) to history after successful response
  5. Store the full answer in history (not the shortened version) for better context

## Server Compatibility

The server-side `/api/qa/ask` endpoint already supports the `history` field:

```python
class AskPayload(BaseModel):
    question: str
    filters: QaFilters | None = None
    history: list[Turn] | None = None  # ← Already implemented
```

No server changes were needed.

## Example Flow

1. **First question:**
   - User asks: "Who was the last customer?"
   - History is empty `[]`
   - Server responds with answer
   - History becomes: `[{role: "user", content: "Who was the last customer?"}, {role: "assistant", content: "John Smith..."}]`

2. **Follow-up question:**
   - User asks: "What did he buy?"
   - History sent: previous Q&A pair
   - Server has context about "he" referring to John Smith
   - Server responds with John's purchases
   - History grows: adds this Q&A pair

3. **Exit QA mode:**
   - User swipes forward to exit
   - `QAModeHolder.exit()` clears history
   - Next QA session starts fresh

## Token Management

- History limited to last 20 turns (10 Q&A pairs)
- Automatically prunes oldest turns when limit exceeded
- Balances context retention vs. token cost
- Can be adjusted by modifying the limit in `QAModeHolder.addTurn()`

## Testing Recommendations

1. **Basic follow-up:**
   - Ask: "Who was the last customer?"
   - Ask: "What did they want?"
   - Verify second answer references the customer from first question

2. **Pronoun resolution:**
   - Ask: "Show me sales for last week"
   - Ask: "What about this week?"
   - Verify "this week" is understood in context

3. **History persistence:**
   - Ask multiple questions in one QA session
   - Verify context is maintained across questions

4. **History clearing:**
   - Ask questions in QA mode
   - Exit QA mode
   - Re-enter QA mode
   - Ask a follow-up question
   - Verify it doesn't reference previous session (history should be cleared)

## Build Verification

✅ Compiled successfully with no errors
✅ No API changes needed (server already supports history)
✅ Backward compatible (history parameter is optional)
