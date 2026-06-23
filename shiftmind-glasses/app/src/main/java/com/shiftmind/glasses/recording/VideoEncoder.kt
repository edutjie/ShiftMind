package com.shiftmind.glasses.recording

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.HandlerThread
import android.util.Log
import android.view.Surface

/**
 * MediaCodec H.264 video encoder feeding a shared [RecordingMuxer].
 *
 * Muxer ownership lives in [RecordingMuxer] (so audio can share the same
 * MP4 container). This class:
 *   1. Owns the codec + its input Surface (the [HudCompositor] draws
 *      composited camera+HUD frames into it).
 *   2. Drains output buffers on its own [HandlerThread] (`shiftmind-venc`).
 *   3. On format-change, calls [RecordingMuxer.addVideoTrack] to register
 *      itself; further sample writes go through [RecordingMuxer.writeVideo].
 *   4. On [stop], signals EOS to the codec, drains to the EOS marker, then
 *      releases — does NOT touch the muxer (orchestrator finalizes after
 *      both encoders complete).
 */
class VideoEncoder(
    private val muxer: RecordingMuxer,
    private val width: Int = 480,
    private val height: Int = 640,
    private val fps: Int = 24,
    private val bitrate: Int = 2_000_000,
) {

    /** Input surface for the GL compositor to render into. Valid after [prepare]. */
    lateinit var inputSurface: Surface
        private set

    private var encoder: MediaCodec? = null
    private var trackIndex: Int = -1

    private val drainThread = HandlerThread("shiftmind-venc")
    @Volatile private var endOfStreamRequested = false
    @Volatile private var draining = false

    fun prepare() {
        val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2)
            // Baseline profile — best playback compatibility for our small MP4s.
            setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline)
        }
        encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC).apply {
            configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            inputSurface = createInputSurface()
        }
        Log.i(TAG, "video encoder prepared: ${width}x${height} @ ${fps}fps, ${bitrate / 1000}kbps")
    }

    fun start() {
        check(encoder != null) { "call prepare() first" }
        encoder!!.start()
        drainThread.start()
        draining = true
        drainThread.threadHandler.post { drainLoop() }
    }

    /** Signal EOS and block until drain finishes. Muxer untouched. */
    fun stop(timeoutMs: Long = 3000) {
        endOfStreamRequested = true
        try {
            encoder?.signalEndOfInputStream()
        } catch (t: Throwable) {
            Log.w(TAG, "signalEndOfInputStream failed", t)
        }
        val deadline = System.nanoTime() + timeoutMs * 1_000_000L
        while (draining && System.nanoTime() < deadline) {
            try { Thread.sleep(20) } catch (_: InterruptedException) { break }
        }
        if (draining) Log.w(TAG, "video drain did not finish within ${timeoutMs}ms — forcing teardown")
        teardown()
    }

    private fun drainLoop() {
        val info = MediaCodec.BufferInfo()
        val enc = encoder!!
        try {
            while (draining) {
                val outIndex = enc.dequeueOutputBuffer(info, DEQUEUE_TIMEOUT_US)
                when {
                    outIndex == MediaCodec.INFO_TRY_AGAIN_LATER -> {
                        if (!endOfStreamRequested) {
                            try { Thread.sleep(2) } catch (_: InterruptedException) { break }
                        }
                    }
                    outIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                        check(trackIndex == -1) { "format changed twice" }
                        trackIndex = muxer.addVideoTrack(enc.outputFormat)
                    }
                    outIndex >= 0 -> {
                        val buf = enc.getOutputBuffer(outIndex)
                            ?: error("getOutputBuffer($outIndex) returned null")
                        if ((info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) {
                            info.size = 0
                        }
                        if (info.size > 0 && muxer.isStarted) {
                            buf.position(info.offset)
                            buf.limit(info.offset + info.size)
                            muxer.writeVideo(buf, info)
                        }
                        enc.releaseOutputBuffer(outIndex, false)
                        if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
                            Log.i(TAG, "video encoder reported EOS")
                            break
                        }
                    }
                    else -> Log.w(TAG, "unexpected dequeueOutputBuffer: $outIndex")
                }
            }
        } catch (t: Throwable) {
            Log.e(TAG, "video drain loop crashed", t)
        } finally {
            draining = false
        }
    }

    private fun teardown() {
        try { encoder?.stop() } catch (_: Throwable) {}
        try { encoder?.release() } catch (_: Throwable) {}
        encoder = null
        try {
            drainThread.quitSafely()
            drainThread.join(500)
        } catch (_: InterruptedException) {}
        Log.i(TAG, "video encoder torn down")
    }

    companion object {
        private const val TAG = "ShiftMind.VEnc"
        private const val DEQUEUE_TIMEOUT_US = 10_000L

        private val HandlerThread.threadHandler: android.os.Handler
            get() = android.os.Handler(looper)
    }
}
