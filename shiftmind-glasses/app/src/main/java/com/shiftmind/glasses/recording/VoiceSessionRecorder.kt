package com.shiftmind.glasses.recording

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

class VoiceSessionRecorder(private val context: Context) {
    private val running = AtomicBoolean(false)
    private var record: AudioRecord? = null
    private var worker: Thread? = null
    private var pcm = ByteArrayOutputStream()

    val isRecording: Boolean get() = running.get()

    fun start(onPcmChunk: ((ByteArray) -> Unit)? = null): Boolean {
        if (running.get()) return true
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "voice session blocked: RECORD_AUDIO not granted")
            return false
        }
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, PCM_FORMAT)
        if (minBuf <= 0) {
            Log.w(TAG, "AudioRecord min buffer failed: $minBuf")
            return false
        }
        val rec = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            SAMPLE_RATE,
            CHANNEL_CONFIG,
            PCM_FORMAT,
            minBuf * 4,
        )
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            Log.w(TAG, "AudioRecord init failed for voice session")
            return false
        }
        record = rec
        pcm = ByteArrayOutputStream()
        running.set(true)
        rec.startRecording()
        worker = thread(name = "ShiftMindVoiceSession") {
            val buf = ByteArray(minBuf)
            while (running.get()) {
                val n = rec.read(buf, 0, buf.size)
                if (n > 0) {
                    val chunk = buf.copyOf(n)
                    synchronized(this) { pcm.write(chunk) }
                    onPcmChunk?.invoke(chunk)
                }
            }
        }
        Log.i(TAG, "voice session recording started")
        return true
    }

    fun stopWav(): ByteArray? {
        if (!running.getAndSet(false)) return null
        worker?.join(1200)
        worker = null
        record?.runCatching { stop() }
        record?.release()
        record = null
        val raw = synchronized(this) { pcm.toByteArray() }
        pcm = ByteArrayOutputStream()
        Log.i(TAG, "voice session recording stopped: pcm=${raw.size} bytes")
        return wav(raw)
    }

    private fun wav(raw: ByteArray): ByteArray {
        val out = ByteArrayOutputStream(raw.size + 44)
        val byteRate = SAMPLE_RATE * 2
        out.write("RIFF".toByteArray())
        out.writeIntLE(36 + raw.size)
        out.write("WAVEfmt ".toByteArray())
        out.writeIntLE(16)
        out.writeShortLE(1)
        out.writeShortLE(1)
        out.writeIntLE(SAMPLE_RATE)
        out.writeIntLE(byteRate)
        out.writeShortLE(2)
        out.writeShortLE(16)
        out.write("data".toByteArray())
        out.writeIntLE(raw.size)
        out.write(raw)
        return out.toByteArray()
    }

    private fun ByteArrayOutputStream.writeIntLE(value: Int) {
        write(ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(value).array())
    }

    private fun ByteArrayOutputStream.writeShortLE(value: Int) {
        write(ByteBuffer.allocate(2).order(ByteOrder.LITTLE_ENDIAN).putShort(value.toShort()).array())
    }

    companion object {
        private const val TAG = "ShiftMind.VoiceSession"
        private const val SAMPLE_RATE = 16000
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val PCM_FORMAT = AudioFormat.ENCODING_PCM_16BIT
    }
}
