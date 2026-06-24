package com.shiftmind.glasses.net

import android.util.Log
import com.shiftmind.glasses.BuildConfig
import com.shiftmind.glasses.hud.CustomerCard
import com.shiftmind.glasses.hud.HudState
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Best-effort mirror of glasses HudState to the laptop dev preview.
 *
 * Only state is posted. Camera frames stay raw; the server draws the HUD late
 * for browser display so future model inputs never see composited pixels.
 */
class HudStateClient(private val baseUrl: String = BuildConfig.SERVER_URL) {

    private val client = OkHttpClient.Builder()
        .connectTimeout(1500, TimeUnit.MILLISECONDS)
        .writeTimeout(1500, TimeUnit.MILLISECONDS)
        .readTimeout(1500, TimeUnit.MILLISECONDS)
        .retryOnConnectionFailure(false)
        .build()

    private val endpoint = baseUrl.trimEnd('/') + "/api/hud-state"
    private var consecutiveErrors = 0

    fun postState(state: HudState): Boolean {
        val req = Request.Builder()
            .url(endpoint)
            .post(state.toJson().toRequestBody("application/json".toMediaType()))
            .build()
        return try {
            client.newCall(req).execute().use { resp ->
                if (resp.isSuccessful) {
                    if (consecutiveErrors > 0) {
                        Log.i(TAG, "HUD state pipeline recovered after $consecutiveErrors errors")
                        consecutiveErrors = 0
                    }
                    true
                } else {
                    bumpError("HTTP ${resp.code}")
                    false
                }
            }
        } catch (e: IOException) {
            bumpError(e.message ?: e.javaClass.simpleName)
            false
        }
    }

    private fun HudState.toJson(): String = buildString {
        append('{')
        appendJsonField("title", title)
        append(',')
        appendJsonField("statusLine", statusLine)
        append(',')
        append("\"customerCard\":")
        append(customerCard?.toJson() ?: "null")
        append(',')
        append("\"recordingActive\":")
        append(recordingActive)
        append('}')
    }

    private fun CustomerCard.toJson(): String = buildString {
        append('{')
        appendJsonField("name", name)
        append(',')
        appendJsonField("lastVisit", lastVisit)
        append(',')
        appendJsonField("memoryLine", memoryLine)
        append(',')
        append("\"preferences\":[")
        preferences.forEachIndexed { idx, pref ->
            if (idx > 0) append(',')
            appendJsonString(pref)
        }
        append("]}")
    }

    private fun StringBuilder.appendJsonField(name: String, value: String?) {
        appendJsonString(name)
        append(':')
        if (value == null) append("null") else appendJsonString(value)
    }

    private fun StringBuilder.appendJsonString(value: String) {
        append('"')
        value.forEach { ch ->
            when (ch) {
                '\\' -> append("\\\\")
                '"' -> append("\\\"")
                '\n' -> append("\\n")
                '\r' -> append("\\r")
                '\t' -> append("\\t")
                else -> append(ch)
            }
        }
        append('"')
    }

    private fun bumpError(reason: String) {
        consecutiveErrors++
        if (consecutiveErrors == 1 || consecutiveErrors % 10 == 0) {
            Log.w(TAG, "POST $endpoint failed (#$consecutiveErrors): $reason")
        }
    }

    companion object {
        private const val TAG = "ShiftMind.HudState"
    }
}
