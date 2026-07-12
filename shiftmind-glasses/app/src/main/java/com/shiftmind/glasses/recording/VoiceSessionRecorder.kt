package com.shiftmind.glasses.recording

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.util.Log
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread
import kotlin.math.log10
import kotlin.math.sqrt

/**
 * Live-ASR/WAV consumer for [SharedMicCapture]. It deliberately never opens an
 * AudioRecord itself: a concurrent AR recording consumes the same source PCM
 * instead of making the Rokid HAL serve two quad-mic clients.
 */
class VoiceSessionRecorder(
    private val context: Context,
    private val micCapture: SharedMicCapture,
) {
    private val running = AtomicBoolean(false)
    private val pendingChunks = ArrayBlockingQueue<SharedMicCapture.Chunk>(MAX_PENDING_CHUNKS)
    private val droppedChunks = AtomicLong(0)

    private var subscription: SharedMicCapture.Subscription? = null
    private var worker: Thread? = null
    private var activeSessionPcm: ByteArrayOutputStream? = null
    private var meterSamples = 0L
    private var meterSquareSum = 0.0
    private var meterPeak = 0

    val isRecording: Boolean get() = running.get()

    @Synchronized
    fun start(onPcmChunk: ((ByteArray) -> Unit)? = null): Boolean {
        if (running.get()) return true
        if (worker?.isAlive == true) {
            Log.w(TAG, "voice session start blocked: previous PCM drain is still closing")
            return false
        }
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "voice session blocked: RECORD_AUDIO not granted")
            return false
        }

        pendingChunks.clear()
        droppedChunks.set(0)
        resetMeter()
        val sessionPcm = ByteArrayOutputStream()
        activeSessionPcm = sessionPcm
        running.set(true)

        val newSubscription = micCapture.subscribe("live ASR") { chunk ->
            if (!running.get()) return@subscribe
            if (!pendingChunks.offer(chunk)) {
                logQueueDrop(droppedChunks.incrementAndGet(), chunk.sequence)
            }
        }
        if (newSubscription == null) {
            running.set(false)
            activeSessionPcm = null
            return false
        }
        subscription = newSubscription

        worker = thread(name = "ShiftMindVoiceSession") {
            drainLoop(sessionPcm, onPcmChunk)
        }
        Log.i(TAG, "voice session started from shared MIC stream")
        return true
    }

    /**
     * Stop accepting microphone data, drain queued PCM, then package the raw
     * session audio. The source subscription closes before join, which lets the
     * shared AudioRecord stop a blocking read instead of waiting behind it.
     */
    @Synchronized
    fun stopWav(): ByteArray? {
        if (!running.getAndSet(false)) return null

        subscription?.close()
        subscription = null
        val sessionWorker = worker
        if (sessionWorker != null && sessionWorker !== Thread.currentThread()) {
            sessionWorker.join(STOP_DRAIN_TIMEOUT_MS)
            if (sessionWorker.isAlive) {
                Log.w(TAG, "voice-session queue did not drain within ${STOP_DRAIN_TIMEOUT_MS}ms")
                sessionWorker.interrupt()
            }
        }
        worker = sessionWorker?.takeIf { it.isAlive }

        // The worker normally drained before the join returned. If a sink is
        // unexpectedly stuck, preserve everything written so far rather than
        // returning an empty WAV; a new session is blocked until it exits.
        val raw = activeSessionPcm?.let { sessionPcm ->
            synchronized(sessionPcm) { sessionPcm.toByteArray() }
        } ?: ByteArray(0)
        activeSessionPcm = null
        pendingChunks.clear()
        Log.i(
            TAG,
            "voice session stopped: pcm=${raw.size} bytes, queuedDrops=${droppedChunks.get()}",
        )
        return wav(raw)
    }

    private fun drainLoop(
        sessionPcm: ByteArrayOutputStream,
        onPcmChunk: ((ByteArray) -> Unit)?,
    ) {
        while (running.get() || pendingChunks.isNotEmpty()) {
            val chunk = try {
                pendingChunks.poll(POLL_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            } catch (_: InterruptedException) {
                break
            } ?: continue
            synchronized(sessionPcm) { sessionPcm.write(chunk.pcm) }
            meterPcm(chunk.pcm)
            try {
                // Preserve the shared chunk for the AAC consumer. OkHttp
                // copies into ByteString, but callers are not allowed to
                // mutate a buffer that also belongs to another consumer.
                onPcmChunk?.invoke(chunk.pcm.copyOf())
            } catch (t: Throwable) {
                Log.w(TAG, "live ASR sink threw for shared chunk ${chunk.sequence}", t)
            }
        }
    }

    private fun wav(raw: ByteArray): ByteArray {
        val out = ByteArrayOutputStream(raw.size + 44)
        val byteRate = SAMPLE_RATE * BYTES_PER_SAMPLE
        out.write("RIFF".toByteArray())
        out.writeIntLE(36 + raw.size)
        out.write("WAVEfmt ".toByteArray())
        out.writeIntLE(16)
        out.writeShortLE(1)
        out.writeShortLE(1)
        out.writeIntLE(SAMPLE_RATE)
        out.writeIntLE(byteRate)
        out.writeShortLE(BYTES_PER_SAMPLE)
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

    /** Maintain the live level meter without modifying shared PCM. */
    private fun meterPcm(input: ByteArray) {
        val sampleCount = input.size / BYTES_PER_SAMPLE
        for (i in 0 until sampleCount) {
            val offset = i * BYTES_PER_SAMPLE
            val sample = ((input[offset + 1].toInt() shl 8) or (input[offset].toInt() and 0xff)).toShort().toInt()
            meterSamples++
            meterSquareSum += sample.toDouble() * sample
            meterPeak = maxOf(meterPeak, kotlin.math.abs(sample))
        }
        if (meterSamples >= SAMPLE_RATE) logAndResetMeter()
    }

    private fun logAndResetMeter() {
        val rms = sqrt(meterSquareSum / maxOf(1L, meterSamples))
        val rmsDbfs = if (rms > 0.0) 20.0 * log10(rms / Short.MAX_VALUE) else -96.0
        val peakDbfs = if (meterPeak > 0) 20.0 * log10(meterPeak.toDouble() / Short.MAX_VALUE) else -96.0
        Log.i(TAG, "live mic level (shared source): rms=%.1fdBFS peak=%.1fdBFS".format(rmsDbfs, peakDbfs))
        resetMeter()
    }

    private fun resetMeter() {
        meterSamples = 0
        meterSquareSum = 0.0
        meterPeak = 0
    }

    private fun logQueueDrop(dropped: Long, sequence: Long) {
        if (dropped == 1L || dropped % DROP_LOG_EVERY == 0L) {
            Log.w(
                TAG,
                "live-ASR queue full: dropped=$dropped at shared seq=$sequence " +
                    "(capacity=$MAX_PENDING_CHUNKS)",
            )
        }
    }

    companion object {
        private const val TAG = "ShiftMind.VoiceSession"
        private const val SAMPLE_RATE = 16_000
        private const val BYTES_PER_SAMPLE = 2
        private const val MAX_PENDING_CHUNKS = 100 // ~2 seconds at 20 ms/chunk
        private const val POLL_TIMEOUT_MS = 100L
        private const val STOP_DRAIN_TIMEOUT_MS = 2_000L
        private const val DROP_LOG_EVERY = 25L
    }
}
