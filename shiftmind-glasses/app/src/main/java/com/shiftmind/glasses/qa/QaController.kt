package com.shiftmind.glasses.qa

import android.util.Log
import com.shiftmind.glasses.net.QaClient
import com.shiftmind.glasses.recording.VoiceSessionRecorder
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/** UI states for the glasses Q&A screen. */
sealed interface QaUiState {
    data object Loading : QaUiState
    data class Suggestions(val items: List<String>) : QaUiState
    data object Recording : QaUiState
    data object Thinking : QaUiState

    /** [text] is the ≤140-char short_answer; [question] echoes what was heard. */
    data class Answer(val question: String, val text: String) : QaUiState

    data class Error(val message: String) : QaUiState
}

/**
 * Owns the Q&A screen state machine + voice capture.
 *
 * Input model (driven by MainActivity.dispatchKeyEvent):
 *   - double-tap SESSION_KEY   → toggleQaMode() → enter()/exit() (MainActivity side)
 *   - single-tap RECORDING_KEY → [onActionKey]: record → stop+send → next
 *
 * Voice capture reuses [VoiceSessionRecorder] (routes to the back mic, which
 * captures both wearer and far-field speech). We pass `onPcmChunk = null` to
 * [VoiceSessionRecorder.start] because Q&A does NOT stream PCM — only the final
 * WAV is POSTed one-shot to /api/qa/ask-audio.
 */
class QaController(
    private val voiceSessionRecorder: VoiceSessionRecorder,
    private val qaClient: QaClient,
    private val io: CoroutineDispatcher = Dispatchers.IO,
) {
    private val _state = MutableStateFlow<QaUiState>(QaUiState.Loading)
    val stateFlow: StateFlow<QaUiState> = _state
    val current: QaUiState get() = _state.value

    private var scope: CoroutineScope? = null

    /** MainActivity passes its lifecycleScope so launches die on destroy. */
    fun bindScope(scope: CoroutineScope) {
        this.scope = scope
    }

    /** Called on Q&A enter. Resets to Loading then loads suggestions. */
    suspend fun enter() {
        _state.value = QaUiState.Loading
        val items = runCatching { qaClient.suggestions() }.getOrDefault(emptyList())
        _state.value = QaUiState.Suggestions(items)
    }

    /** Single-tap of RECORDING_KEY while in Q&A. Drives the state machine. */
    fun onActionKey() {
        when (_state.value) {
            QaUiState.Loading, QaUiState.Thinking -> Unit  // busy — ignore
            QaUiState.Recording -> stopAndAsk()
            is QaUiState.Suggestions, is QaUiState.Answer, is QaUiState.Error -> startRecording()
        }
    }

    /** Stop + discard any open capture (called from onPause / onDestroy). */
    fun cancelIfRecording() {
        if (voiceSessionRecorder.isRecording) {
            voiceSessionRecorder.stopWav()  // discard
            Log.i(TAG, "cancelIfRecording: discarded open capture")
        }
    }

    private fun startRecording() {
        val ok = voiceSessionRecorder.start(onPcmChunk = null)
        if (!ok) {
            _state.value = QaUiState.Error("mic unavailable")
            return
        }
        _state.value = QaUiState.Recording
    }

    private fun stopAndAsk() {
        _state.value = QaUiState.Thinking
        val scope = this.scope ?: run {
            _state.value = QaUiState.Error("not ready")
            return
        }
        scope.launch(io) {
            val wav = voiceSessionRecorder.stopWav()
            if (wav == null) {
                _state.value = QaUiState.Error("no audio captured")
                return@launch
            }
            val r = qaClient.askAudio(wav)
            if (r == null) {
                _state.value = QaUiState.Error("server unreachable")
                return@launch
            }
            if (!r.configured) {
                _state.value = QaUiState.Error(r.fullAnswer.ifBlank { "server not configured" })
                return@launch
            }
            val text = r.shortAnswer.ifBlank { r.fullAnswer.ifBlank { "(no answer)" } }
            _state.value = QaUiState.Answer(question = r.question, text = text)
        }
    }

    companion object {
        private const val TAG = "ShiftMind.QaController"
    }
}
