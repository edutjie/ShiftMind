package com.shiftmind.glasses.recording

import android.content.ContentValues
import android.content.Context
import android.media.MediaFormat
import android.media.MediaMuxer
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.ParcelFileDescriptor
import android.provider.MediaStore
import android.util.Log
import java.io.File
import java.nio.ByteBuffer
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * Shared muxer wrapper coordinating video + (optional) audio tracks.
 *
 * Why: a single [MediaMuxer] can hold multiple tracks but requires
 * `addTrack` BEFORE `start`, and `writeSampleData` only AFTER `start`. With
 * two encoders running concurrently, both must hand their `MediaFormat` to
 * the muxer before either can emit samples. This class:
 *
 *   1. Lets each encoder register its track when its `INFO_OUTPUT_FORMAT_CHANGED`
 *      callback fires ([addVideoTrack] / [addAudioTrack]).
 *   2. Starts the muxer only when every expected track has been added —
 *      controlled by [audioEnabled] flag set before [open].
 *   3. Serializes [writeVideo] / [writeAudio] against a single lock so
 *      MediaMuxer (not thread-safe) sees a single writer.
 *
 * Output: `Movies/ShiftMind/shiftmind_<timestamp><suffix>.mp4` via MediaStore
 * on API 29+, raw filesystem path on older. The file is marked
 * `IS_PENDING=1` while writing and cleared on [finalize].
 *
 * The [timestamp] parameter is shared across parallel muxer instances (camera
 * + HUD recording) so the two output files match by basename — `..._cam.mp4`
 * + `..._hud.mp4` from the same recording session.
 *
 * Threading: [open], [addVideoTrack], [addAudioTrack], [writeVideo],
 * [writeAudio], [finalize] are all internally synchronized — call from
 * whatever thread the encoder drains on. [finalize] should be invoked
 * exactly once after both encoders have signaled EOS.
 */
class RecordingMuxer(
    private val context: Context,
    /**
     * Shared session timestamp (e.g. `20260623_233916`). Used as the filename
     * stem so parallel cam+hud muxers produce coordinated `..._cam.mp4` and
     * `..._hud.mp4` files. Pass the same value to both.
     */
    private val timestamp: String = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date()),
    /**
     * Filename suffix before `.mp4`, e.g. `_cam` or `_hud`. Empty string keeps
     * the legacy single-file naming `shiftmind_<timestamp>.mp4`.
     */
    private val suffix: String = "",
) {

    private val lock = ReentrantLock()

    private var muxer: MediaMuxer? = null
    private var pfd: ParcelFileDescriptor? = null
    private var fileUri: Uri? = null
    private var outPath: String? = null

    private var videoTrackIndex: Int = -1
    private var audioTrackIndex: Int = -1
    private var audioEnabled: Boolean = false

    @Volatile private var started: Boolean = false
    @Volatile private var finalized: Boolean = false

    /** Set BEFORE [open] to indicate audio track will be added. */
    fun setAudioEnabled(enabled: Boolean) { audioEnabled = enabled }

    /** Returns the path/uri the muxer writes to (for logging). */
    val outputPath: String? get() = outPath

    /** Create the MediaStore entry + open the underlying MediaMuxer. */
    fun open() = lock.withLock {
        check(muxer == null) { "muxer already opened" }
        val path = openOutputFile()
        muxer = MediaMuxer(path, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        Log.i(TAG, "muxer opened -> $outPath (audio=$audioEnabled)")
    }

    /**
     * Register video track. Called from VideoEncoder's drain loop when its
     * output format becomes known. Returns the track index for subsequent
     * [writeVideo] calls.
     */
    fun addVideoTrack(format: MediaFormat): Int = lock.withLock {
        check(videoTrackIndex == -1) { "video track already added" }
        check(!started) { "cannot add track after muxer started" }
        videoTrackIndex = muxer!!.addTrack(format)
        Log.i(TAG, "video track added: idx=$videoTrackIndex")
        maybeStart()
        videoTrackIndex
    }

    /** Register audio track. Returns its track index. */
    fun addAudioTrack(format: MediaFormat): Int = lock.withLock {
        check(audioEnabled) { "audio not enabled — call setAudioEnabled(true) before open()" }
        check(audioTrackIndex == -1) { "audio track already added" }
        check(!started) { "cannot add track after muxer started" }
        audioTrackIndex = muxer!!.addTrack(format)
        Log.i(TAG, "audio track added: idx=$audioTrackIndex")
        maybeStart()
        audioTrackIndex
    }

    /** True once all required tracks added and muxer.start() was called. */
    val isStarted: Boolean get() = started

    private fun maybeStart() {
        if (started) return
        if (videoTrackIndex < 0) return
        if (audioEnabled && audioTrackIndex < 0) return
        muxer!!.start()
        started = true
        Log.i(TAG, "muxer started (video=$videoTrackIndex audio=$audioTrackIndex)")
    }

    /** Thread-safe video write. No-op if muxer not started yet. */
    fun writeVideo(buffer: ByteBuffer, info: android.media.MediaCodec.BufferInfo) = lock.withLock {
        if (!started || finalized) return@withLock
        muxer!!.writeSampleData(videoTrackIndex, buffer, info)
    }

    /** Thread-safe audio write. No-op if muxer not started yet. */
    fun writeAudio(buffer: ByteBuffer, info: android.media.MediaCodec.BufferInfo) = lock.withLock {
        if (!started || finalized) return@withLock
        muxer!!.writeSampleData(audioTrackIndex, buffer, info)
    }

    /**
     * Stop + release the muxer, close the file descriptor, mark MediaStore
     * entry visible (IS_PENDING=0). Safe to call multiple times.
     */
    fun finalize() = lock.withLock {
        if (finalized) return@withLock
        finalized = true
        try {
            if (started) muxer?.stop()
        } catch (t: Throwable) {
            Log.w(TAG, "muxer.stop failed", t)
        }
        try { muxer?.release() } catch (_: Throwable) {}
        muxer = null
        try { pfd?.close() } catch (_: Throwable) {}
        pfd = null
        publishToMediaStore()
        Log.i(TAG, "muxer finalized -> $outPath")
    }

    /** Build the output file path (and MediaStore row on API 29+). */
    private fun openOutputFile(): String {
        val name = "shiftmind_${timestamp}${suffix}.mp4"

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues().apply {
                put(MediaStore.Video.Media.DISPLAY_NAME, name)
                put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
                put(MediaStore.Video.Media.RELATIVE_PATH, "${Environment.DIRECTORY_MOVIES}/ShiftMind/")
                put(MediaStore.Video.Media.IS_PENDING, 1)
            }
            val uri = context.contentResolver.insert(
                MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values,
            ) ?: error("MediaStore insert returned null")
            fileUri = uri
            val descriptor = context.contentResolver.openFileDescriptor(uri, "w")
                ?: error("openFileDescriptor returned null")
            pfd = descriptor
            val fdPath = "/proc/self/fd/${descriptor.fd}"
            outPath = fdPath
            return fdPath
        } else {
            val dir = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MOVIES), "ShiftMind")
            dir.mkdirs()
            val f = File(dir, name)
            outPath = f.absolutePath
            return f.absolutePath
        }
    }

    private fun publishToMediaStore() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && fileUri != null) {
            val values = ContentValues().apply {
                put(MediaStore.Video.Media.IS_PENDING, 0)
            }
            context.contentResolver.update(fileUri!!, values, null, null)
        }
    }

    companion object {
        private const val TAG = "ShiftMind.Muxer"
    }
}
