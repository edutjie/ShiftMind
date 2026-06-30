package com.shiftmind.glasses.net

import android.util.Log
import com.shiftmind.glasses.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
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
    val transcribeStatus: String?,
)

/**
 * OkHttp wrapper for the glasses Q&A endpoints.
 *
 * Generous read timeout (120s): the server agent loop + `_shorten_answer` can
 * take 10–30s. Mirrors [SessionClient]'s client config. Never throws — returns
 * null/empty on failure so the HUD shows a clean Error state instead of crashing.
 */
class QaClient(private val baseUrl: String = BuildConfig.SERVER_URL) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(2500, TimeUnit.MILLISECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false)
        .build()

    /** GET /api/qa/suggestions. Returns the suggestion strings (empty on failure). */
    suspend fun suggestions(): List<String> = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/api/qa/suggestions")
            .get()
            .build()
        try {
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) {
                    Log.w(TAG, "suggestions failed: HTTP ${resp.code}")
                    return@use emptyList()
                }
                val arr = JSONObject(resp.body?.string().orEmpty())
                    .optJSONArray("suggestions") ?: return@use emptyList()
                (0 until arr.length()).mapNotNull { idx ->
                    arr.optString(idx).takeIf { it.isNotBlank() }
                }
            }
        } catch (e: IOException) {
            Log.w(TAG, "suggestions failed: ${e.message}")
            emptyList()
        }
    }

    /**
     * POST /api/qa/ask-audio with a spoken WAV. Returns null on network/HTTP
     * failure (caller shows Error). A non-null result with
     * [QaAnswer.configured] == false means the server's LLM/transcription is
     * not configured — surface its message via Error.
     */
    suspend fun askAudio(wav: ByteArray): QaAnswer? = withContext(Dispatchers.IO) {
        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("audio", "qa.wav", wav.toRequestBody("audio/wav".toMediaType()))
            .build()
        val req = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/api/qa/ask-audio")
            .post(body)
            .build()
        try {
            client.newCall(req).execute().use { resp ->
                val raw = resp.body?.string().orEmpty()
                if (!resp.isSuccessful) {
                    Log.w(TAG, "ask-audio failed: HTTP ${resp.code} $raw")
                    return@use null
                }
                parseAnswer(raw)
            }
        } catch (e: IOException) {
            Log.w(TAG, "ask-audio failed: ${e.message}")
            null
        }
    }

    private fun parseAnswer(raw: String): QaAnswer? {
        val root = runCatching { JSONObject(raw) }.getOrNull() ?: run {
            Log.w(TAG, "ask-audio: non-JSON response: $raw")
            return null
        }
        return QaAnswer(
            question = root.optString("question"),
            shortAnswer = root.optString("short_answer").ifBlank { root.optString("answer") },
            fullAnswer = root.optString("answer"),
            elapsedMs = root.optInt("elapsed_ms"),
            configured = root.optBoolean("configured", true),
            transcribeStatus = root.optString("transcribe_status").ifBlank { null },
        )
    }

    companion object {
        private const val TAG = "ShiftMind.QaClient"
    }
}
