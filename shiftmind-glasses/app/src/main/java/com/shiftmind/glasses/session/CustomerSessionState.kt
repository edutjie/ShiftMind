package com.shiftmind.glasses.session

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

data class CustomerSessionState(
    val sessionId: String,
    val customerId: String,
    val name: String,
    val isNewCustomer: Boolean,
    val memoryCard: String,
)

object CustomerSessionHolder {
    private val _state = MutableStateFlow<CustomerSessionState?>(null)
    val stateFlow: StateFlow<CustomerSessionState?> = _state
    val current: CustomerSessionState? get() = _state.value

    fun activate(state: CustomerSessionState) {
        if (_state.value?.sessionId != state.sessionId) {
            _state.value = state
        }
    }

    fun clear(sessionId: String? = null) {
        if (sessionId == null || _state.value?.sessionId == sessionId) {
            _state.value = null
        }
    }
}
