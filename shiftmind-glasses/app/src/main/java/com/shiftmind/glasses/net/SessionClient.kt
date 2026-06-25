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

class SessionClient(private val baseUrl: String = BuildConfig.SERVER_URL) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(2500, TimeUnit.MILLISECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false)
        .build()

    fun finishSession(sessionId: String, wav: ByteArray?, transcript: String = ""): Boolean {
        val bodyBuilder = MultipartBody.Builder().setType(MultipartBody.FORM)
        if (wav != null) {
            bodyBuilder.addFormDataPart(
                "audio",
                "$sessionId.wav",
                wav.toRequestBody("audio/wav".toMediaType()),
            )
        }
        if (transcript.isNotBlank()) {
            bodyBuilder.addFormDataPart("transcript", transcript)
        }
        val req = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/api/session/$sessionId/finish")
            .post(bodyBuilder.build())
            .build()
        return try {
            client.newCall(req).execute().use { resp ->
                val body = resp.body?.string().orEmpty()
                if (resp.isSuccessful) {
                    Log.i(TAG, "session finished: $sessionId $body")
                    true
                } else {
                    Log.w(TAG, "finish session failed: HTTP ${resp.code} $body")
                    false
                }
            }
        } catch (e: IOException) {
            Log.w(TAG, "finish session failed: ${e.message}")
            false
        }
    }

    companion object {
        private const val TAG = "ShiftMind.SessionClient"
    }
}
