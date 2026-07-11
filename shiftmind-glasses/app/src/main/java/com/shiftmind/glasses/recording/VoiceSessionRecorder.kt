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
import kotlin.math.log10
import kotlin.math.sqrt
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

class VoiceSessionRecorder(private val context: Context) {
    private val running = AtomicBoolean(false)
    private var record: AudioRecord? = null
    private var worker: Thread? = null
    private var pcm = ByteArrayOutputStream()
    private var meterSamples = 0L
    private var meterSquareSum = 0.0
    private var meterPeak = 0

    val isRecording: Boolean get() = running.get()

    fun start(onPcmChunk: ((ByteArray) -> Unit)? = null): Boolean {
        if (running.get()) return true
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "voice session blocked: RECORD_AUDIO not granted")
            return false
        }
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, RokidMicCapture.channelMask, PCM_FORMAT)
        if (minBuf <= 0) {
            Log.w(TAG, "AudioRecord min buffer failed: $minBuf")
            return false
        }
        // Live audition on this RG-glasses selected default MIC + QUAD channel
        // 4 as the clearest balanced capture for wearer and customer.
        val rec = AudioRecord.Builder()
            .setAudioSource(MediaRecorder.AudioSource.MIC)
            .setAudioFormat(RokidMicCapture.audioFormat(SAMPLE_RATE))
            .setBufferSizeInBytes(minBuf * 4)
            .build()
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            Log.w(TAG, "AudioRecord init failed for voice session")
            return false
        }
        Log.i(TAG, "voice session input: default MIC, QUAD channel 4 -> mono, gain=1x")
        record = rec
        pcm = ByteArrayOutputStream()
        resetMeter()
        running.set(true)
        rec.startRecording()
        worker = thread(name = "ShiftMindVoiceSession") {
            val quadPcm = ShortArray(RokidMicCapture.FRAMES_PER_CHUNK * RokidMicCapture.INPUT_CHANNEL_COUNT)
            val monoPcm = ByteArray(RokidMicCapture.FRAMES_PER_CHUNK * 2)
            var routeLogged = false
            while (running.get()) {
                val samplesRead = rec.read(quadPcm, 0, quadPcm.size, AudioRecord.READ_BLOCKING)
                if (samplesRead > 0) {
                    if (!routeLogged) {
                        logActualRoute(rec, "voice session")
                        routeLogged = true
                    }
                    val byteCount = RokidMicCapture.extractSelectedChannel(quadPcm, samplesRead, monoPcm)
                    val chunk = meterPcm(monoPcm, byteCount)
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

    /** Copy raw selected-channel PCM while maintaining the live level meter. */
    private fun meterPcm(input: ByteArray, byteCount: Int): ByteArray {
        val output = input.copyOf(byteCount)
        val sampleCount = byteCount / 2
        for (i in 0 until sampleCount) {
            val offset = i * 2
            val raw = ((input[offset + 1].toInt() shl 8) or (input[offset].toInt() and 0xff)).toShort().toInt()

            val sample = raw

            output[offset] = (sample and 0xff).toByte()
            output[offset + 1] = ((sample shr 8) and 0xff).toByte()

            meterSamples++
            meterSquareSum += sample.toDouble() * sample
            meterPeak = maxOf(meterPeak, kotlin.math.abs(sample))
        }
        if (meterSamples >= SAMPLE_RATE) logAndResetMeter()
        return output
    }

    private fun logAndResetMeter() {
        val rms = sqrt(meterSquareSum / maxOf(1L, meterSamples))
        val rmsDbfs = if (rms > 0.0) 20.0 * log10(rms / Short.MAX_VALUE) else -96.0
        val peakDbfs = if (meterPeak > 0) 20.0 * log10(meterPeak.toDouble() / Short.MAX_VALUE) else -96.0
        Log.i(TAG, "live mic level (no gain): rms=%.1fdBFS peak=%.1fdBFS".format(rmsDbfs, peakDbfs))
        resetMeter()
    }

    private fun resetMeter() {
        meterSamples = 0
        meterSquareSum = 0.0
        meterPeak = 0
    }

    /**
     * Log the route selected by the audio policy after capture has actually
     * started. setPreferredDevice() only records a preference; routedDevice is
     * the source the HAL is really delivering PCM from.
     */
    private fun logActualRoute(rec: AudioRecord, label: String) {
        val preferred = rec.preferredDevice
        val routed = rec.routedDevice
        if (routed == null) {
            Log.w(TAG, "$label actual routed device unavailable while recording; preferredId=${preferred?.id}")
            return
        }
        val detail = "$label actual routed device: id=${routed.id}, type=${routed.type}, " +
            "address=${routed.address}, product=${routed.productName}; preferredId=${preferred?.id}"
        if (preferred != null && routed.id != preferred.id) {
            Log.w(TAG, "$detail MISMATCH")
        } else {
            Log.i(TAG, detail)
        }
    }

    companion object {
        private const val TAG = "ShiftMind.VoiceSession"
        private const val SAMPLE_RATE = 16000
        private const val PCM_FORMAT = AudioFormat.ENCODING_PCM_16BIT
    }
}
