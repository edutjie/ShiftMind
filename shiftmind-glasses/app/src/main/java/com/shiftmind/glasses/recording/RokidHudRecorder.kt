package com.shiftmind.glasses.recording

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.util.Log

/**
 * Drives Rokid's built-in HUD screen recorder via the screenstream broadcast
 * channel. The actual MP4 is produced by the Rokid system service
 * `com.rokid.os.master.screenstream` and lands in that service's own MediaStore
 * folder (typically `Movies/ScreenRecorder/`).
 *
 * ShiftMind uses screenstream for the HUD side, then `/pull-videos` combines
 * the HUD MP4 with the cam+audio MP4 after pulling files from the glasses.
 * The screenstream service is a vendor-owned recorder that accepts these
 * broadcasts directly.
 *
 * Intent facts (re-derived independently from OverlayRec's published behavior
 * — GPL-3.0 viral, source not copied):
 *
 *   start:  action  com.rokid.yodaos.action.SCREENRECORD_ON
 *           package com.rokid.os.master.screenstream    ← REQUIRED. Without
 *                                                         setPackage() the
 *                                                         broadcast is dropped
 *                                                         silently (system
 *                                                         observed: result=0
 *                                                         then no-op).
 *           flags   FLAG_RECEIVER_FOREGROUND
 *   stop:   action  com.rokid.yodaos.action.SCREENRECORD_OFF (same target)
 *
 * The screenstream service echoes its state back on these (read-only) actions,
 * which [RecordingStateReceiver] / [observeStateBroadcasts] watch:
 *
 *   started: com.rokid.yodaos.action.SCREENRECORD_START
 *   stopped: com.rokid.yodaos.action.SCREENRECORD_STOP
 *
 * Pairing with [ArRecorder]:
 *   - press-record → call [start] here AND `recorder.start(ts)` together.
 *     Two MP4s land in parallel; basenames don't match (Rokid names its own
 *     file), but they share approximate wall-clock time.
 *   - press-record again → [stop] + `recorder.stop()`.
 *
 * This recorder is fire-and-forget — no error path beyond logging. If
 * screenstream is missing on the device (non-Rokid hardware), the broadcast
 * is just discarded and nothing happens. Confirm health via the echoed
 * SCREENRECORD_START broadcast appearing in logcat.
 */
class RokidHudRecorder(private val context: Context) {

    @Volatile private var requested = false
    /** Last confirmed state from the screenstream echo broadcast. */
    @Volatile var confirmedActive: Boolean = false
        private set

    /**
     * Best-effort: returns the requested state. Whether screenstream actually
     * complied is reflected in [confirmedActive] after the echo broadcast.
     */
    val isRecording: Boolean get() = requested

    fun start() {
        if (requested) return
        requested = true
        send(ACTION_SCREENRECORD_ON)
    }

    fun stop() {
        if (!requested) return
        requested = false
        send(ACTION_SCREENRECORD_OFF)
    }

    /** Idempotent toggle: returns the new requested state. */
    fun toggle(): Boolean {
        if (requested) { stop(); return false }
        start()
        return true
    }

    /**
     * Optional: install a lightweight receiver that updates [confirmedActive]
     * when screenstream echoes its state. Caller owns the lifecycle — call
     * [stopObservingStateBroadcasts] before context teardown.
     */
    fun observeStateBroadcasts() {
        if (echoReceiver != null) return
        val rx = object : BroadcastReceiver() {
            override fun onReceive(context: Context?, intent: Intent?) {
                when (intent?.action) {
                    ACTION_SCREENRECORD_START -> {
                        confirmedActive = true
                        Log.i(TAG, "screenstream confirmed: SCREENRECORD_START")
                    }
                    ACTION_SCREENRECORD_STOP -> {
                        confirmedActive = false
                        Log.i(TAG, "screenstream confirmed: SCREENRECORD_STOP")
                    }
                }
            }
        }
        val filter = IntentFilter().apply {
            addAction(ACTION_SCREENRECORD_START)
            addAction(ACTION_SCREENRECORD_STOP)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.registerReceiver(rx, filter, Context.RECEIVER_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            context.registerReceiver(rx, filter)
        }
        echoReceiver = rx
    }

    fun stopObservingStateBroadcasts() {
        echoReceiver?.let {
            runCatching { context.unregisterReceiver(it) }
            echoReceiver = null
        }
    }

    private var echoReceiver: BroadcastReceiver? = null

    private fun send(action: String) {
        val intent = Intent(action).apply {
            setPackage(PACKAGE_SCREENSTREAM)
            addFlags(Intent.FLAG_RECEIVER_FOREGROUND)
        }
        context.sendBroadcast(intent)
        Log.i(TAG, "sent $action → $PACKAGE_SCREENSTREAM")
    }

    companion object {
        private const val TAG = "ShiftMind.RokidHud"
        // The vendor service that owns MANAGE_MEDIA_PROJECTION on Rokid.
        private const val PACKAGE_SCREENSTREAM = "com.rokid.os.master.screenstream"
        // Trigger broadcasts.
        private const val ACTION_SCREENRECORD_ON = "com.rokid.yodaos.action.SCREENRECORD_ON"
        private const val ACTION_SCREENRECORD_OFF = "com.rokid.yodaos.action.SCREENRECORD_OFF"
        // State echoes (read-only).
        private const val ACTION_SCREENRECORD_START = "com.rokid.yodaos.action.SCREENRECORD_START"
        private const val ACTION_SCREENRECORD_STOP = "com.rokid.yodaos.action.SCREENRECORD_STOP"
    }
}
