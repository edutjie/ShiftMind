package com.shiftmind.glasses.recording

import android.graphics.SurfaceTexture
import android.opengl.GLES11Ext
import android.opengl.GLES20
import android.opengl.Matrix
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.Surface
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer

/**
 * Renders camera frames into a MediaCodec input Surface.
 *
 * HUD is recorded separately by Rokid's screenstream service and combined
 * with the cam MP4 after pulling. This class records only the camera side.
 *
 * Threading model:
 *   - Owns its own [HandlerThread] (`shiftmind-gl`). All EGL calls happen
 *     here.
 *   - Camera SurfaceTexture is created with that handler, so its
 *     `OnFrameAvailableListener` fires on the same thread — no cross-thread
 *     GL context juggling needed.
 *   - Each fired frame triggers the full render pipeline (camera → encoder
 *     surface) in lockstep with the camera's effective FPS.
 *
 * Frame pipeline per available camera frame:
 *   1. `surfaceTexture.updateTexImage()` pulls the latest camera frame into
 *      the external OES texture.
 *   2. `surfaceTexture.getTransformMatrix()` gives us the texture transform
 *      the camera framework wants (accounts for sensor orientation, mirror,
 *      etc.). We combine that with our own rotation correction so the final
 *      encoded video is upright.
 *   3. Draw camera quad (external sampler) full-frame.
 *   4. Set PTS from camera timestamp, swap buffers → MediaCodec gets the
 *      camera frame.
 *
 * Recording uses the same raw `640x480` camera stream as the live preview.
 * The encoder surface is `480x640`; the SurfaceTexture transform supplies the
 * device-specific mapping, and [extraRotationDeg] stays 0 on current hardware.
 */
class HudCompositor(
    private val encoderSurface: Surface,
    private val outputWidth: Int,
    private val outputHeight: Int,
    /**
     * Extra rotation (degrees, multiples of 90) applied AFTER the camera
     * SurfaceTexture transform. Defaults to 0 — the SurfaceTexture matrix
     * usually handles sensor orientation. Override if it doesn't.
     */
    private val extraRotationDeg: Int = 0,
    /**
     * Camera-side buffer dimensions. MUST match what the Camera2 session is
     * actually streaming. Keep this aligned with [CameraPreviewStreamer]'s
     * `640x480` preview stream, not the portrait encoder size.
     */
    private val cameraBufferWidth: Int = 640,
    private val cameraBufferHeight: Int = 480,
) {

    private val thread = HandlerThread("shiftmind-gl")
    private lateinit var handler: Handler

    /** Shared CLOCK_MONOTONIC origin used by both camera and microphone. */
    @Volatile private var recordingStartNs: Long = 0L
    fun setRecordingStartNs(value: Long) { recordingStartNs = value }

    private var egl: EglCore? = null
    private var windowSurface: WindowSurface? = null
    private var cameraProgram: TexturedQuadProgram? = null

    private var cameraTextureId = 0
    private var surfaceTexture: SurfaceTexture? = null

    private val cameraTexMatrix = FloatArray(16)
    private val rotationMatrix = FloatArray(16)
    private val finalCameraMatrix = FloatArray(16)

    // Vertex buffer: full-viewport quad as TRIANGLE_STRIP (4 vertices).
    // MUST be a DIRECT ByteBuffer — GLES20.glVertexAttribPointer rejects
    // heap-backed FloatBuffers with `glVertexAttribPointerBounds` failure.
    private val vertexBuffer: FloatBuffer = ByteBuffer
        .allocateDirect(8 * java.lang.Float.SIZE / 8)
        .order(ByteOrder.nativeOrder())
        .asFloatBuffer()
        .also {
            it.put(floatArrayOf(
                -1f, -1f,    // bottom-left
                 1f, -1f,    // bottom-right
                -1f,  1f,    // top-left
                 1f,  1f,    // top-right
            ))
            it.position(0)
        }
    private val texCoordBuffer: FloatBuffer = ByteBuffer
        .allocateDirect(8 * java.lang.Float.SIZE / 8)
        .order(ByteOrder.nativeOrder())
        .asFloatBuffer()
        .also {
            it.put(floatArrayOf(
                0f, 0f,
                1f, 0f,
                0f, 1f,
                1f, 1f,
            ))
            it.position(0)
        }

    private var firstFrameLogged = false

    /** Open the GL pipeline. Blocks until the surface texture is ready to wire up. */
    fun start(onCameraSurfaceTextureReady: (SurfaceTexture) -> Unit) {
        thread.start()
        handler = Handler(thread.looper)
        handler.post {
            try {
                initGl()
                onCameraSurfaceTextureReady(surfaceTexture!!)
            } catch (t: Throwable) {
                Log.e(TAG, "GL init failed", t)
            }
        }
    }

    private fun initGl() {
        egl = EglCore()
        windowSurface = WindowSurface(egl!!, encoderSurface).also { it.makeCurrent() }

        cameraProgram = TexturedQuadProgram(TexturedQuadProgram.SamplerType.EXTERNAL_OES)

        cameraTextureId = createOesTexture()

        surfaceTexture = SurfaceTexture(cameraTextureId).apply {
            // Match preview capture dimensions exactly; do not request a
            // portrait stream from Camera2.
            setDefaultBufferSize(cameraBufferWidth, cameraBufferHeight)
            setOnFrameAvailableListener({ st -> handler.post { drawFrame(st) } }, handler)
        }

        GLES20.glViewport(0, 0, outputWidth, outputHeight)
        GLES20.glDisable(GLES20.GL_DEPTH_TEST)
        GLES20.glDisable(GLES20.GL_BLEND)

        // Precompute the static rotation matrix that the camera texture matrix
        // is multiplied by. Rotation acts on texture coords centered at (0.5,0.5),
        // so we translate→rotate→translate-back.
        Matrix.setIdentityM(rotationMatrix, 0)
        if (extraRotationDeg != 0) {
            Matrix.translateM(rotationMatrix, 0, 0.5f, 0.5f, 0f)
            Matrix.rotateM(rotationMatrix, 0, extraRotationDeg.toFloat(), 0f, 0f, 1f)
            Matrix.translateM(rotationMatrix, 0, -0.5f, -0.5f, 0f)
        }

        Log.i(TAG, "GL initialized; encoder surface ${outputWidth}x${outputHeight}, extra rotation ${extraRotationDeg}°")
    }

    private fun drawFrame(st: SurfaceTexture) {
        try {
            st.updateTexImage()
            st.getTransformMatrix(cameraTexMatrix)
            // finalCameraMatrix = rotationMatrix * cameraTexMatrix
            Matrix.multiplyMM(finalCameraMatrix, 0, rotationMatrix, 0, cameraTexMatrix, 0)

            GLES20.glClearColor(0f, 0f, 0f, 1f)
            GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT)

            // Camera (external OES) — full viewport. HUD is captured separately
            // by Rokid screenstream and merged after pull.
            cameraProgram!!.draw(
                cameraTextureId,
                GLES11Ext.GL_TEXTURE_EXTERNAL_OES,
                finalCameraMatrix,
                vertexBuffer,
                texCoordBuffer,
            )

            // PTS for encoder: camera timestamp rebased to ptsAnchor so
            // audio + video share the same monotonic-zero timeline. Without
            // this, video PTS is raw uptime-nanos and audio PTS is delta-nanos,
            // producing wildly wrong MP4 duration + audio/video desync.
            // Do not use SurfaceTexture.timestamp here. Rokid's camera producer
            // has shown both a different epoch and a different effective rate,
            // which makes players interpret A/V sync differently. Timestamp the
            // delivered frame on the same CLOCK_MONOTONIC timeline as audio.
            val pts = (System.nanoTime() - recordingStartNs).coerceAtLeast(0L)
            windowSurface!!.setPresentationTime(pts)
            if (!windowSurface!!.swap()) {
                Log.w(TAG, "eglSwapBuffers failed")
            }

            if (!firstFrameLogged) {
                firstFrameLogged = true
                Log.i(TAG, "first encoded frame submitted (raw=${st.timestamp}ns anchored=${pts}ns)")
            }
            checkGl("drawFrame")
        } catch (t: Throwable) {
            Log.e(TAG, "drawFrame threw", t)
        }
    }

    fun release() {
        handler.post {
            surfaceTexture?.release()
            surfaceTexture = null
            cameraProgram?.release()
            if (cameraTextureId != 0) {
                GLES20.glDeleteTextures(1, intArrayOf(cameraTextureId), 0)
            }
            windowSurface?.release()
            egl?.release()
        }
        thread.quitSafely()
        try {
            thread.join(500)
        } catch (_: InterruptedException) {}
    }

    companion object {
        private const val TAG = "ShiftMind.GL"
    }
}
