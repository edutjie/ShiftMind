package com.shiftmind.glasses.hud

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * Process-wide HudState holder. Acts as a tiny in-memory store — no DI, no
 * Hilt, no ViewModel — because at this scope it's not worth the ceremony.
 *
 * Update via `mutate { it.copy(...) }`. Compose reads via [stateFlow.collectAsState]
 * and rerenders; the recording overlay renderer reads the latest value when
 * preparing each video frame.
 *
 * Thread-safe because StateFlow handles concurrent emits.
 */
object HudStateHolder {

    private val _state = MutableStateFlow(HudState())
    val stateFlow: StateFlow<HudState> = _state

    /** Convenience: return current snapshot without collecting the flow. */
    val current: HudState get() = _state.value

    /** Atomic update — `mutate { it.copy(...) }` style. */
    fun mutate(transform: (HudState) -> HudState) {
        _state.value = transform(_state.value)
    }
}
