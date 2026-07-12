package com.shiftmind.glasses.recording

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.SurfaceTexture
import android.util.Log
import android.view.Surface
import androidx.core.content.ContextCompat
import com.shiftmind.glasses.camera.CameraPreviewStreamer
import com.shiftmind.glasses.hud.HudStateHolder

/**
 * Orchestrates an AR recording session (video + optional audio).
 *
 * Pipeline (start):
 *   1. [RecordingMuxer.open]                          → MediaStore entry + MediaMuxer
 *   2. [VideoEncoder.prepare]                         → MediaCodec.createInputSurface()
 *   3. (if audio) [AudioEncoder.prepare]              → shared PCM + AAC codec
 *   4. [HudCompositor] wraps encoder surface          → EGL/GL on render thread
 *   5. HudCompositor callback delivers camera-side [SurfaceTexture]
 *   6. Wrap that SurfaceTexture in a [Surface]        → camera output target
 *   7. Streamer.attachRecordingTarget(surface)        → multi-output session
 *   8. [VideoEncoder.start]                           → format-change registers track on muxer
 *   9. (if audio) [AudioEncoder.start]                → format-change registers second track,
 *                                                       muxer.start() fires once both ready
 *  10. HudState.recordingActive = true                → REC dot shows
 *
 * Pipeline (stop):
 *   1. Clear HudState.recordingActive
 *   2. Streamer.detachRecordingTarget()               → camera back to ImageReader-only
 *   3. [HudCompositor.release]                        → GL teardown
 *   4. [VideoEncoder.stop]                            → EOS, drain, release codec
 *   5. (if audio) [AudioEncoder.stop]                 → unsubscribe, EOS, drain codec
 *   6. [RecordingMuxer.finalize]                      → mux.stop, release, mark IS_PENDING=0
 *   7. Release camera Surface
 */
class ArRecorder(
    private val context: Context,
    private val streamer: CameraPreviewStreamer,
    private val micCapture: SharedMicCapture,
    private val videoWidth: Int = 480,
    private val videoHeight: Int = 640,
    private val fps: Int = 24,
    private val bitrate: Int = 2_000_000,
    /**
     * Extra rotation applied AFTER the camera SurfaceTexture transform matrix.
     * Rokid preview is raw 640x480 rotated counter-clockwise on the server;
     * rotate texture coords clockwise into a 480x640 encoder to match it.
     */
    private val extraRotationDeg: Int = 0,
    /**
     * Whether to record audio. If true but RECORD_AUDIO is not granted, audio
     * is silently disabled for this session (video-only fallback).
     */
    private val recordAudio: Boolean = true,
) {

    @Volatile private var recording = false
    val isRecording: Boolean get() = recording

    private var muxer: RecordingMuxer? = null
    private var videoEncoder: VideoEncoder? = null
    private var audioEncoder: AudioEncoder? = null
    private var compositor: HudCompositor? = null
    private var cameraSurface: Surface? = null
    private var cameraSurfaceTexture: SurfaceTexture? = null

    /** Toggle: returns true if recording is now active, false if just stopped. */
    fun toggle(timestamp: String? = null): Boolean =
        if (recording) { stop(); false } else { start(timestamp); true }

    /**
     * Start recording. If [timestamp] is non-null it's used as the shared
     * session stem so this `..._cam.mp4` pairs with a parallel Rokid
     * screenstream HUD MP4. Otherwise a fresh timestamp is generated.
     */
    fun start(timestamp: String? = null) {
        if (recording) return
        val audioGranted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.RECORD_AUDIO,
        ) == PackageManager.PERMISSION_GRANTED
        val withAudio = recordAudio && audioGranted
        if (recordAudio && !audioGranted) {
            Log.w(TAG, "RECORD_AUDIO not granted — recording video only this session")
        }
        Log.i(TAG, "starting AR recording: ${videoWidth}x${videoHeight} @ ${fps}fps, audio=$withAudio, stem=${timestamp ?: "(auto)"}")

        try {
            val mux = (if (timestamp != null)
                RecordingMuxer(context, timestamp = timestamp, suffix = "_cam")
            else RecordingMuxer(context, suffix = "_cam"))
                .also {
                    it.setAudioEnabled(withAudio)
                    it.open()
                }
            muxer = mux

            val venc = VideoEncoder(mux, videoWidth, videoHeight, fps, bitrate).also { it.prepare() }
            videoEncoder = venc

            val aenc = if (withAudio) AudioEncoder(mux, micCapture).also { it.prepare() } else null
            audioEncoder = aenc

            val comp = HudCompositor(
                encoderSurface = venc.inputSurface,
                outputWidth = videoWidth,
                outputHeight = videoHeight,
                extraRotationDeg = extraRotationDeg,
                cameraBufferWidth = CAMERA_BUFFER_WIDTH,
                cameraBufferHeight = CAMERA_BUFFER_HEIGHT,
            )
            compositor = comp

            comp.start { surfaceTexture ->
                cameraSurfaceTexture = surfaceTexture
                val surface = Surface(surfaceTexture)
                cameraSurface = surface
                streamer.attachRecordingTarget(surface)
                // One CLOCK_MONOTONIC origin for both delivered camera frames
                // and captured PCM blocks keeps the MP4 timeline player-neutral.
                val anchorNs = System.nanoTime()
                comp.setRecordingStartNs(anchorNs)
                venc.start()
                aenc?.start(anchorNs)
                recording = true
                HudStateHolder.mutate { it.copy(recordingActive = true) }
                Log.i(TAG, "AR recording active (audio=${aenc != null}); output: ${mux.outputPath}")
            }
        } catch (t: Throwable) {
            Log.e(TAG, "failed to start AR recording", t)
            cleanupAfterFailure()
        }
    }

    fun stop() {
        if (!recording) return
        Log.i(TAG, "stopping AR recording")
        recording = false
        HudStateHolder.mutate { it.copy(recordingActive = false) }

        // 1. Detach camera target — no new frames into GL pipeline.
        try { streamer.detachRecordingTarget() } catch (t: Throwable) {
            Log.w(TAG, "detachRecordingTarget threw", t)
        }
        // 2. Release compositor — GL thread quitSafely + EGL teardown.
        try { compositor?.release() } catch (t: Throwable) {
            Log.w(TAG, "compositor.release threw", t)
        }
        compositor = null
        // 3. EOS + drain video encoder.
        try { videoEncoder?.stop() } catch (t: Throwable) {
            Log.w(TAG, "videoEncoder.stop threw", t)
        }
        videoEncoder = null
        // 4. EOS + drain audio encoder.
        try { audioEncoder?.stop() } catch (t: Throwable) {
            Log.w(TAG, "audioEncoder.stop threw", t)
        }
        audioEncoder = null
        // 5. Finalize muxer — must happen AFTER both encoders are fully drained.
        try { muxer?.finalize() } catch (t: Throwable) {
            Log.w(TAG, "muxer.finalize threw", t)
        }
        muxer = null
        // 6. Release camera surface.
        try { cameraSurface?.release() } catch (_: Throwable) {}
        cameraSurface = null
        cameraSurfaceTexture = null
        Log.i(TAG, "AR recording stopped")
    }

    private fun cleanupAfterFailure() {
        recording = false
        HudStateHolder.mutate { it.copy(recordingActive = false) }
        compositor?.runCatching { release() }
        compositor = null
        videoEncoder?.runCatching { stop() }
        videoEncoder = null
        audioEncoder?.runCatching { stop() }
        audioEncoder = null
        muxer?.runCatching { finalize() }
        muxer = null
        cameraSurface?.runCatching { release() }
        cameraSurface = null
    }

    companion object {
        private const val TAG = "ShiftMind.ArRec"
        private const val CAMERA_BUFFER_WIDTH = 640
        private const val CAMERA_BUFFER_HEIGHT = 480
    }
}
