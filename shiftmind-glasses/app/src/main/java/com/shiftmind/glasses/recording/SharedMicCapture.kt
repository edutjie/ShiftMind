package com.shiftmind.glasses.recording

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioRecord
import android.media.AudioTimestamp
import android.media.MediaRecorder
import android.os.Process
import android.util.Log
import androidx.core.content.ContextCompat
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * The sole owner of the Rokid microphone while ShiftMind is running.
 *
 * The Rokid HAL is unreliable when separate [AudioRecord] clients read the
 * same quad input concurrently. This class reads the hardware once, extracts
 * the selected mono channel, and fans each immutable PCM chunk out to small,
 * non-blocking consumers. Consumers must only enqueue work from [subscribe]'s
 * callback; network or codec work on this capture thread will cause overruns.
 */
class SharedMicCapture(private val context: Context) {

    /** A 20 ms-ish mono PCM block with the source-clock metadata for AAC PTS. */
    data class Chunk(
        val pcm: ByteArray,
        val sequence: Long,
        val captureTimestampNs: Long?,
        val captureFramePosition: Long?,
    )

    interface Subscription {
        fun close()
    }

    private class Subscriber(
        val label: String,
        val onChunk: (Chunk) -> Unit,
    ) {
        val active = AtomicBoolean(true)
    }

    private val lock = Any()
    private val subscribers = CopyOnWriteArrayList<Subscriber>()

    @Volatile private var capturing = false
    private var audioRecord: AudioRecord? = null
    private var worker: Thread? = null
    private var sequence = 0L
    private var lastTimestampNs = 0L
    private var readErrors = 0L

    /**
     * Begin receiving the shared mono stream. Returns null when the physical
     * input cannot be started. The callback runs on the capture thread and
     * therefore must return immediately (use a bounded queue in the consumer).
     */
    fun subscribe(label: String, onChunk: (Chunk) -> Unit): Subscription? {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "$label blocked: RECORD_AUDIO not granted")
            return null
        }

        val subscriber = Subscriber(label, onChunk)
        synchronized(lock) {
            subscribers += subscriber
            if (!capturing && !startLocked()) {
                subscribers -= subscriber
                return null
            }
            Log.i(TAG, "shared mic subscriber added: $label (total=${subscribers.size})")
        }

        return object : Subscription {
            override fun close() = unsubscribe(subscriber)
        }
    }

    private fun unsubscribe(subscriber: Subscriber) {
        if (!subscriber.active.compareAndSet(true, false)) return

        val workerToJoin = synchronized(lock) {
            subscribers -= subscriber
            Log.i(TAG, "shared mic subscriber removed: ${subscriber.label} (total=${subscribers.size})")
            if (subscribers.isNotEmpty() || !capturing) {
                null
            } else {
                // Stop first to unblock READ_BLOCKING, then let the capture
                // thread release the AudioRecord before a later subscriber
                // starts a new session.
                capturing = false
                audioRecord?.runCatching { stop() }
                worker
            }
        }
        if (workerToJoin != null && workerToJoin !== Thread.currentThread()) {
            workerToJoin.join(STOP_JOIN_TIMEOUT_MS)
            if (workerToJoin.isAlive) {
                Log.w(TAG, "shared mic worker did not stop within ${STOP_JOIN_TIMEOUT_MS}ms")
            }
        }
    }

    private fun startLocked(): Boolean {
        // A previous capture is still unwinding. Do not open a second source
        // while it owns the Rokid HAL.
        if (audioRecord != null) {
            Log.w(TAG, "shared mic start deferred: previous AudioRecord is still closing")
            return false
        }

        val minBuffer = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            RokidMicCapture.channelMask,
            RokidMicCapture.PCM_FORMAT,
        )
        if (minBuffer <= 0) {
            Log.w(TAG, "AudioRecord.getMinBufferSize failed: $minBuffer")
            return false
        }
        val bufferBytes = maxOf(
            minBuffer * 4,
            SAMPLE_RATE / 4 * RokidMicCapture.INPUT_CHANNEL_COUNT * BYTES_PER_SAMPLE,
        )

        val record = try {
            AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.MIC)
                .setAudioFormat(RokidMicCapture.audioFormat(SAMPLE_RATE))
                .setBufferSizeInBytes(bufferBytes)
                .build()
        } catch (t: Throwable) {
            Log.w(TAG, "shared mic AudioRecord creation failed", t)
            return false
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            Log.w(TAG, "shared mic AudioRecord init failed: state=${record.state}")
            record.release()
            return false
        }

        try {
            record.startRecording()
        } catch (t: Throwable) {
            Log.w(TAG, "shared mic AudioRecord.startRecording failed", t)
            record.release()
            return false
        }
        if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            Log.w(TAG, "shared mic failed to enter RECORDSTATE_RECORDING: state=${record.recordingState}")
            record.runCatching { stop() }
            record.release()
            return false
        }

        sequence = 0L
        lastTimestampNs = 0L
        readErrors = 0L
        audioRecord = record
        capturing = true
        worker = thread(name = "ShiftMindSharedMic") { captureLoop(record) }
        Log.i(
            TAG,
            "shared mic started: default MIC QUAD channel 4 -> mono, " +
                "sessionId=${record.audioSessionId}, buffer=${bufferBytes}B",
        )
        return true
    }

    private fun captureLoop(record: AudioRecord) {
        try {
            Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO)
        } catch (t: Throwable) {
            Log.w(TAG, "could not elevate shared mic thread priority", t)
        }
        val quadPcm = ShortArray(RokidMicCapture.FRAMES_PER_CHUNK * RokidMicCapture.INPUT_CHANNEL_COUNT)
        val timestamp = AudioTimestamp()
        var routeLogged = false
        try {
            while (capturing && audioRecord === record) {
                val samplesRead = record.read(quadPcm, 0, quadPcm.size, AudioRecord.READ_BLOCKING)
                if (samplesRead <= 0) {
                    if (capturing) logReadError(samplesRead)
                    continue
                }

                val mono = ByteArray((samplesRead / RokidMicCapture.INPUT_CHANNEL_COUNT) * BYTES_PER_SAMPLE)
                val bytesRead = RokidMicCapture.extractSelectedChannel(quadPcm, samplesRead, mono)
                if (bytesRead <= 0) continue
                if (!routeLogged) {
                    logActualRoute(record)
                    routeLogged = true
                }

                val timestampOk = record.getTimestamp(timestamp, AudioTimestamp.TIMEBASE_MONOTONIC) == AudioRecord.SUCCESS &&
                    timestamp.nanoTime > 0L && timestamp.framePosition > 0L
                val timestampNs = timestamp.nanoTime.takeIf { timestampOk }
                val framePosition = timestamp.framePosition.takeIf { timestampOk }
                logCaptureGap(timestampNs, ++sequence)

                val chunk = Chunk(
                    pcm = mono,
                    sequence = sequence,
                    captureTimestampNs = timestampNs,
                    captureFramePosition = framePosition,
                )
                subscribers.forEach { subscriber ->
                    if (!subscriber.active.get()) return@forEach
                    try {
                        // Subscribers only offer into their private bounded
                        // queues; they never perform network/codec work here.
                        subscriber.onChunk(chunk)
                    } catch (t: Throwable) {
                        Log.e(TAG, "shared mic subscriber ${subscriber.label} threw; keeping capture alive", t)
                    }
                }
            }
        } finally {
            record.runCatching { stop() }
            record.runCatching { release() }
            synchronized(lock) {
                if (audioRecord === record) {
                    audioRecord = null
                    worker = null
                    capturing = false
                }
            }
            Log.i(TAG, "shared mic stopped: chunks=$sequence subscribers=${subscribers.size}")
        }
    }

    private fun logReadError(result: Int) {
        readErrors += 1
        if (readErrors == 1L || readErrors % 25L == 0L) {
            Log.w(TAG, "shared mic read returned $result (count=$readErrors)")
        }
    }

    private fun logCaptureGap(timestampNs: Long?, currentSequence: Long) {
        val previous = lastTimestampNs
        if (timestampNs != null && previous > 0L) {
            val gapMs = (timestampNs - previous) / 1_000_000L
            if (gapMs > CAPTURE_GAP_WARNING_MS) {
                Log.w(TAG, "shared mic capture gap: seq=$currentSequence delta=${gapMs}ms")
            }
        }
        if (timestampNs != null) lastTimestampNs = timestampNs
    }

    private fun logActualRoute(record: AudioRecord) {
        val preferred = record.preferredDevice
        val routed = record.routedDevice
        if (routed == null) {
            Log.w(TAG, "shared mic routed device unavailable; preferredId=${preferred?.id}")
            return
        }
        Log.i(
            TAG,
            "shared mic actual routed device: id=${routed.id}, type=${routed.type}, " +
                "address=${routed.address}, product=${routed.productName}; preferredId=${preferred?.id}",
        )
    }

    private companion object {
        const val TAG = "ShiftMind.AudioStream"
        const val SAMPLE_RATE = 16_000
        const val BYTES_PER_SAMPLE = 2
        const val STOP_JOIN_TIMEOUT_MS = 1_000L
        const val CAPTURE_GAP_WARNING_MS = 60L
    }
}
