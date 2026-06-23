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
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import com.shiftmind.glasses.BuildConfig
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
 *   (id "0" on Rokid RG-glasses) and stream from it. See CLAUDE.md:
 *   "If a future firmware update breaks [CameraX], fall back to raw Camera2."
 *
 * Pipeline:
 *   Camera2 → ImageReader (YUV_420_888, 640x480, maxImages=2)
 *     → onImageAvailable → Y/V/U → NV21 → YuvImage.compressToJpeg
 *     → PreviewClient.postFrame (multipart over OkHttp)
 *
 * Throttling: target FPS from BuildConfig.PREVIEW_FPS. Frames arriving faster
 * than the budget are dropped — we still call image.close() so the buffer
 * pool doesn't starve.
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

    @Volatile private var started = false

    private var cameraThread: HandlerThread? = null
    private var cameraHandler: Handler? = null
    private var imageReader: ImageReader? = null
    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var lifecycleObserver: DefaultLifecycleObserver? = null
    private var ownerRef: LifecycleOwner? = null

    @SuppressLint("MissingPermission")
    fun start(owner: LifecycleOwner) {
        if (started) return
        started = true

        val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val cameraId = pickCameraId(manager)
        if (cameraId == null) {
            Log.e(TAG, "no camera id available; aborting")
            started = false
            return
        }
        Log.i(TAG, "opening Camera2 id=$cameraId; streaming ${targetFps}fps -> ${BuildConfig.SERVER_URL}/api/preview-frame")

        cameraThread = HandlerThread("shiftmind-camera").apply { start() }
        val handler = Handler(cameraThread!!.looper).also { cameraHandler = it }

        // ImageReader receives YUV frames. maxImages=2 — one in flight, one being
        // produced. acquireLatestImage drops the older if we can't keep up.
        val reader = ImageReader.newInstance(width, height, ImageFormat.YUV_420_888, 2)
        reader.setOnImageAvailableListener({ r -> onImageAvailable(r) }, handler)
        imageReader = reader

        // Observe lifecycle so we tear down on ON_DESTROY.
        val obs = object : DefaultLifecycleObserver {
            override fun onDestroy(o: LifecycleOwner) = stop()
        }
        ownerRef = owner
        lifecycleObserver = obs
        owner.lifecycle.addObserver(obs)

        try {
            manager.openCamera(cameraId, cameraStateCallback, handler)
        } catch (t: Throwable) {
            Log.e(TAG, "openCamera failed", t)
            stopInternal()
        }
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
            Log.w(TAG, "camera disconnected")
            device.close()
            cameraDevice = null
        }

        override fun onError(device: CameraDevice, error: Int) {
            Log.e(TAG, "camera onError code=$error")
            device.close()
            cameraDevice = null
            stopInternal()
        }
    }

    private fun createSession(device: CameraDevice) {
        val reader = imageReader ?: return
        val target = reader.surface
        try {
            device.createCaptureSession(
                listOf(target),
                object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(session: CameraCaptureSession) {
                        captureSession = session
                        try {
                            val req = device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                                addTarget(target)
                                // Auto everything — preview quality is enough for face detection.
                                set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
                                set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                            }.build()
                            session.setRepeatingRequest(req, null, cameraHandler)
                            Log.i(TAG, "capture session configured; repeating preview active")
                        } catch (t: Throwable) {
                            Log.e(TAG, "setRepeatingRequest failed", t)
                            stopInternal()
                        }
                    }

                    override fun onConfigureFailed(session: CameraCaptureSession) {
                        Log.e(TAG, "capture session configure failed")
                        stopInternal()
                    }
                },
                cameraHandler,
            )
        } catch (t: Throwable) {
            Log.e(TAG, "createCaptureSession failed", t)
            stopInternal()
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
            val jpeg = image.toJpeg(jpegQuality) ?: return
            previewClient.postFrame(jpeg)
        } catch (t: Throwable) {
            Log.w(TAG, "frame handler error", t)
        } finally {
            image.close()
        }
    }

    fun stop() {
        stopInternal()
        lifecycleObserver?.let { obs -> ownerRef?.lifecycle?.removeObserver(obs) }
        lifecycleObserver = null
        ownerRef = null
    }

    private fun stopInternal() {
        started = false
        try { captureSession?.close() } catch (_: Throwable) {}
        captureSession = null
        try { cameraDevice?.close() } catch (_: Throwable) {}
        cameraDevice = null
        try { imageReader?.close() } catch (_: Throwable) {}
        imageReader = null
        cameraThread?.quitSafely()
        cameraThread = null
        cameraHandler = null
    }

    /**
     * YUV_420_888 → JPEG via NV21. Same approach as the old CameraX path:
     * concatenate Y, V, U as contiguous blocks. This is good enough for
     * preview-quality JPEGs even when row/pixel strides aren't ideal; if a
     * specific Rokid sensor produces visibly garbled frames at 640x480, the
     * fix is to honor planes[i].rowStride / pixelStride per pixel, not here.
     */
    private fun Image.toJpeg(quality: Int): ByteArray? {
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

        val yuv = YuvImage(nv21, ImageFormat.NV21, width, height, null)
        val out = ByteArrayOutputStream(64 * 1024)
        return if (yuv.compressToJpeg(Rect(0, 0, width, height), quality, out)) {
            out.toByteArray()
        } else {
            null
        }
    }

    companion object {
        private const val TAG = "ShiftMind.Camera"
    }
}
