package com.shiftmind.glasses.qa

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
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

private val Green = Color(0xFF00FF00)
private val Muted = Color.White.copy(alpha = 0.65f)
private val Red = Color(0xFFFF6060)

/**
 * Q&A HUD screen — entered by double-tapping SESSION_KEY when no customer is
 * scanned. Mirrors [com.shiftmind.glasses.hud.HudScreen]'s visual language
 * (black bg, green monospace) so it reads as the same surface on the prism.
 *
 * Input is keys + voice only (touch precision on the Rokid touchpad is
 * unverified): suggestions are recommendations to SPEAK aloud, not tap targets.
 */
@Composable
fun QAScreen(controller: QaController) {
    val state by controller.stateFlow.collectAsState()

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black),
    ) {
        // Main content — top of viewport with prism FOV padding.
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(top = 80.dp, start = 24.dp, end = 24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                text = "ShiftMind Q&A",
                color = Green,
                fontSize = 24.sp,
                fontWeight = FontWeight.Bold,
            )
            Spacer(Modifier.height(10.dp))

            when (val s = state) {
                is QaUiState.Loading -> {
                    Text("loading suggestions…", color = Muted, fontSize = 12.sp)
                }

                is QaUiState.Thinking -> {
                    Text("thinking…", color = Green, fontSize = 16.sp, fontWeight = FontWeight.Bold)
                }

                is QaUiState.Recording -> {
                    Text("● Listening…", color = Green, fontSize = 18.sp, fontWeight = FontWeight.Bold)
                    Spacer(Modifier.height(4.dp))
                    Text("tap ● (rec key) to ask", color = Muted, fontSize = 11.sp)
                }

                is QaUiState.Suggestions -> {
                    Text("speak any question (tap ● to record):", color = Muted, fontSize = 11.sp)
                    Spacer(Modifier.height(8.dp))
                    if (s.items.isEmpty()) {
                        Text("(no suggestions — just ask)", color = Muted, fontSize = 11.sp)
                    } else {
                        s.items.take(5).forEachIndexed { idx, q ->
                            Text(
                                text = "${idx + 1}. $q",
                                color = Green.copy(alpha = 0.85f),
                                fontSize = 11.sp,
                                lineHeight = 14.sp,
                                maxLines = 2,
                            )
                            Spacer(Modifier.height(3.dp))
                        }
                    }
                }

                is QaUiState.Answer -> {
                    if (s.question.isNotBlank()) {
                        Text(s.question, color = Muted, fontSize = 10.sp, maxLines = 2)
                        Spacer(Modifier.height(6.dp))
                    }
                    Text(
                        text = s.text,
                        color = Green,
                        fontSize = 20.sp,
                        fontWeight = FontWeight.Bold,
                        lineHeight = 24.sp,
                        maxLines = 6,
                    )
                }

                is QaUiState.Error -> {
                    Text(s.message, color = Red, fontSize = 13.sp, lineHeight = 16.sp, maxLines = 6)
                    Spacer(Modifier.height(6.dp))
                    Text("tap ● to retry", color = Muted, fontSize = 11.sp)
                }
            }
        }

        // Footer hint — pinned to bottom.
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(start = 24.dp, end = 24.dp, bottom = 16.dp),
            contentAlignment = Alignment.BottomCenter,
        ) {
            Column(
                modifier = Modifier.height(28.dp),
                verticalArrangement = Arrangement.Bottom,
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(
                    text = "● rec = record/ask     ●● session = exit",
                    color = Green.copy(alpha = 0.55f),
                    fontSize = 10.sp,
                )
            }
        }
    }
}
