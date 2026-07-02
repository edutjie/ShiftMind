package com.shiftmind.glasses.qa

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * A single Q&A conversation turn (user question or assistant answer).
 */
data class QaTurn(
    val role: String,  // "user" | "assistant"
    val content: String,
)

/**
 * Process-wide flag for the glasses-side Q&A mode.
 *
 * Mirrors the [com.shiftmind.glasses.hud.HudStateHolder] /
 * [com.shiftmind.glasses.session.CustomerSessionHolder] singleton pattern: no
 * DI, just a tiny in-memory store.
 *
 * Two consumers read [current]:
 *  - the UI swap in MainActivity (render [QAScreen] only while true AND no
 *    active customer session);
 *  - the scan gates in CameraPreviewStreamer + PreviewClient (suspend face
 *    detection + server recognition while true so Q&A never triggers a scan).
 *
 * Thread-safe: StateFlow handles concurrent emits.
 *
 * **Message history**: maintains a conversation history for the current Q&A
 * session. Cleared on [exit]. Used to provide context for follow-up questions.
 */
object QAModeHolder {
    private val _state = MutableStateFlow(false)
    val stateFlow: StateFlow<Boolean> = _state
    val current: Boolean get() = _state.value

    private val _history = mutableListOf<QaTurn>()
    val history: List<QaTurn> get() = _history.toList()

    fun enter() {
        _state.value = true
        // Keep existing history when re-entering (in case user toggled by accident)
    }

    fun exit() {
        _state.value = false
        _history.clear()  // Clear history when exiting QA mode
    }

    fun toggle() {
        if (_state.value) {
            exit()
        } else {
            enter()
        }
    }

    fun addTurn(role: String, content: String) {
        _history.add(QaTurn(role, content))
        // Keep at most 20 turns (10 Q&A pairs) to avoid excessive token usage
        if (_history.size > 20) {
            _history.removeAt(0)
            _history.removeAt(0)
        }
    }

    fun clearHistory() {
        _history.clear()
    }
}
