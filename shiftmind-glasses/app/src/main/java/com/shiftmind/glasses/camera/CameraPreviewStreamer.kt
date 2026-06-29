package com.shiftmind.glasses.camera

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.Image
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.Surface
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import com.shiftmind.glasses.BuildConfig
import com.shiftmind.glasses.hud.HudStateHolder
import com.shiftmind.glasses.session.CustomerSessionHolder
import com.shiftmind.glasses.net.PreviewClient
import java.io.ByteArrayOutputStream
import java.util.concurrent.atomic.AtomicLong

/**
 * Camera2 → JPEG → POST /api/preview-frame.
 *
 * Why Camera2 instead of CameraX:
 *   The Rokid legacy camera HAL reports `LENS_FACING` as **null** through the
 *   CameraX wrapper, so `CameraSelector.DEFAULT_BACK_CAMERA` matches nothing
 *   and the analyzer never gets frames. Raw Camera2 sidesteps the validator
 *   entirely — we just open the first camera id reported by CameraManager
 *   (id "0" on Rokid RG-glasses) and stream from it. See CLAUDE.md.
 *
 * Pipeline:
 *   Camera2 → ImageReader (YUV_420_888, 640x480, maxImages=2)
 *     → onImageAvailable → Y/V/U → NV21 → YuvImage.compressToJpeg
 *     → PreviewClient.postFrame (multipart over OkHttp)
 *
 * Self-healing: if the camera service rejects open/createSession (usually
 * because another client currently owns the sensor), we tear down and
 * schedule a retry. Up to MAX_RETRIES with exponential backoff. Cancellable
 * via stop().
 *
 * Lifecycle: `start(LifecycleOwner)` attaches a DefaultLifecycleObserver that
 * calls `stop()` on ON_DESTROY. Callers may also call stop() directly.
 */
class CameraPreviewStreamer(
    private val context: Context,
    private val previewClient: PreviewClient = PreviewClient(),
    private val targetFps: Int = BuildConfig.PREVIEW_FPS,
    private val jpegQuality: Int = 70,
    private val width: Int = 640,
    private val height: Int = 480,
) {

    private val frameIntervalNs: Long = (1_000_000_000.0 / targetFps).toLong()
    private val lastSentNs = AtomicLong(0L)

    @Volatile private var wantRunning = false   // user intent — drives auto-retry
    @Volatile private var open = false          // current open attempt in flight or active

    /** True while camera is open and delivering frames. False after retry exhaustion. */
    val isActive: Boolean get() = wantRunning && open

    /**
     * Optional second output Surface (the camera-side input of the recording
     * GL pipeline). Setting/clearing while running triggers a session
     * recreate (Camera2 doesn't allow modifying outputs in-place pre-API 28
     * `updateOutputConfiguration` API, which the Rokid HAL doesn't expose).
     */
    @Volatile private var recordingTarget: Surface? = null

    private var cameraThread: HandlerThread? = null
    private var cameraHandler: Handler? = null
    private var imageReader: ImageReader? = null
    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var lifecycleObserver: DefaultLifecycleObserver? = null
    private var ownerRef: LifecycleOwner? = null
    private var faceGateDetector: FaceGateDetector? = null
    @Volatile private var latestPreviewJpeg: ByteArray? = null
    @Volatile private var latestFaceCropJpeg: ByteArray? = null
    @Volatile private var latestFaceBoxJson: String? = null

    private var retryAttempt = 0
    private var pendingRetry: Runnable? = null

    fun start(owner: LifecycleOwner) {
        if (wantRunning && open) return
        wantRunning = true
        retryAttempt = 0

        // Observe lifecycle so we tear down on ON_DESTROY. Idempotent — if we
        // were already attached to this owner, removeObserver is a no-op.
        if (ownerRef !== owner) {
            lifecycleObserver?.let { ownerRef?.lifecycle?.removeObserver(it) }
            val obs = object : DefaultLifecycleObserver {
                override fun onDestroy(o: LifecycleOwner) = stop()
            }
            ownerRef = owner
            lifecycleObserver = obs
            owner.lifecycle.addObserver(obs)
        }

        attemptOpen()
    }

    @SuppressLint("MissingPermission")
    private fun attemptOpen() {
        if (!wantRunning || open) return
        val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val cameraId = pickCameraId(manager)
        if (cameraId == null) {
            Log.e(TAG, "no camera id available; aborting")
            return
        }
        Log.i(TAG, "opening Camera2 id=$cameraId (attempt ${retryAttempt + 1}); streaming ${targetFps}fps -> ${BuildConfig.SERVER_URL}/api/preview-frame")
        open = true

        cameraThread = HandlerThread("shiftmind-camera").apply { start() }
        val handler = Handler(cameraThread!!.looper).also { cameraHandler = it }

        val reader = ImageReader.newInstance(width, height, ImageFormat.YUV_420_888, 2)
        reader.setOnImageAvailableListener({ r -> onImageAvailable(r) }, handler)
        imageReader = reader
        faceGateDetector = FaceGateDetector(width, height, onResult = ::onFaceGateResult)

        try {
            manager.openCamera(cameraId, cameraStateCallback, handler)
        } catch (t: Throwable) {
            Log.e(TAG, "openCamera failed", t)
            handleTransientFailure()
        }
    }

    /** Tear down current open attempt and schedule another if user still wants us running. */
    private fun handleTransientFailure() {
        teardownCameraState()
        if (!wantRunning) return
        if (retryAttempt >= MAX_RETRIES) {
            Log.w(TAG, "giving up after ${retryAttempt + 1} attempts; call start() to resume")
            wantRunning = false
            return
        }
        val delay = RETRY_DELAYS_MS[retryAttempt.coerceAtMost(RETRY_DELAYS_MS.lastIndex)]
        retryAttempt++
        Log.i(TAG, "scheduling retry in ${delay}ms (attempt ${retryAttempt + 1}/${MAX_RETRIES + 1})")
        val r = Runnable {
            pendingRetry = null
            attemptOpen()
        }
        pendingRetry = r
        retryHandler.postDelayed(r, delay)
    }

    /**
     * Pick a camera id. We prefer id "0" if present; otherwise the first id
     * in the list. We deliberately do NOT filter by lens facing — that's the
     * exact metadata field the Rokid HAL leaves null.
     */
    private fun pickCameraId(manager: CameraManager): String? = try {
        val ids = manager.cameraIdList
        if (ids.isEmpty()) null
        else if ("0" in ids) "0"
        else ids.first()
    } catch (t: Throwable) {
        Log.e(TAG, "cameraIdList failed", t)
        null
    }

    private val cameraStateCallback = object : CameraDevice.StateCallback() {
        override fun onOpened(device: CameraDevice) {
            cameraDevice = device
            createSession(device)
        }

        override fun onDisconnected(device: CameraDevice) {
            // Another client grabbed the sensor. Tear down and let the
            // foreground activity retry if the user still wants preview.
            Log.w(TAG, "camera disconnected — releasing for the new owner")
            handleTransientFailure()
        }

        override fun onError(device: CameraDevice, error: Int) {
            Log.e(TAG, "camera onError code=$error")
            handleTransientFailure()
        }
    }

    private fun createSession(device: CameraDevice) {
        val reader = imageReader ?: return
        val readerSurface = reader.surface
        // Session outputs: ImageReader always; optional recording target when set.
        // Each output of a session needs an addTarget on the CaptureRequest below.
        val recSurface = recordingTarget
        val outputs = if (recSurface != null) listOf(readerSurface, recSurface) else listOf(readerSurface)
        try {
            device.createCaptureSession(
                outputs,
                object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(session: CameraCaptureSession) {
                        captureSession = session
                        try {
                            val req = device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                                addTarget(readerSurface)
                                recSurface?.let { addTarget(it) }
                                set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
                                set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                            }.build()
                            session.setRepeatingRequest(req, null, cameraHandler)
                            retryAttempt = 0    // successful open clears the retry counter
                            Log.i(TAG, "capture session configured; outputs=${outputs.size}, repeating preview active")
                        } catch (t: Throwable) {
                            Log.e(TAG, "setRepeatingRequest failed", t)
                            handleTransientFailure()
                        }
                    }

                    override fun onConfigureFailed(session: CameraCaptureSession) {
                        Log.e(TAG, "capture session configure failed")
                        handleTransientFailure()
                    }
                },
                cameraHandler,
            )
        } catch (t: Throwable) {
            Log.e(TAG, "createCaptureSession failed", t)
            handleTransientFailure()
        }
    }

    /**
     * Attach a recording-side Surface to the camera session. Triggers a session
     * recreate (~100-200ms). Camera2 doesn't allow modifying session outputs in
     * place pre-API 28 `updateOutputConfiguration`, which the Rokid HAL doesn't
     * expose, so recreating is the only path.
     *
     * Must be called on any thread; we hop to the cameraHandler internally.
     */
    fun attachRecordingTarget(surface: Surface) {
        Log.i(TAG, "attachRecordingTarget(surface=$surface, valid=${surface.isValid})")
        recordingTarget = surface
        recreateSession("attach")
    }

    /** Detach the recording surface and recreate session with ImageReader only. */
    fun detachRecordingTarget() {
        Log.i(TAG, "detachRecordingTarget()")
        recordingTarget = null
        recreateSession("detach")
    }

    private fun recreateSession(reason: String) {
        val handler = cameraHandler
        val device = cameraDevice
        Log.i(TAG, "recreateSession($reason): handler=${handler != null}, device=${device != null}, recTarget=${recordingTarget != null}")
        if (handler == null || device == null) return  // not running — next start() will pick up the new target list
        handler.post {
            Log.i(TAG, "recreateSession($reason) dispatched: closing prev session, recreating")
            try { captureSession?.close() } catch (t: Throwable) {
                Log.w(TAG, "captureSession.close threw", t)
            }
            captureSession = null
            createSession(device)
        }
    }

    private fun onImageAvailable(reader: ImageReader) {
        val image: Image? = try {
            reader.acquireLatestImage()
        } catch (t: Throwable) {
            Log.w(TAG, "acquireLatestImage threw", t)
            null
        }
        if (image == null) return
        try {
            val now = System.nanoTime()
            val prev = lastSentNs.get()
            if (now - prev < frameIntervalNs) return
            if (!lastSentNs.compareAndSet(prev, now)) return
            val nv21 = image.toNv21() ?: return
            val jpeg = nv21.toJpeg(Rect(0, 0, width, height), jpegQuality) ?: return
            latestPreviewJpeg = jpeg
            previewClient.postFrame(
                jpeg = jpeg,
                faceCropJpeg = latestFaceCropJpeg,
                faceBox = latestFaceBoxJson,
            )
            faceGateDetector?.process(nv21)
        } catch (t: Throwable) {
            Log.w(TAG, "frame handler error", t)
        } finally {
            image.close()
        }
    }

    fun stop() {
        wantRunning = false
        pendingRetry?.let { retryHandler.removeCallbacks(it) }
        pendingRetry = null
        retryAttempt = 0
        teardownCameraState()
        lifecycleObserver?.let { obs -> ownerRef?.lifecycle?.removeObserver(obs) }
        lifecycleObserver = null
        ownerRef = null
    }

    /** Close camera+session+reader+thread but do NOT touch wantRunning/retry counters. */
    private fun teardownCameraState() {
        open = false
        try { captureSession?.close() } catch (_: Throwable) {}
        captureSession = null
        try { cameraDevice?.close() } catch (_: Throwable) {}
        cameraDevice = null
        try { imageReader?.close() } catch (_: Throwable) {}
        imageReader = null
        try { faceGateDetector?.close() } catch (_: Throwable) {}
        faceGateDetector = null
        latestPreviewJpeg = null
        latestFaceCropJpeg = null
        latestFaceBoxJson = null
        cameraThread?.quitSafely()
        cameraThread = null
        cameraHandler = null
    }

    /**
     * YUV_420_888 → NV21. Good enough for preview-quality frames even when
     * row/pixel strides aren't ideal on this HAL.
     */
    private fun Image.toNv21(): ByteArray? {
        if (format != ImageFormat.YUV_420_888) {
            Log.w(TAG, "unexpected image format $format")
            return null
        }
        val yPlane = planes[0]
        val uPlane = planes[1]
        val vPlane = planes[2]
        val yBuf = yPlane.buffer
        val uBuf = uPlane.buffer
        val vBuf = vPlane.buffer

        val ySize = yBuf.remaining()
        val uSize = uBuf.remaining()
        val vSize = vBuf.remaining()

        val nv21 = ByteArray(ySize + uSize + vSize)
        yBuf.get(nv21, 0, ySize)
        vBuf.get(nv21, ySize, vSize)
        uBuf.get(nv21, ySize + vSize, uSize)

        return nv21
    }

    private fun ByteArray.toJpeg(rect: Rect, quality: Int): ByteArray? {
        val yuv = YuvImage(this, ImageFormat.NV21, width, height, null)
        val out = ByteArrayOutputStream(64 * 1024)
        return if (yuv.compressToJpeg(rect, quality, out)) {
            out.toByteArray()
        } else {
            null
        }
    }

    private fun onFaceGateResult(result: FaceGateResult) {
        if (CustomerSessionHolder.current != null) return
        val faceBox = result.faceBox
        if (result.faceInCursor && faceBox != null) {
            latestFaceCropJpeg = result.faceCropJpeg
            latestFaceBoxJson = faceBox.toJson()
            HudStateHolder.mutate {
                it.copy(
                    statusLine = "recognizing",
                    customerCard = null,
                )
            }
        } else {
            latestFaceCropJpeg = null
            latestFaceBoxJson = null
            HudStateHolder.mutate {
                it.copy(
                    statusLine = when {
                        result.faceNotFrontal -> "face the camera"
                        result.faceDetected -> "align face in box"
                        else -> "awaiting customer"
                    },
                    customerCard = null,
                )
            }
        }
    }

    private fun Rect.toJson(): String =
        """{"left":$left,"top":$top,"right":$right,"bottom":$bottom}"""

    companion object {
        private const val TAG = "ShiftMind.Camera"
        // Retry plan: 500ms, 1s, 2s, 3s, 5s — covers short camera-service
        // handoff windows after another client releases the sensor.
        private val RETRY_DELAYS_MS = longArrayOf(500, 1000, 2000, 3000, 5000)
        private const val MAX_RETRIES = 5
        // Use the main looper for the timer so we don't tie up our camera thread.
        private val retryHandler = Handler(android.os.Looper.getMainLooper())
    }
}
