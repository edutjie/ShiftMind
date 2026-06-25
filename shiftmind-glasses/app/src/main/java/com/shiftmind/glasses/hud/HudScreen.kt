package com.shiftmind.glasses.hud

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

/**
 * HUD root, driven by [HudStateHolder].
 *
 * Layout:
 *   ┌─────────────────────────┐
 *   │ ShiftMind          ● REC│  ← title TopCenter + recording indicator TopEnd
 *   │ awaiting customer       │
 *   │                         │
 *   │   ┌────────────┐        │  ← customer card (when populated)
 *   │   │ John D.    │        │
 *   │   │ Last visit │        │
 *   │   └────────────┘        │
 *   │       ┌──┐              │
 *   │       │  │  BoxCursor   │  ← centered both axes
 *   │       └──┘              │
 *   │                         │
 *   └─────────────────────────┘
 *
 * NOTE on prism mirror: the rokid_android_dev_guide.md claims `scaleX = -1f`
 * is required to un-mirror prism output. On Rokid Max Pro the prism does NOT
 * mirror the wearer's view, so applying the flag produces reversed text.
 * Removed for Max Pro. See CLAUDE.md for the diagnostic.
 */
@Composable
fun HudScreen() {
    val state by HudStateHolder.stateFlow.collectAsState()
    val recordingStartedAtMs = remember(state.recordingActive) {
        if (state.recordingActive) System.currentTimeMillis() else 0L
    }
    var recordingNowMs by remember { mutableStateOf(System.currentTimeMillis()) }

    LaunchedEffect(state.recordingActive, recordingStartedAtMs) {
        if (!state.recordingActive) return@LaunchedEffect
        while (true) {
            recordingNowMs = System.currentTimeMillis()
            delay(1_000L)
        }
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black),
    ) {
        // BoxCursor — natural face slot. Drawn first so other overlays stack above.
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(top = 60.dp),
            contentAlignment = Alignment.Center,
        ) {
            BoxCursor()
        }

        // Title + status block — top of viewport, with prism FOV padding.
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(top = 80.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                text = state.title,
                color = Color(0xFF00FF00),
                fontSize = 28.sp,
                fontWeight = FontWeight.Bold,
            )
            state.statusLine?.let { line ->
                Spacer(Modifier.height(4.dp))
                Text(
                    text = line,
                    color = Color.White.copy(alpha = 0.65f),
                    fontSize = 12.sp,
                )
            }
        }

        // Recording indicator — bright red dot in top-right corner.
        if (state.recordingActive) {
            val elapsedSec = ((recordingNowMs - recordingStartedAtMs).coerceAtLeast(0L) / 1_000L).toInt()
            val elapsed = "%02d:%02d".format(elapsedSec / 60, elapsedSec % 60)
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(top = 54.dp, end = 18.dp),
                contentAlignment = Alignment.TopEnd,
            ) {
                Text(
                    text = "● REC $elapsed",
                    color = Color(0xFFFF3030),
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Bold,
                )
            }
        }

        // Customer card — appears just under title when a known face is recognised.
        // Name + stored memory sit near the top; the live transcript is pinned to
        // the bottom of the viewport so it never overlaps the memory card.
        state.customerCard?.let { card ->
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(top = 150.dp, start = 24.dp, end = 24.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                // Customer name
                Text(
                    text = card.name,
                    color = Color(0xFF00FF00),
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold,
                )

                // Last visit info (if available)
                card.lastVisit?.let { visit ->
                    Spacer(Modifier.height(2.dp))
                    Text(
                        text = visit,
                        color = Color.White.copy(alpha = 0.6f),
                        fontSize = 9.sp,
                    )
                }

                Spacer(Modifier.height(6.dp))

                // Stored memory section - fixed height so live-transcript updates
                // below never reflow this block (the cause of flicker).
                Column(
                    modifier = Modifier.height(72.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    if (card.memoryLine != null) {
                        Text(
                            text = "💭 Memory",
                            color = Color(0xFF00FF00).copy(alpha = 0.7f),
                            fontSize = 8.sp,
                        )
                        Spacer(Modifier.height(2.dp))
                        Text(
                            text = card.memoryLine,
                            color = Color(0xFF00FF00).copy(alpha = 0.85f),
                            fontSize = 11.sp,
                            lineHeight = 14.sp,
                            maxLines = 5,
                        )
                    }
                }
            }

            // Live transcript — pinned to a fixed-height box at the bottom of the
            // viewport. Fixed height keeps the text bottom-anchored without
            // reflowing as the line count changes, eliminating flicker.
            if (card.liveTranscript != null) {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(start = 24.dp, end = 24.dp, bottom = 20.dp),
                    contentAlignment = Alignment.BottomCenter,
                ) {
                    Column(
                        modifier = Modifier.height(64.dp),
                        verticalArrangement = Arrangement.Bottom,
                        horizontalAlignment = Alignment.CenterHorizontally,
                    ) {
                        Text(
                            text = "📝 Live",
                            color = Color.White.copy(alpha = 0.7f),
                            fontSize = 8.sp,
                        )
                        Spacer(Modifier.height(2.dp))
                        Text(
                            text = card.liveTranscript,
                            color = Color.White,
                            fontSize = 10.sp,
                            lineHeight = 13.sp,
                            maxLines = 3,
                        )
                    }
                }
            }
        }
    }
}
