package com.shiftmind.glasses.hud

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * HUD root.
 *
 * Layout:
 *   ┌─────────────────────────┐
 *   │  ShiftMind              │  ← title TopCenter (with top padding for prism FOV)
 *   │  awaiting customer      │
 *   │                         │
 *   │       ┌──┐              │
 *   │       │  │  BoxCursor   │  ← centered both axes — natural face position
 *   │       └──┘              │
 *   │                         │
 *   └─────────────────────────┘
 *
 * NOTE on prism mirror: the rokid_android_dev_guide.md claims `scaleX = -1f`
 * is required to un-mirror prism output. On Rokid Max Pro the prism does NOT
 * mirror the wearer's view, so applying the flag produces reversed text.
 * Removed for Max Pro. If a different Rokid model needs it, re-add the flag
 * to the root Box (or gate it on Build.MODEL).
 */
@Composable
fun HudScreen() {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black),
    ) {
        // BoxCursor at true screen center — the place the wearer will naturally
        // align with a customer's face. Drawn first so the title overlay sits
        // above it on the z-axis.
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(top = 60.dp),  // small visual offset so cursor + title don't crowd
            contentAlignment = Alignment.Center,
        ) {
            BoxCursor()
        }

        // Title block: top of viewport, with prism FOV padding.
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(top = 80.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                text = "ShiftMind",
                color = Color(0xFF00FF00),
                fontSize = 28.sp,
                fontWeight = FontWeight.Bold,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                text = "awaiting customer",
                color = Color.White.copy(alpha = 0.65f),
                fontSize = 12.sp,
            )
        }
    }
}
