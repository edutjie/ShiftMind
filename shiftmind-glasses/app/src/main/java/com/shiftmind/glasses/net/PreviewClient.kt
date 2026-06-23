package com.shiftmind.glasses.net

import android.util.Log
import com.shiftmind.glasses.BuildConfig
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Minimal OkHttp wrapper for POST /api/preview-frame.
 *
 * Phase-1.5 scope: blocking calls on whatever thread invokes postFrame(). The
 * caller (CameraPreviewStreamer) runs us off the Camera2 handler thread,
 * so this never blocks the UI thread.
 *
 * Logs network failures at WARN but never throws — preview is best-effort
 * and shouldn't crash the HUD if the laptop is unreachable.
 */
class PreviewClient(private val baseUrl: String = BuildConfig.SERVER_URL) {

    private val client = OkHttpClient.Builder()
        .connectTimeout(1500, TimeUnit.MILLISECONDS)
        .writeTimeout(2000, TimeUnit.MILLISECONDS)
        .readTimeout(2000, TimeUnit.MILLISECONDS)
        // Disable retries — one missed frame is fine; backed-up retries cause queue grow.
        .retryOnConnectionFailure(false)
        .build()

    private val endpoint = baseUrl.trimEnd('/') + "/api/preview-frame"

    private var consecutiveErrors = 0

    /** Returns true on 2xx, false otherwise. Never throws. */
    fun postFrame(jpeg: ByteArray): Boolean {
        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart(
                "image",
                "frame.jpg",
                jpeg.toRequestBody("image/jpeg".toMediaType()),
            )
            .build()
        val req = Request.Builder().url(endpoint).post(body).build()
        return try {
            client.newCall(req).execute().use { resp ->
                if (resp.isSuccessful) {
                    if (consecutiveErrors > 0) {
                        Log.i(TAG, "preview pipeline recovered after $consecutiveErrors errors")
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

    private fun bumpError(reason: String) {
        consecutiveErrors++
        // Log every 10th error so a dead server doesn't flood logcat.
        if (consecutiveErrors == 1 || consecutiveErrors % 10 == 0) {
            Log.w(TAG, "POST $endpoint failed (#$consecutiveErrors): $reason")
        }
    }

    companion object {
        private const val TAG = "ShiftMind.Preview"
    }
}
