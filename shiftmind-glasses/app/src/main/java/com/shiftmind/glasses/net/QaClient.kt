package com.shiftmind.glasses.net

import android.util.Log
import com.shiftmind.glasses.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Result of a glasses-side Q&A turn.
 *
 * Glasses render only [shortAnswer] (≤140 chars, AR-tuned by the server's
 * `_shorten_answer`). [fullAnswer] is the complete server answer — kept here
 * for potential client-side logging but NOT shown on the HUD; the full answer
 * is surfaced server-side (console + /api/qa.html panel) instead.
 */
data class QaAnswer(
    val question: String,
    val shortAnswer: String,
    val fullAnswer: String,
    val elapsedMs: Int,
    val configured: Boolean,
)

/**
 * A single conversation turn for Q&A history.
 */
data class QaHistoryTurn(
    val role: String,
    val content: String,
)

/**
 * OkHttp wrapper for the glasses Q&A text endpoint.
 *
 * Generous read timeout (120s): the server agent loop + `_shorten_answer` can
 * take 10–30s. Mirrors [SessionClient]'s client config. Never throws — returns
 * null on failure so the HUD shows a clean Error state instead of crashing.
 *
 * The spoken question is transcribed separately via [QaAudioStreamClient]
 * (Doubao streaming); this client only POSTs the resulting text to /api/qa/ask.
 *
 * Supports message history for contextual follow-up questions (e.g., "who was
 * the last customer?" followed by "what did he buy?").
 */
class QaClient(private val baseUrl: String = BuildConfig.SERVER_URL) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(2500, TimeUnit.MILLISECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false)
        .build()

    /**
     * POST /api/qa/ask with a text question and optional history.
     * Null on network/HTTP failure.
     *
     * @param question Current question text
     * @param history Optional list of previous conversation turns for context
     */
    suspend fun askText(
        question: String,
        history: List<QaHistoryTurn> = emptyList(),
    ): QaAnswer? = withContext(Dispatchers.IO) {
        val bodyJson = JSONObject().apply {
            put("question", question)
            if (history.isNotEmpty()) {
                put("history", org.json.JSONArray().apply {
                    history.forEach { turn ->
                        put(JSONObject().apply {
                            put("role", turn.role)
                            put("content", turn.content)
                        })
                    }
                })
            }
        }
        val body = bodyJson.toString().toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/api/qa/ask")
            .post(body)
            .build()
        try {
            client.newCall(req).execute().use { resp ->
                val raw = resp.body?.string().orEmpty()
                if (!resp.isSuccessful) {
                    Log.w(TAG, "ask failed: HTTP ${resp.code} $raw")
                    return@use null
                }
                parseAnswer(question, raw)
            }
        } catch (e: IOException) {
            Log.w(TAG, "ask failed: ${e.message}")
            null
        }
    }

    private fun parseAnswer(question: String, raw: String): QaAnswer? {
        val root = runCatching { JSONObject(raw) }.getOrNull() ?: run {
            Log.w(TAG, "ask: non-JSON response: $raw")
            return null
        }
        return QaAnswer(
            question = question,
            shortAnswer = root.optString("short_answer").ifBlank { root.optString("answer") },
            fullAnswer = root.optString("answer"),
            elapsedMs = root.optInt("elapsed_ms"),
            configured = root.optBoolean("configured", true),
        )
    }

    companion object {
        private const val TAG = "ShiftMind.QaClient"
    }
}
