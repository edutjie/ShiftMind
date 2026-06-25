package com.shiftmind.glasses.hud

import androidx.compose.runtime.Immutable

/**
 * Single source of truth for what the HUD displays.
 *
 * [HudScreen] consumes this for the prism HUD. Rokid's screenstream recorder
 * captures that same HUD directly, so there is no second Canvas/video HUD
 * renderer to keep in sync.
 *
 * Kept deliberately simple — no behavior, no observable wiring beyond the
 * StateFlow that holds it in `HudStateHolder`. Add fields as new phases need
 * them (transcript triggers, current task, etc.).
 */
@Immutable
data class HudState(
    /** Title shown at top of HUD. Always rendered. */
    val title: String = "ShiftMind",

    /** Subtitle / status line under the title. Null = hidden. */
    val statusLine: String? = "awaiting customer",

    /** Customer "memory card" — populated when face-detect resolves a returning customer. */
    val customerCard: CustomerCard? = null,

    /** True while AR recording is active. Drives the corner REC indicator. */
    val recordingActive: Boolean = false,
)

/**
 * The card that pops up when ShiftMind recognises a customer. Matches the
 * mock response shape in `shiftmind-server/api/detect_face.py`; will be
 * populated from real `/api/detect-face` results in phase 3.
 */
@Immutable
data class CustomerCard(
    val name: String,
    val lastVisit: String?,            // "2026-06-01"
    val memoryLine: String?,           // Stored memory from previous sessions
    val liveTranscript: String? = null, // Live transcript during active session
    val preferences: List<String> = emptyList(),
)
