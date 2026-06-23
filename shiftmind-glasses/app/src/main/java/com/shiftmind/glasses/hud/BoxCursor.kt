package com.shiftmind.glasses.hud

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * Center "face slot" reticle. Wearer aligns a customer's face inside this
 * frame to trigger recognition.
 *
 * Style: four solid corner brackets + a small center crosshair. No dashed
 * perimeter — cleaner viewfinder feel, less visual noise on the prism.
 *
 * Size: ~240dp. Refined in phase 2 once Camera2 FOV at ~50cm conversation
 * distance is measured on real hardware.
 */
@Composable
fun BoxCursor(
    size: Dp = 240.dp,
    color: Color = Color(0xFF00FF00),
    strokeWidthPx: Float = 4f,
) {
    Canvas(modifier = Modifier.size(size)) {
        val w = this.size.width
        val h = this.size.height
        val bracket = w * 0.18f  // bracket length scales with box

        // Top-left
        drawLine(color, Offset(0f, 0f), Offset(bracket, 0f), strokeWidthPx)
        drawLine(color, Offset(0f, 0f), Offset(0f, bracket), strokeWidthPx)
        // Top-right
        drawLine(color, Offset(w - bracket, 0f), Offset(w, 0f), strokeWidthPx)
        drawLine(color, Offset(w, 0f), Offset(w, bracket), strokeWidthPx)
        // Bottom-right
        drawLine(color, Offset(w - bracket, h), Offset(w, h), strokeWidthPx)
        drawLine(color, Offset(w, h - bracket), Offset(w, h), strokeWidthPx)
        // Bottom-left
        drawLine(color, Offset(0f, h), Offset(bracket, h), strokeWidthPx)
        drawLine(color, Offset(0f, h - bracket), Offset(0f, h), strokeWidthPx)

        // Center crosshair plus
        val cx = w / 2f
        val cy = h / 2f
        val tick = 12f
        drawLine(color, Offset(cx - tick, cy), Offset(cx + tick, cy), strokeWidthPx)
        drawLine(color, Offset(cx, cy - tick), Offset(cx, cy + tick), strokeWidthPx)
    }
}
