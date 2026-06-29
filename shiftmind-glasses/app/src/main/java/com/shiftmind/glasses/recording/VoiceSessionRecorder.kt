package com.shiftmind.glasses.recording

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
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
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, PCM_FORMAT)
        if (minBuf <= 0) {
            Log.w(TAG, "AudioRecord min buffer failed: $minBuf")
            return false
        }
        // Use MIC source directly - proven to work in HiveGlass project for
        // transcribing both wearer and customer speech. The issue was likely
        // the 4x software gain + limiter, not the audio source.
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
        // Route capture to the Built-In Back Mic. Empirically (MicConfigProbe,
        // shiftmind.audioProbe) the back mic captures BOTH the wearer (-33 dBFS)
        // and the far-field customer (-38 dBFS) better than the default primary
        // input, which runs Qualcomm Fluence beamforming toward the wearer and
        // spectrally mangles far-field speech so ASR drops the customer. Back
        // mic is a different physical device → bypasses that beam. See
        // CLAUDE.md "AudioRecord source on Rokid HAL" for why source-swapping
        // is off the table (VOICE_RECOGNITION = silence, CAMCORDER/UNPROCESSED
        // wedge Camera2); device routing is the safe lever.
        val preferred = findBackMic()
        if (preferred != null) {
            val ok = rec.setPreferredDevice(preferred)
            Log.i(TAG, "voice session setPreferredDevice(backMic id=${preferred.id}) -> $ok")
        } else {
            Log.w(TAG, "no Built-In Back Mic found — falling back to default input")
        }
        record = rec
        pcm = ByteArrayOutputStream()
        resetMeter()
        running.set(true)
        rec.startRecording()
        worker = thread(name = "ShiftMindVoiceSession") {
            val buf = ByteArray(minBuf)
            while (running.get()) {
                val n = rec.read(buf, 0, buf.size)
                if (n > 0) {
                    val chunk = applyGainAndLimit(buf, n)
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

    /**
     * Remove gain and limiter - HiveGlass project proved that raw MIC PCM
     * transcribes customer speech well without amplification. The 4x gain
     * may have been over-amplifying the wearer's voice while still leaving
     * customer speech too quiet.
     */
    private fun applyGainAndLimit(input: ByteArray, byteCount: Int): ByteArray {
        val output = input.copyOf(byteCount)
        val sampleCount = byteCount / 2
        for (i in 0 until sampleCount) {
            val offset = i * 2
            val raw = ((input[offset + 1].toInt() shl 8) or (input[offset].toInt() and 0xff)).toShort().toInt()

            // No gain, no limiter - pass through raw samples for metering only
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

    private fun tryOpenAudioRecord(source: Int, minBuf: Int): AudioRecord? {
        return try {
            val rec = AudioRecord(source, SAMPLE_RATE, CHANNEL_CONFIG, PCM_FORMAT, minBuf * 4)
            if (rec.state == AudioRecord.STATE_INITIALIZED) {
                Log.i(TAG, "AudioRecord opened: source=$source (${sourceToString(source)})")
                rec
            } else {
                Log.w(TAG, "AudioRecord source=$source init failed (state=${rec.state})")
                try { rec.release() } catch (_: Throwable) {}
                null
            }
        } catch (t: Throwable) {
            Log.w(TAG, "AudioRecord source=$source threw: ${t.message}")
            null
        }
    }

    private fun sourceToString(source: Int): String = when (source) {
        MediaRecorder.AudioSource.DEFAULT -> "DEFAULT"
        MediaRecorder.AudioSource.MIC -> "MIC"
        MediaRecorder.AudioSource.VOICE_COMMUNICATION -> "VOICE_COMMUNICATION"
        MediaRecorder.AudioSource.VOICE_RECOGNITION -> "VOICE_RECOGNITION"
        MediaRecorder.AudioSource.CAMCORDER -> "CAMCORDER"
        else -> "unknown($source)"
    }

    /**
     * Find the Built-In Back Mic for setPreferredDevice. Mirrors MicConfigProbe's
     * selection: prefer a BUILTIN_MIC whose product name contains "back", else
     * the second BUILTIN_MIC entry (the array exposes two devices). Returns null
     * if none — caller falls back to the default input.
     */
    private fun findBackMic(): AudioDeviceInfo? {
        val am = context.getSystemService(AudioManager::class.java) ?: return null
        val mics = am.getDevices(AudioManager.GET_DEVICES_ALL)
            .filter { it.isSource && it.type == AudioDeviceInfo.TYPE_BUILTIN_MIC }
        return mics.firstOrNull { it.productName.contains("back", ignoreCase = true) }
            ?: mics.getOrNull(1)
            ?: mics.firstOrNull()
    }

    companion object {
        private const val TAG = "ShiftMind.VoiceSession"
        private const val SAMPLE_RATE = 16000
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val PCM_FORMAT = AudioFormat.ENCODING_PCM_16BIT
        private const val SOFTWARE_GAIN = 4.0f
        private const val LIMITER_THRESHOLD = 28_000
        private const val LIMITER_SLOPE = 0.20f
    }
}
