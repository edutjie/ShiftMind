package com.shiftmind.glasses.recording

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import java.nio.ByteBuffer
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.atomic.AtomicLong

/**
 * Shared PCM → MediaCodec AAC LC encoder → [RecordingMuxer].
 *
 * [SharedMicCapture] owns the single hardware AudioRecord. This encoder only
 * consumes its mono chunks on a private bounded queue, so codec back-pressure
 * can never delay a microphone read or steal input from live ASR.
 */
class AudioEncoder(
    private val muxer: RecordingMuxer,
    private val micCapture: SharedMicCapture,
    private val sampleRate: Int = 16_000,
    private val bitrate: Int = 48_000,
) {
    private var codec: MediaCodec? = null
    private var trackIndex: Int = -1
    private var subscription: SharedMicCapture.Subscription? = null

    private val thread = HandlerThread("shiftmind-aenc")
    private lateinit var handler: Handler
    private val bufferInfo = MediaCodec.BufferInfo()
    private val pendingChunks = ArrayBlockingQueue<SharedMicCapture.Chunk>(MAX_PENDING_CHUNKS)
    private var pendingInput: SharedMicCapture.Chunk? = null
    private val droppedChunks = AtomicLong(0)

    @Volatile private var running = false
    @Volatile private var accepting = false
    @Volatile private var stopRequested = false
    @Volatile private var draining = false
    private var eosQueued = false

    private var totalFramesRead = 0L
    private var recordingStartNs = 0L
    private var firstHwTimestampNs = 0L
    private var framesAtFirstHwTimestamp = 0L
    private var lastHwTimestampNs = 0L
    private var lastHwFramePosition = 0L
    private var ptsOffsetUs = 0L
    private var lastPtsUs = 0L

    /** Configure the codec. The microphone subscription begins in [start]. */
    fun prepare() {
        check(codec == null) { "audio encoder already prepared" }
        val mediaFormat = MediaFormat.createAudioFormat(
            MediaFormat.MIMETYPE_AUDIO_AAC,
            sampleRate,
            /* channels= */ 1,
        ).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 16_384)
        }
        codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC).apply {
            configure(mediaFormat, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        }
        Log.i(TAG, "audio encoder prepared: shared MIC -> AAC LC ${sampleRate}Hz mono @ ${bitrate / 1000}kbps")
    }

    /** Start the codec and subscribe to the single shared microphone stream. */
    fun start(startEpochNs: Long = System.nanoTime()) {
        check(codec != null) { "call prepare() first" }
        check(!running) { "audio encoder already started" }

        resetForStart(startEpochNs)
        val activeCodec = codec ?: error("codec disappeared before start")
        activeCodec.start()
        running = true
        accepting = true
        draining = true
        thread.start()
        handler = Handler(thread.looper)

        val newSubscription = micCapture.subscribe("AR AAC") { chunk ->
            offerChunk(chunk)
        }
        if (newSubscription == null) {
            accepting = false
            running = false
            draining = false
            teardown()
            error("shared microphone unavailable for AR recording")
        }
        subscription = newSubscription
        handler.post(::pumpLoop)
        Log.i(TAG, "audio encoder subscribed to shared mic")
    }

    /** Stop input first, drain all accepted PCM, then signal AAC EOS. */
    fun stop(timeoutMs: Long = STOP_TIMEOUT_MS) {
        if (codec == null) return

        accepting = false
        subscription?.close()
        subscription = null
        stopRequested = true
        if (::handler.isInitialized && draining) handler.post(::pumpLoop)

        val deadline = System.nanoTime() + timeoutMs * 1_000_000L
        while (draining && System.nanoTime() < deadline) {
            try {
                Thread.sleep(STOP_WAIT_INTERVAL_MS)
            } catch (_: InterruptedException) {
                break
            }
        }
        if (draining) Log.w(TAG, "audio drain did not finish within ${timeoutMs}ms")
        logMeasuredRate()
        teardown()
    }

    /**
     * Drains at most one PCM block per short tick. Both input and output codec
     * dequeues are non-blocking, so this thread never competes with the source
     * thread's READ_BLOCKING cadence.
     */
    private fun pumpLoop() {
        try {
            val input = pendingInput ?: pendingChunks.poll()
            if (input != null) {
                if (feedInput(input)) {
                    pendingInput = null
                } else {
                    pendingInput = input
                }
            } else if (stopRequested && !eosQueued) {
                queueEndOfStream()
            }

            drainOutput()
            if (draining) {
                handler.postDelayed(::pumpLoop, PUMP_INTERVAL_MS)
            }
        } catch (t: Throwable) {
            Log.e(TAG, "audio encoder pump loop crashed", t)
            draining = false
        }
    }

    /** Offer only; this runs on SharedMicCapture's real-time source thread. */
    private fun offerChunk(chunk: SharedMicCapture.Chunk) {
        if (!accepting) return
        if (!pendingChunks.offer(chunk)) {
            val dropped = droppedChunks.incrementAndGet()
            if (dropped == 1L || dropped % DROP_LOG_EVERY == 0L) {
                Log.w(
                    TAG,
                    "AAC queue full: dropped=$dropped at shared seq=${chunk.sequence} " +
                        "(capacity=$MAX_PENDING_CHUNKS)",
                )
            }
        }
    }

    /** Queue one source-timestamped PCM chunk, returning false if codec input is busy. */
    private fun feedInput(chunk: SharedMicCapture.Chunk): Boolean {
        val activeCodec = codec ?: return false
        val inputIndex = activeCodec.dequeueInputBuffer(NO_WAIT_US)
        if (inputIndex < 0) return false
        val inputBuffer: ByteBuffer = activeCodec.getInputBuffer(inputIndex)
            ?: error("getInputBuffer($inputIndex) returned null")
        inputBuffer.clear()
        check(chunk.pcm.size <= inputBuffer.remaining()) {
            "AAC input buffer too small (${inputBuffer.remaining()} < ${chunk.pcm.size})"
        }
        inputBuffer.put(chunk.pcm)

        val framesRead = chunk.pcm.size / BYTES_PER_SAMPLE
        val hasHardwareTimestamp = observeHardwareTimestamp(chunk)
        val ptsUs = computePtsUs(hasHardwareTimestamp)
        totalFramesRead += framesRead.toLong()
        lastPtsUs = ptsUs
        activeCodec.queueInputBuffer(inputIndex, 0, chunk.pcm.size, ptsUs, 0)
        return true
    }

    private fun queueEndOfStream() {
        val activeCodec = codec ?: return
        val inputIndex = activeCodec.dequeueInputBuffer(NO_WAIT_US)
        if (inputIndex < 0) return
        activeCodec.queueInputBuffer(
            inputIndex,
            0,
            0,
            lastPtsUs,
            MediaCodec.BUFFER_FLAG_END_OF_STREAM,
        )
        eosQueued = true
        running = false
    }

    /**
     * Preserve the earlier A/V calibration: use source hardware timestamps
     * when available, while retaining a sample-clock fallback and a smooth
     * transition between the two. The timestamp travels with PCM, rather than
     * being sampled late after codec backlog has developed.
     */
    private fun observeHardwareTimestamp(chunk: SharedMicCapture.Chunk): Boolean {
        val timestampNs = chunk.captureTimestampNs ?: return false
        val framePosition = chunk.captureFramePosition ?: return false
        if (firstHwTimestampNs == 0L) {
            val fallbackPtsUs = totalFramesRead * 1_000_000L / sampleRate
            val hardwarePtsUs = (timestampNs - recordingStartNs) / 1_000L
            ptsOffsetUs = fallbackPtsUs - hardwarePtsUs
            firstHwTimestampNs = timestampNs
            framesAtFirstHwTimestamp = framePosition
            Log.d(
                TAG,
                "first shared HW ts: ${timestampNs}ns @ frame $framePosition, ptsOffset=${ptsOffsetUs}us",
            )
        }
        lastHwTimestampNs = timestampNs
        lastHwFramePosition = framePosition
        return true
    }

    private fun computePtsUs(hasHardwareTimestamp: Boolean): Long {
        // Never reuse a timestamp from a preceding chunk. A failed timestamp
        // read must fall back to the sample clock rather than queuing repeated
        // PTS values (which players render as a burst or a gap).
        val calculated = if (hasHardwareTimestamp) {
            ((lastHwTimestampNs - recordingStartNs) / 1_000L + ptsOffsetUs).coerceAtLeast(0L)
        } else {
            totalFramesRead * 1_000_000L / sampleRate
        }
        return maxOf(lastPtsUs, calculated)
    }

    /** Drain only ready output; waiting here would make capture queues overflow. */
    private fun drainOutput() {
        val activeCodec = codec ?: return
        while (true) {
            val outputIndex = activeCodec.dequeueOutputBuffer(bufferInfo, NO_WAIT_US)
            when {
                outputIndex == MediaCodec.INFO_TRY_AGAIN_LATER -> return
                outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    if (trackIndex < 0) trackIndex = muxer.addAudioTrack(activeCodec.outputFormat)
                }
                outputIndex >= 0 -> {
                    val outputBuffer = activeCodec.getOutputBuffer(outputIndex)
                        ?: error("getOutputBuffer($outputIndex) returned null")
                    if ((bufferInfo.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) {
                        bufferInfo.size = 0
                    }
                    if (bufferInfo.size > 0) {
                        outputBuffer.position(bufferInfo.offset)
                        outputBuffer.limit(bufferInfo.offset + bufferInfo.size)
                        // RecordingMuxer buffers this until the video format
                        // arrives, so do not discard the opening AAC frames.
                        muxer.writeAudio(outputBuffer, bufferInfo)
                    }
                    activeCodec.releaseOutputBuffer(outputIndex, false)
                    if ((bufferInfo.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
                        Log.i(TAG, "audio encoder reported EOS")
                        draining = false
                        return
                    }
                }
                else -> Log.w(TAG, "unexpected dequeueOutputBuffer: $outputIndex")
            }
        }
    }

    private fun resetForStart(startEpochNs: Long) {
        pendingChunks.clear()
        pendingInput = null
        droppedChunks.set(0)
        trackIndex = -1
        recordingStartNs = startEpochNs
        totalFramesRead = 0L
        firstHwTimestampNs = 0L
        framesAtFirstHwTimestamp = 0L
        lastHwTimestampNs = 0L
        lastHwFramePosition = 0L
        ptsOffsetUs = 0L
        lastPtsUs = 0L
        eosQueued = false
        stopRequested = false
    }

    private fun logMeasuredRate() {
        if (firstHwTimestampNs <= 0L || lastHwTimestampNs <= firstHwTimestampNs) return
        val elapsedNs = lastHwTimestampNs - firstHwTimestampNs
        val capturedFrames = lastHwFramePosition - framesAtFirstHwTimestamp
        if (capturedFrames <= 0L) return
        val measuredRate = capturedFrames * 1_000_000_000L / elapsedNs
        Log.i(
            TAG,
            "shared mic measured sample rate: ${measuredRate}Hz (declared ${sampleRate}Hz), " +
                "captured ${capturedFrames}fr over ${elapsedNs / 1_000_000}ms, " +
                "queuedDrops=${droppedChunks.get()}",
        )
    }

    private fun teardown() {
        accepting = false
        subscription?.close()
        subscription = null
        pendingChunks.clear()
        pendingInput = null
        if (::handler.isInitialized) handler.removeCallbacksAndMessages(null)
        if (thread.isAlive) {
            thread.quitSafely()
            try {
                thread.join(THREAD_JOIN_TIMEOUT_MS)
            } catch (_: InterruptedException) {
            }
        }
        try {
            codec?.stop()
        } catch (_: Throwable) {
        }
        try {
            codec?.release()
        } catch (_: Throwable) {
        }
        codec = null
        running = false
        draining = false
        Log.i(TAG, "audio encoder torn down")
    }

    companion object {
        private const val TAG = "ShiftMind.AEnc"
        private const val BYTES_PER_SAMPLE = 2
        private const val MAX_PENDING_CHUNKS = 100 // ~2 seconds at 20 ms/chunk
        private const val NO_WAIT_US = 0L
        private const val PUMP_INTERVAL_MS = 5L
        private const val STOP_TIMEOUT_MS = 2_000L
        private const val STOP_WAIT_INTERVAL_MS = 20L
        private const val THREAD_JOIN_TIMEOUT_MS = 500L
        private const val DROP_LOG_EVERY = 25L
    }
}
