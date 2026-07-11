package com.shiftmind.glasses.qa

import android.util.Log
import com.shiftmind.glasses.net.QaAudioStreamClient
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
    /** Ready to record — tap the action key to speak. */
    data object Idle : QaUiState

    /** Recording; [partial] is the live ASR transcript of the question so far. */
    data class Recording(val partial: String) : QaUiState

    data object Thinking : QaUiState

    /** [text] is the ≤140-char short_answer; [question] echoes the transcribed prompt. */
    data class Answer(val question: String, val text: String) : QaUiState

    data class Error(val message: String) : QaUiState
}

/**
 * Owns the Q&A screen state machine + voice capture.
 *
 * Input model (driven by MainActivity.dispatchKeyEvent):
 *   - touchpad swipe-forward (back→front) → toggleQaMode() (MainActivity side)
 *   - touchpad single-tap            → [onActionKey]: record → stop+send → next
 *
 * Voice: [VoiceSessionRecorder] captures selected array channel 4 and feeds PCM chunks
 * to [QaAudioStreamClient] (Doubao streaming), which returns a live transcript
 * shown on the HUD. On stop, the final transcript is POSTed to /api/qa/ask.
 */
class QaController(
    private val voiceSessionRecorder: VoiceSessionRecorder,
    private val qaClient: QaClient,
    private val qaStream: QaAudioStreamClient,
    private val io: CoroutineDispatcher = Dispatchers.IO,
) {
    private val _state = MutableStateFlow<QaUiState>(QaUiState.Idle)
    val stateFlow: StateFlow<QaUiState> = _state
    val current: QaUiState get() = _state.value

    private var scope: CoroutineScope? = null

    /** MainActivity passes its lifecycleScope so launches die on destroy. */
    fun bindScope(scope: CoroutineScope) {
        this.scope = scope
    }

    /** Called on Q&A enter. */
    suspend fun enter() {
        _state.value = QaUiState.Idle
    }

    /** Single-tap of the touchpad while in Q&A. Drives the state machine. */
    fun onActionKey() {
        when (_state.value) {
            QaUiState.Thinking -> Unit  // busy — ignore
            is QaUiState.Recording -> stopAndAsk()
            is QaUiState.Idle, is QaUiState.Answer, is QaUiState.Error -> startRecording()
        }
    }

    /** Stop + discard any open capture (called from onPause / onDestroy). */
    fun cancelIfRecording() {
        if (voiceSessionRecorder.isRecording) {
            voiceSessionRecorder.stopWav()  // discard
            Log.i(TAG, "cancelIfRecording: discarded open capture")
        }
        qaStream.cancel()
    }

    private fun startRecording() {
        qaStream.onTranscript = { text, _ ->
            // Live-update the partial only while still recording (guard so a late
            // transcript after stop doesn't clobber Thinking/Answer).
            if (_state.value is QaUiState.Recording) {
                _state.value = QaUiState.Recording(text)
            }
        }
        qaStream.start()
        val ok = voiceSessionRecorder.start { chunk -> qaStream.sendPcm(chunk) }
        if (!ok) {
            qaStream.cancel()
            _state.value = QaUiState.Error("mic unavailable")
            return
        }
        _state.value = QaUiState.Recording("")
    }

    private fun stopAndAsk() {
        _state.value = QaUiState.Thinking
        val scope = this.scope ?: run {
            _state.value = QaUiState.Error("not ready")
            return
        }
        scope.launch(io) {
            voiceSessionRecorder.stopWav()  // PCM already streamed; discard the WAV
            val question = qaStream.stopAndWait().trim()
            if (question.isBlank()) {
                _state.value = QaUiState.Error(
                    if (!qaStream.asrReady) "ASR not configured (DOUBAO_ASR_API_KEY)" else "no speech heard"
                )
                return@launch
            }

            // Build history from QAModeHolder for contextual follow-up questions
            val history = QAModeHolder.history.map { turn ->
                com.shiftmind.glasses.net.QaHistoryTurn(
                    role = turn.role,
                    content = turn.content
                )
            }

            val r = qaClient.askText(question, history)
            if (r == null) {
                _state.value = QaUiState.Error("server unreachable")
                return@launch
            }
            if (!r.configured) {
                _state.value = QaUiState.Error(r.fullAnswer.ifBlank { "server not configured" })
                return@launch
            }
            val text = r.shortAnswer.ifBlank { r.fullAnswer.ifBlank { "(no answer)" } }

            // Add this turn to history (both question and answer)
            QAModeHolder.addTurn("user", question)
            QAModeHolder.addTurn("assistant", r.fullAnswer.ifBlank { text })

            _state.value = QaUiState.Answer(question = r.question, text = text)
        }
    }

    companion object {
        private const val TAG = "ShiftMind.QaController"
    }
}
