package com.shiftmind.glasses.recording

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTimestamp
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaRecorder
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import java.nio.ByteBuffer

/**
 * AudioRecord → MediaCodec AAC LC encoder → [RecordingMuxer].
 *
 * Configuration (defaults):
 *   - 16 kHz mono 16-bit PCM (Whisper-friendly, matches the typical AR
 *     glasses single-mic capture).
 *   - AAC LC @ 48 kbps in MP4 container.
 *
 * Threading: PCM capture + codec drain share a single [HandlerThread]
 * (`shiftmind-aenc`). Each loop iteration:
 *   1. Dequeue input buffer, blocking read PCM from AudioRecord into it,
 *      `queueInputBuffer` with monotonic PTS.
 *   2. Drain output buffers (non-blocking) — write samples to muxer.
 *   3. Re-post self.
 *
 * PTS: derived from cumulative sample count, not wall-clock — keeps audio
 * monotonic and prevents the first blocking AudioRecord.read() from pushing
 * the whole audio track later in the MP4 timeline.
 */
class AudioEncoder(
    private val context: Context,
    private val muxer: RecordingMuxer,
    private val sampleRate: Int = 16_000,
    private val bitrate: Int = 48_000,
    /**
     * Software gain applied to each PCM sample before encoding. Rokid HAL's
     * AGC under-shoots for AR-glass close-talk mic placement; ×4 (~+12dB)
     * brings spoken voice from ~-19dBFS peaks to ~-7dBFS, comfortable for
     * Whisper. Set to 1.0f to disable.
     */
    private val softwareGain: Float = 4.0f,
) {

    private var audioRecord: AudioRecord? = null
    private var codec: MediaCodec? = null
    private var trackIndex: Int = -1

    private val thread = HandlerThread("shiftmind-aenc")
    private lateinit var handler: Handler

    private val bufferInfo = MediaCodec.BufferInfo()

    @Volatile private var running = false
    @Volatile private var stopRequested = false
    @Volatile private var draining = false

    private var totalFramesRead: Long = 0L
    // Shared CLOCK_MONOTONIC anchor for A/V sync.
    private var recordingStartNs: Long = 0L
    // Hardware timestamp tracking to avoid discontinuity when HW ts becomes available.
    private val audioTimestamp = AudioTimestamp()
    private var firstHwTimestampNs: Long = 0L
    private var framesAtFirstHwTimestamp: Long = 0L
    // PTS offset to apply when transitioning from fallback to HW timestamp.
    private var ptsOffsetUs: Long = 0L

    private val pcmReadBufSize: Int
    private val pcmTempBuf: ShortArray

    init {
        val minBuf = AudioRecord.getMinBufferSize(
            sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
        )
        require(minBuf > 0) { "AudioRecord.getMinBufferSize failed: $minBuf" }
        // Oversize the AudioRecord internal buffer so blocking reads don't
        // drop samples if drain stalls briefly.
        pcmReadBufSize = maxOf(minBuf * 4, sampleRate / 4 * 2) // ~250ms
        // Per-read chunk: 20ms worth of frames (16k * 0.02 = 320 frames mono).
        pcmTempBuf = ShortArray(sampleRate / 50)
    }

    /** Build AudioRecord + codec. Call before [start]. */
    fun prepare() {
        // MIC source matches VoiceSessionRecorder (ASR). Proven to work on
        // Rokid HAL at declared 16kHz. No resampling, no timestamp correction.
        val record = openAudioRecord(MediaRecorder.AudioSource.MIC)
            ?: error("AudioRecord failed for MIC")

        // Route to the Built-In Back Mic, matching VoiceSessionRecorder's routing.
        // This ensures consistent audio quality (captures both wearer and customer
        // speech) and avoids potential device contention if both recorders run
        // simultaneously during live transcription + AR recording.
        val backMic = findBackMic()
        if (backMic != null) {
            val ok = record.setPreferredDevice(backMic)
            Log.i(TAG, "AR audio encoder setPreferredDevice(backMic id=${backMic.id}) -> $ok")
        } else {
            Log.w(TAG, "no Built-In Back Mic found for AR recording — using default input")
        }

        audioRecord = record

        val mediaFormat = MediaFormat.createAudioFormat(
            MediaFormat.MIMETYPE_AUDIO_AAC, sampleRate, /* channels= */ 1,
        ).apply {
            setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 16384)
        }
        codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC).apply {
            configure(mediaFormat, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        }
        Log.i(TAG, "audio encoder prepared: AAC LC ${sampleRate}Hz mono @ ${bitrate / 1000}kbps")
    }

    private fun openAudioRecord(source: Int): AudioRecord? = try {
        val rec = AudioRecord(
            source,
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            pcmReadBufSize,
        )
        if (rec.state == AudioRecord.STATE_INITIALIZED) {
            Log.i(TAG, "AudioRecord opened: source=$source, sessionId=${rec.audioSessionId}")
            rec
        } else {
            Log.w(TAG, "AudioRecord source=$source init failed (state=${rec.state})")
            try { rec.release() } catch (_: Throwable) {}
            null
        }
    } catch (t: Throwable) {
        Log.w(TAG, "AudioRecord source=$source threw", t)
        null
    }

    fun start(startEpochNs: Long = System.nanoTime()) {
        check(codec != null && audioRecord != null) { "call prepare() first" }
        recordingStartNs = startEpochNs
        codec!!.start()
        audioRecord!!.startRecording()
        running = true
        draining = true
        thread.start()
        handler = Handler(thread.looper)
        handler.post(::pumpLoop)
    }

    /** Signal EOS, wait for drain, tear down. */
    fun stop(timeoutMs: Long = 2000) {
        if (!running) return
        stopRequested = true
        // Log measured sample rate if hardware timestamps available
        if (firstHwTimestampNs > 0L && audioTimestamp.framePosition > 0) {
            val elapsedNs = audioTimestamp.nanoTime - firstHwTimestampNs
            val capturedFrames = audioTimestamp.framePosition - framesAtFirstHwTimestamp
            if (elapsedNs > 0L) {
                val measuredRate = capturedFrames * 1_000_000_000L / elapsedNs
                Log.i(TAG, "measured sample rate: ${measuredRate}Hz (declared ${sampleRate}Hz), captured ${capturedFrames}fr over ${elapsedNs / 1_000_000}ms")
            }
        }
        // Wait for the pump loop to push EOS through codec and drain.
        val deadline = System.nanoTime() + timeoutMs * 1_000_000L
        while (draining && System.nanoTime() < deadline) {
            try { Thread.sleep(20) } catch (_: InterruptedException) { break }
        }
        if (draining) Log.w(TAG, "audio drain did not finish within ${timeoutMs}ms")
        teardown()
    }

    /** Capture + drain. Re-posts self until stopRequested + EOS sent + drained. */
    private fun pumpLoop() {
        try {
            if (running && !stopRequested) {
                feedInput(eos = false)
            } else if (running) {
                // First post after stop — feed the EOS marker, then continue draining.
                feedInput(eos = true)
                running = false
            }
            drainOutput()
            if (draining) {
                // AudioRecord.read() is blocking, so no artificial delay is
                // needed here. A delay changes the observed capture cadence.
                handler.post(::pumpLoop)
            }
        } catch (t: Throwable) {
            Log.e(TAG, "audio pump loop crashed", t)
            draining = false
        }
    }

    private fun feedInput(eos: Boolean) {
        val c = codec ?: return
        val rec = audioRecord ?: return
        val inIdx = c.dequeueInputBuffer(INPUT_DEQUEUE_TIMEOUT_US)
        if (inIdx < 0) return
        val inBuf: ByteBuffer = c.getInputBuffer(inIdx) ?: return
        inBuf.clear()

        val framesToRead = minOf(pcmTempBuf.size, inBuf.capacity() / 2)
        val framesRead = if (eos) 0 else rec.read(pcmTempBuf, 0, framesToRead)
        if (framesRead < 0) {
            Log.w(TAG, "AudioRecord.read returned $framesRead")
            c.queueInputBuffer(inIdx, 0, 0, computePtsUs(), MediaCodec.BUFFER_FLAG_END_OF_STREAM)
            return
        }

        // Track hardware timestamp to measure actual sample rate (HAL may
        // deliver faster than declared 16kHz). Use for PTS, not resampling.
        if (!eos && framesRead > 0) {
            val tsOk = rec.getTimestamp(audioTimestamp, AudioTimestamp.TIMEBASE_MONOTONIC)
            if (tsOk == AudioRecord.SUCCESS && audioTimestamp.framePosition > 0 && audioTimestamp.nanoTime > 0) {
                if (firstHwTimestampNs == 0L) {
                    // First valid HW timestamp - capture the offset between fallback
                    // PTS (sample-based) and HW timestamp to avoid discontinuity.
                    val fallbackPtsUs = totalFramesRead * 1_000_000L / sampleRate
                    val hwPtsUs = (audioTimestamp.nanoTime - recordingStartNs) / 1_000L
                    ptsOffsetUs = fallbackPtsUs - hwPtsUs

                    firstHwTimestampNs = audioTimestamp.nanoTime
                    framesAtFirstHwTimestamp = audioTimestamp.framePosition
                    Log.d(TAG, "first HW ts: ${audioTimestamp.nanoTime}ns @ frame ${audioTimestamp.framePosition}, ptsOffset=${ptsOffsetUs}us")
                }
            }
        }

        val byteCount = framesRead * 2
        if (byteCount > 0) {
            // Direct copy with software gain. No resampling - preserve voice quality.
            for (i in 0 until framesRead) {
                val sample = pcmTempBuf[i]
                val boosted = (sample * softwareGain).toInt()
                    .coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt())
                inBuf.put((boosted and 0xFF).toByte())
                inBuf.put(((boosted shr 8) and 0xFF).toByte())
            }
        }

        val ptsUs = computePtsUs()
        totalFramesRead += framesRead.toLong()
        val flags = if (eos) MediaCodec.BUFFER_FLAG_END_OF_STREAM else 0
        c.queueInputBuffer(inIdx, 0, byteCount, ptsUs, flags)
    }

    private fun computePtsUs(): Long {
        // CRITICAL: Rokid hardware captures at ~15,150Hz (measured), NOT 16,000Hz
        // (declared). Using sample-count-based PTS causes 5.6% speed-up because:
        //   - Capture 15,150 samples in 1 real second
        //   - Label as 16kHz → player thinks they span 15,150/16,000 = 0.947s
        //   - Audio plays 5.6% faster than real-time
        //
        // Solution: Use hardware timestamps (AudioRecord.getTimestamp()) which
        // reflect true CLOCK_MONOTONIC capture time. Anchor to the same epoch as
        // video (recordingStartNs from System.nanoTime()) for A/V sync.
        //
        // To avoid discontinuity when HW timestamp becomes available (~320 frames,
        // ~30s into recording), we compute ptsOffsetUs at transition time to ensure
        // smooth handoff from fallback (sample-based) to HW timestamp.
        if (audioTimestamp.framePosition > 0 && audioTimestamp.nanoTime > 0) {
            // HW timestamp is valid - use it (ground truth) + offset for continuity.
            return (audioTimestamp.nanoTime - recordingStartNs) / 1_000L + ptsOffsetUs
        }
        // HW timestamp not yet available - temporary fallback.
        return totalFramesRead * 1_000_000L / sampleRate
    }

    private fun drainOutput() {
        val c = codec ?: return
        while (true) {
            val outIdx = c.dequeueOutputBuffer(bufferInfo, DEQUEUE_TIMEOUT_US)
            when {
                outIdx == MediaCodec.INFO_TRY_AGAIN_LATER -> return
                outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    if (trackIndex < 0) {
                        trackIndex = muxer.addAudioTrack(c.outputFormat)
                    }
                }
                outIdx >= 0 -> {
                    val outBuf = c.getOutputBuffer(outIdx)
                        ?: error("getOutputBuffer($outIdx) returned null")
                    if ((bufferInfo.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) {
                        // CSD captured via INFO_OUTPUT_FORMAT_CHANGED — drop here.
                        bufferInfo.size = 0
                    }
                    if (bufferInfo.size > 0 && muxer.isStarted) {
                        outBuf.position(bufferInfo.offset)
                        outBuf.limit(bufferInfo.offset + bufferInfo.size)
                        muxer.writeAudio(outBuf, bufferInfo)
                    }
                    c.releaseOutputBuffer(outIdx, false)
                    if ((bufferInfo.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
                        Log.i(TAG, "audio encoder reported EOS")
                        draining = false
                        return
                    }
                }
                else -> Log.w(TAG, "unexpected dequeueOutputBuffer: $outIdx")
            }
        }
    }

    private fun teardown() {
        try { audioRecord?.stop() } catch (_: Throwable) {}
        try { audioRecord?.release() } catch (_: Throwable) {}
        audioRecord = null
        try { codec?.stop() } catch (_: Throwable) {}
        try { codec?.release() } catch (_: Throwable) {}
        codec = null
        try {
            thread.quitSafely()
            thread.join(500)
        } catch (_: InterruptedException) {}
        Log.i(TAG, "audio encoder torn down")
    }

    /**
     * Find the Built-In Back Mic for setPreferredDevice. Mirrors VoiceSessionRecorder's
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
        private const val TAG = "ShiftMind.AEnc"
        private const val INPUT_DEQUEUE_TIMEOUT_US = 10_000L
        private const val DEQUEUE_TIMEOUT_US = 10_000L
    }
}
