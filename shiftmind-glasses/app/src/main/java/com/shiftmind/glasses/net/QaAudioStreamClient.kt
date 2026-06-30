package com.shiftmind.glasses.net

import android.util.Log
import com.shiftmind.glasses.BuildConfig
import kotlinx.coroutines.delay
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * Live ASR stream for glasses-side Q&A — mirrors [SessionAudioStreamClient] but
 * talks to `/api/qa/audio-stream` (no customer session).
 *
 * Glasses send 16kHz mono PCM chunks via [sendPcm]; the server relays them to
 * Doubao Seed ASR and pushes back partial/final transcripts, surfaced via
 * [onTranscript] so the HUD can show the spoken question as it forms.
 *
 * [stopAndWait] sends `eof`, waits briefly for Doubao's final transcript, then
 * returns the gathered question text. [cancel] closes immediately (teardown).
 */
class QaAudioStreamClient(private val baseUrl: String = BuildConfig.SERVER_URL) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(2500, TimeUnit.MILLISECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)  // long-lived WS
        .build()

    private val socket = AtomicReference<WebSocket?>(null)
    private val transcript = StringBuilder()
    @Volatile private var partial = ""

    /** True once the server reports Doubao ASR connected. */
    @Volatile var asrReady = false
        private set

    /** (displayText, isFinal) — displayText joins finalized + partial text. */
    var onTranscript: ((String, Boolean) -> Unit)? = null

    fun start() {
        cancel()
        transcript.setLength(0)
        partial = ""
        asrReady = false
        val wsUrl = baseUrl.trimEnd('/')
            .replaceFirst("http://", "ws://")
            .replaceFirst("https://", "wss://") +
            "/api/qa/audio-stream"
        val req = Request.Builder().url(wsUrl).build()
        val ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "qa audio stream opened")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val obj = runCatching { JSONObject(text) }.getOrNull() ?: return
                when (obj.optString("type")) {
                    "transcript" -> {
                        synchronized(transcript) {
                            transcript.setLength(0)
                            transcript.append(obj.optString("final_text"))
                        }
                        partial = obj.optString("partial_text")
                        onTranscript?.invoke(displayText(), obj.optBoolean("final", false))
                    }
                    "asr_status" -> {
                        val status = obj.optString("status")
                        Log.i(TAG, "ASR $status")
                        if (status == "connected") asrReady = true
                    }
                    "error" -> Log.w(TAG, "qa audio stream error: ${obj.optString("message")}")
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "qa audio stream failed: ${t.message}")
                socket.compareAndSet(webSocket, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "qa audio stream closed: $code $reason")
                socket.compareAndSet(webSocket, null)
            }
        })
        socket.set(ws)
    }

    fun sendPcm(chunk: ByteArray) {
        socket.get()?.send(chunk.toByteString())
    }

    /**
     * Send `eof`, wait [FINAL_FLUSH_MS] for Doubao to return the final transcript,
     * then close. Returns the gathered question text. Suspend — call off the
     * IO dispatcher.
     */
    suspend fun stopAndWait(): String {
        val s = socket.getAndSet(null) ?: return displayText()
        s.send("eof")
        delay(FINAL_FLUSH_MS)
        s.close(1000, "qa ended")
        return displayText()
    }

    /** Immediate close, no wait — for teardown (onPause/onDestroy). */
    fun cancel() {
        socket.getAndSet(null)?.close(1000, "cancelled")
    }

    private fun displayText(): String {
        val finalText = synchronized(transcript) { transcript.toString() }.trim()
        return listOf(finalText, partial.trim()).filter { it.isNotBlank() }.joinToString(" ")
    }

    companion object {
        private const val TAG = "ShiftMind.QaAudioStream"
        private const val FINAL_FLUSH_MS = 1800L
    }
}
