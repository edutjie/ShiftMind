package com.shiftmind.glasses.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * HUD theme: bright primaries on pure black. Anything else washes out through
 * the prism. Material3 light scheme is unusable on AR optics.
 */
private val GlassHudColorScheme = darkColorScheme(
    primary = Color(0xFF00FF00),    // bright green
    onPrimary = Color.Black,
    secondary = Color(0xFF00BFFF),  // sky blue for accents
    onSecondary = Color.Black,
    background = Color.Black,
    onBackground = Color.White,
    surface = Color.Black,
    onSurface = Color.White,
    error = Color(0xFFFF4040),
    onError = Color.Black,
)

@Composable
fun GlassHudTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = GlassHudColorScheme,
        content = content,
    )
}
