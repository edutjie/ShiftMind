package com.shiftmind.glasses.net

import android.util.Log
import com.shiftmind.glasses.BuildConfig
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

class SessionAudioStreamClient(private val baseUrl: String = BuildConfig.SERVER_URL) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(2500, TimeUnit.MILLISECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private val socket = AtomicReference<WebSocket?>(null)
    private val transcript = StringBuilder()
    @Volatile private var partial = ""
    var onTranscript: ((String, Boolean) -> Unit)? = null

    fun start(sessionId: String) {
        stop()
        transcript.setLength(0)
        partial = ""
        val wsUrl = baseUrl.trimEnd('/')
            .replaceFirst("http://", "ws://")
            .replaceFirst("https://", "wss://") +
            "/api/session/$sessionId/audio-stream"
        val req = Request.Builder().url(wsUrl).build()
        val ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "audio stream opened: $sessionId")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val obj = runCatching { JSONObject(text) }.getOrNull() ?: return
                when (obj.optString("type")) {
                    "transcript" -> {
                        val finalText = obj.optString("final_text")
                        val partialText = obj.optString("partial_text")
                        val isFinal = obj.optBoolean("final", false)
                        synchronized(transcript) {
                            transcript.setLength(0)
                            transcript.append(finalText)
                        }
                        partial = partialText
                        onTranscript?.invoke(displayText(), isFinal)
                    }
                    "asr_status" -> Log.i(TAG, "ASR ${obj.optString("status")}")
                    "error" -> Log.w(TAG, "audio stream error: ${obj.optString("message")}")
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "audio stream failed: ${t.message}")
                socket.compareAndSet(webSocket, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "audio stream closed: $code $reason")
                socket.compareAndSet(webSocket, null)
            }
        })
        socket.set(ws)
    }

    fun sendPcm(chunk: ByteArray) {
        socket.get()?.send(chunk.toByteString())
    }

    fun stop(): String {
        socket.getAndSet(null)?.let {
            it.send("eof")
            it.close(1000, "session ended")
        }
        return displayText()
    }

    private fun displayText(): String {
        val finalText = synchronized(transcript) { transcript.toString() }.trim()
        return listOf(finalText, partial.trim()).filter { it.isNotBlank() }.joinToString(" ")
    }

    companion object {
        private const val TAG = "ShiftMind.AudioStream"
    }
}
