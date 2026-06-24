package com.shiftmind.glasses.camera

import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.util.Log
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.face.Face
import com.google.mlkit.vision.face.FaceDetection
import com.google.mlkit.vision.face.FaceDetector
import com.google.mlkit.vision.face.FaceDetectorOptions
import java.io.ByteArrayOutputStream
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Lightweight on-device face gate.
 *
 * ML Kit only answers "face box exists"; recognition stays server-side later.
 * We use it here to decide whether a face overlaps the center HUD cursor and
 * to produce a face crop for the dev preview.
 */
class FaceGateDetector(
    private val frameWidth: Int,
    private val frameHeight: Int,
    private val jpegQuality: Int = 82,
    private val onResult: (FaceGateResult) -> Unit,
) {
    private val detector: FaceDetector = FaceDetection.getClient(
        FaceDetectorOptions.Builder()
            .setPerformanceMode(FaceDetectorOptions.PERFORMANCE_MODE_ACCURATE)
            .setLandmarkMode(FaceDetectorOptions.LANDMARK_MODE_NONE)
            .setContourMode(FaceDetectorOptions.CONTOUR_MODE_NONE)
            .setClassificationMode(FaceDetectorOptions.CLASSIFICATION_MODE_NONE)
            .setMinFaceSize(0.04f)
            .enableTracking()
            .build(),
    )

    private val busy = AtomicBoolean(false)

    fun process(nv21: ByteArray, rotationDegrees: Int = 0) {
        if (!busy.compareAndSet(false, true)) return
        val image = InputImage.fromByteArray(
            nv21,
            frameWidth,
            frameHeight,
            rotationDegrees,
            InputImage.IMAGE_FORMAT_NV21,
        )
        detector.process(image)
            .addOnSuccessListener { faces ->
                try {
                    onResult(buildResult(nv21, faces))
                } catch (t: Throwable) {
                    Log.w(TAG, "face gate result failed", t)
                } finally {
                    busy.set(false)
                }
            }
            .addOnFailureListener { t ->
                Log.w(TAG, "ML Kit face detection failed", t)
                busy.set(false)
            }
    }

    fun close() {
        detector.close()
    }

    private fun buildResult(nv21: ByteArray, faces: List<Face>): FaceGateResult {
        val cursor = cursorRect()
        val best = faces
            .map { it.boundingBox.clampToFrame() }
            .filter { !it.isEmpty }
            .maxByOrNull { intersectionArea(it, cursor) }
            ?: return FaceGateResult(faceDetected = false, faceInCursor = false)

        val overlap = intersectionArea(best, cursor)
        val faceInCursor = overlap > 0 && overlap.toFloat() / maxOf(1, best.width() * best.height()) >= MIN_FACE_OVERLAP
        val crop = if (faceInCursor) cropFace(nv21, best) else null
        return FaceGateResult(
            faceDetected = true,
            faceInCursor = faceInCursor,
            faceBox = best,
            cursorBox = cursor,
            faceCropJpeg = crop,
        )
    }

    private fun cursorRect(): Rect {
        val side = (minOf(frameWidth, frameHeight) * CURSOR_SIDE_RATIO).toInt()
        val cx = frameWidth / 2
        val cy = frameHeight / 2
        return Rect(cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)
    }

    private fun cropFace(nv21: ByteArray, faceBox: Rect): ByteArray? {
        val padded = faceBox.expand(0.28f).clampToFrame().evenAligned()
        if (padded.width() < 2 || padded.height() < 2) return null
        val yuv = YuvImage(nv21, ImageFormat.NV21, frameWidth, frameHeight, null)
        val out = ByteArrayOutputStream(32 * 1024)
        return if (yuv.compressToJpeg(padded, jpegQuality, out)) out.toByteArray() else null
    }

    private fun Rect.clampToFrame(): Rect = Rect(
        left.coerceIn(0, frameWidth - 1),
        top.coerceIn(0, frameHeight - 1),
        right.coerceIn(1, frameWidth),
        bottom.coerceIn(1, frameHeight),
    )

    private fun Rect.expand(fraction: Float): Rect {
        val dx = (width() * fraction).toInt()
        val dy = (height() * fraction).toInt()
        return Rect(left - dx, top - dy, right + dx, bottom + dy)
    }

    private fun Rect.evenAligned(): Rect = Rect(
        left and 1.inv(),
        top and 1.inv(),
        if (right % 2 == 0) right else right - 1,
        if (bottom % 2 == 0) bottom else bottom - 1,
    )

    private fun intersectionArea(a: Rect, b: Rect): Int {
        val left = maxOf(a.left, b.left)
        val top = maxOf(a.top, b.top)
        val right = minOf(a.right, b.right)
        val bottom = minOf(a.bottom, b.bottom)
        return maxOf(0, right - left) * maxOf(0, bottom - top)
    }

    companion object {
        private const val TAG = "ShiftMind.FaceGate"
        private const val CURSOR_SIDE_RATIO = 0.52f
        private const val MIN_FACE_OVERLAP = 0.35f
    }
}

data class FaceGateResult(
    val faceDetected: Boolean,
    val faceInCursor: Boolean,
    val faceBox: Rect? = null,
    val cursorBox: Rect? = null,
    val faceCropJpeg: ByteArray? = null,
)
