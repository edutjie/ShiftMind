package com.shiftmind.glasses.recording

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.util.Log

/**
 * Diagnostic receiver for Rokid recording broadcasts.
 *
 * Intent facts learned from inspecting OverlayRec (GPL-3.0 — facts only,
 * no source copied) and Rokid's published assist-server protocol:
 *
 *   action:   com.rokid.os.master.assist.server.cmd
 *   extras:   cmd_type=control_scene
 *             scene=mix_record   (HUD+camera fused recording)
 *             open=true|false    (start / stop)
 *
 * ShiftMind no longer uses external `mix_record`; it owns the camera-side
 * recorder and starts Rokid screenstream for HUD-only capture. If another
 * app triggers `mix_record`, it will conflict for the single camera, so this
 * receiver only logs the event.
 */
class RecordingStateReceiver(
    private val onRecordingStart: () -> Unit,
    private val onRecordingStop: () -> Unit,
) : BroadcastReceiver() {

    private var registered = false

    fun register(context: Context) {
        if (registered) return
        val filter = IntentFilter().apply {
            addAction(ACTION_ROKID_CMD)
            addAction(ACTION_SCREENRECORD_ON)
            addAction(ACTION_SCREENRECORD_OFF)
        }
        // RECEIVER_EXPORTED — these are system broadcasts from other apps/services.
        // Android 14 (targetSdk 34) requires the flag explicitly.
        context.registerReceiver(this, filter, Context.RECEIVER_EXPORTED)
        registered = true
        Log.i(TAG, "registered for Rokid mix_record + screen-record broadcasts")
    }

    fun unregister(context: Context) {
        if (!registered) return
        runCatching { context.unregisterReceiver(this) }
        registered = false
    }

    override fun onReceive(context: Context?, intent: Intent?) {
        val action = intent?.action ?: return
        when (action) {
            ACTION_ROKID_CMD -> handleRokidCmd(intent)
            ACTION_SCREENRECORD_ON -> Log.i(TAG, "HUD screen record started (camera not affected)")
            ACTION_SCREENRECORD_OFF -> Log.i(TAG, "HUD screen record stopped")
        }
    }

    private fun handleRokidCmd(intent: Intent) {
        val cmdType = intent.getStringExtra("cmd_type")
        val scene = intent.getStringExtra("scene")
        // Rokid passes `open` either as boolean or string; tolerate both.
        val open = intent.getBooleanExtra("open", false) ||
            intent.getStringExtra("open")?.equals("true", ignoreCase = true) == true

        if (cmdType != "control_scene" || scene != SCENE_MIX_RECORD) return

        if (open) {
            Log.i(TAG, "external mix_record START detected")
            onRecordingStart()
        } else {
            Log.i(TAG, "external mix_record STOP detected")
            onRecordingStop()
        }
    }

    companion object {
        private const val TAG = "ShiftMind.RecRx"
        // Rokid assist server command channel.
        private const val ACTION_ROKID_CMD = "com.rokid.os.master.assist.server.cmd"
        private const val SCENE_MIX_RECORD = "mix_record"
        // HUD-only screen record (different code path on the device).
        private const val ACTION_SCREENRECORD_ON = "com.rokid.yodaos.action.SCREENRECORD_ON"
        private const val ACTION_SCREENRECORD_OFF = "com.rokid.yodaos.action.SCREENRECORD_OFF"
    }
}
