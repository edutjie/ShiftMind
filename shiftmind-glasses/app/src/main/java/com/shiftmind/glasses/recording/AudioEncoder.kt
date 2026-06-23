package com.shiftmind.glasses.recording

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
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
 * monotonic and gives MediaMuxer a sane sample timing even if reads are
 * jittery. Track timeline matches camera SurfaceTexture.timestamp closely
 * enough for sub-frame sync (camera PTS is system-monotonic-ns; we anchor
 * audio PTS to the same epoch on [start]).
 */
class AudioEncoder(
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

    /** PTS anchor — System.nanoTime() at start. Audio PTS = anchor + (samples * 1e6 / sampleRate). */
    private var startEpochNs: Long = 0L
    private var totalFramesRead: Long = 0L

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
        // Source selection on Rokid Max Pro (real-hardware findings, do NOT
        // re-derive from theory — the order matters):
        //   - MIC: heavy HAL AGC + NS. Speech buried in noise floor — RMS
        //     mostly -50 to -90 dBFS, ×4 software gain just amplifies noise.
        //   - VOICE_RECOGNITION: digital silence (hot-word DSP path).
        //   - CAMCORDER: probing it back-to-back with startRecording() trips
        //     a HAL race that wedges the Camera2 session and video frames
        //     stop being delivered — observed empirically. Don't touch it
        //     while the camera is open.
        //   - UNPROCESSED: same risk as CAMCORDER.
        //   - VOICE_COMMUNICATION: different DSP path from MIC. Try as primary.
        // Strategy: open with VOICE_COMMUNICATION, then disable any AGC / NS /
        // AEC effects the HAL attaches to the session. If VOICE_COMMUNICATION
        // init fails on this HAL, fall back to MIC + ×4 software gain.
        val record = openAudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
            ?: openAudioRecord(MediaRecorder.AudioSource.MIC)
            ?: error("AudioRecord failed for both VOICE_COMMUNICATION and MIC")
        audioRecord = record
        disableHalPostProcessing(record.audioSessionId)

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

    /**
     * Disable HAL post-processing on the AudioRecord session. Rokid's MIC path
     * applies aggressive AGC + NS that buries speech in the noise floor
     * (~-90dBFS RMS even with ×4 software gain). Turning these off recovers
     * raw mic levels at the cost of room noise. Whisper handles noise fine;
     * it can't recover destroyed signal.
     */
    private fun disableHalPostProcessing(sessionId: Int) {
        if (sessionId == AudioRecord.ERROR) return
        runCatching {
            if (AutomaticGainControl.isAvailable()) {
                AutomaticGainControl.create(sessionId)?.apply {
                    enabled = false
                    Log.i(TAG, "AGC disabled on session $sessionId (was ${if (enabled) "on" else "off"})")
                }
            } else {
                Log.i(TAG, "AGC effect not available on this HAL")
            }
        }.onFailure { Log.w(TAG, "AGC disable threw", it) }
        runCatching {
            if (NoiseSuppressor.isAvailable()) {
                NoiseSuppressor.create(sessionId)?.apply {
                    enabled = false
                    Log.i(TAG, "NS disabled on session $sessionId")
                }
            } else {
                Log.i(TAG, "NS effect not available on this HAL")
            }
        }.onFailure { Log.w(TAG, "NS disable threw", it) }
        runCatching {
            if (AcousticEchoCanceler.isAvailable()) {
                AcousticEchoCanceler.create(sessionId)?.apply {
                    enabled = false
                    Log.i(TAG, "AEC disabled on session $sessionId")
                }
            } else {
                Log.i(TAG, "AEC effect not available on this HAL")
            }
        }.onFailure { Log.w(TAG, "AEC disable threw", it) }
    }

    fun start(startEpochNs: Long = System.nanoTime()) {
        check(codec != null && audioRecord != null) { "call prepare() first" }
        this.startEpochNs = startEpochNs
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
                // Keep looping. Use small delay so we don't busy-spin between
                // 20ms PCM reads.
                handler.postDelayed(::pumpLoop, 5)
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
        val byteCount = framesRead * 2
        if (byteCount > 0) {
            // Software gain. Rokid HAL applies aggressive AGC + NS to MIC source
            // so spoken-voice peaks land around -19dBFS and means around -59dBFS
            // — quiet but audible. We boost ×4 (~+12dB) with hard-clip to keep
            // Whisper happy without making clipping audibly obvious. Override
            // by setting [softwareGain] to 1.0f to disable.
            for (i in 0 until framesRead) {
                val boosted = (pcmTempBuf[i].toInt() * softwareGain).toInt()
                    .coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt())
                inBuf.put((boosted and 0xFF).toByte())
                inBuf.put(((boosted shr 8) and 0xFF).toByte())
            }
        }
        val ptsUs = computePtsUs()
        totalFramesRead += framesRead
        val flags = if (eos) MediaCodec.BUFFER_FLAG_END_OF_STREAM else 0
        c.queueInputBuffer(inIdx, 0, byteCount, ptsUs, flags)
    }

    private fun computePtsUs(): Long {
        // PTS = nanos since start, in micros. We anchor to startEpochNs so
        // audio + video share the same monotonic timeline (camera SurfaceTexture
        // timestamp is also System.nanoTime-based on Android).
        return (System.nanoTime() - startEpochNs) / 1_000L
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

    companion object {
        private const val TAG = "ShiftMind.AEnc"
        private const val INPUT_DEQUEUE_TIMEOUT_US = 10_000L
        private const val DEQUEUE_TIMEOUT_US = 10_000L
    }
}
