package com.shiftmind.glasses.qa

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

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
 */
object QAModeHolder {
    private val _state = MutableStateFlow(false)
    val stateFlow: StateFlow<Boolean> = _state
    val current: Boolean get() = _state.value

    fun enter() {
        _state.value = true
    }

    fun exit() {
        _state.value = false
    }

    fun toggle() {
        _state.value = !_state.value
    }
}
