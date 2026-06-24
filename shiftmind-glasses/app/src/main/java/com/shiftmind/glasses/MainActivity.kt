package com.shiftmind.glasses

import android.Manifest
import android.app.KeyguardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.util.Log
import android.view.KeyEvent
import android.view.WindowManager
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.lifecycle.lifecycleScope
import androidx.core.content.ContextCompat
import com.shiftmind.glasses.camera.CameraPreviewStreamer
import com.shiftmind.glasses.hud.HudScreen
import com.shiftmind.glasses.hud.HudStateHolder
import com.shiftmind.glasses.net.HudStateClient
import com.shiftmind.glasses.recording.ArRecorder
import com.shiftmind.glasses.recording.RecordingStateReceiver
import com.shiftmind.glasses.recording.RokidHudRecorder
import com.shiftmind.glasses.service.GlassesService
import com.shiftmind.glasses.ui.theme.GlassHudTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : ComponentActivity() {

    private val streamer by lazy { CameraPreviewStreamer(applicationContext) }
    private val recorder by lazy { ArRecorder(applicationContext, streamer, extraRotationDeg = BuildConfig.RECORDING_ROTATION_DEG) }
    private val hudRecorder by lazy { RokidHudRecorder(applicationContext) }
    private val hudStateClient by lazy { HudStateClient() }

    // External AR recorder (Rokid mix_record from OverlayRec etc.) is no longer
    // expected to be used now that ShiftMind has its own recorder — but if
    // someone still triggers it, we just log the broadcast instead of fighting
    // for the camera. The user can resolve manually.
    private val recordingReceiver = RecordingStateReceiver(
        onRecordingStart = {
            Log.w(TAG, "external mix_record START detected — ShiftMind has its own recorder; this will conflict for the camera")
        },
        onRecordingStop = {
            Log.i(TAG, "external mix_record STOP detected")
        },
    )

    private var screenWakeLock: PowerManager.WakeLock? = null

    private val cameraPermissionRequest = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            Log.i(TAG, "Camera permission granted; starting preview stream")
            startStreamerIfForeground()
        } else {
            Log.w(TAG, "Camera permission denied; preview stream disabled")
        }
    }

    private val audioPermissionRequest = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            Log.i(TAG, "Mic permission granted; recordings will include audio")
        } else {
            Log.w(TAG, "Mic permission denied; recordings will be video-only")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // ─── Rokid prism display-wake workaround ───────────────────────────
        // See CLAUDE.md for the full diagnostic. tl;dr: prism doesn't report
        // its wake state to AOSP; without these flags + a deprecated wake
        // lock, the activity sits in a sleeping state and camera open fails.
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        getSystemService(KeyguardManager::class.java)
            ?.requestDismissKeyguard(this, null)
        acquireScreenWakeLock()

        startForegroundService(Intent(this, GlassesService::class.java))

        setContent {
            GlassHudTheme {
                HudScreen()
            }
        }

        recordingReceiver.register(this)
        hudRecorder.observeStateBroadcasts()
        startHudStateMirror()
        ensureCameraPermission()
        ensureAudioPermission()

        Log.i(TAG, "recording trigger key = ${BuildConfig.RECORDING_KEY} (${KeyEvent.keyCodeToString(BuildConfig.RECORDING_KEY)})")
    }

    override fun onResume() {
        super.onResume()
        window.decorView.postDelayed({ startStreamerIfForeground() }, 300)
    }

    override fun onPause() {
        // If we're paused mid-recording, stop the recorders cleanly first so
        // the MP4 finalizes. Then release camera.
        if (recorder.isRecording) {
            Log.i(TAG, "activity paused while recording — finalizing MP4")
            stopRecording()
        }
        streamer.stop()
        super.onPause()
    }

    /**
     * Catch hardware key presses at the activity level. We log every keycode
     * (so the user can discover which physical button corresponds to which
     * Android KeyEvent on their hardware) and toggle recording on the
     * configured trigger keycode (BuildConfig.RECORDING_KEY, defaults to
     * KEYCODE_ENTER = 66). Customize via `shiftmind.recordingKey=NNN` in
     * `local.properties`.
     *
     * Using dispatchKeyEvent (not onKeyDown) so we see keys even when Compose
     * focus would otherwise consume them.
     */
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (event.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0) {
            Log.i(TAG, "keyEvent: code=${event.keyCode} (${KeyEvent.keyCodeToString(event.keyCode)}) scan=${event.scanCode}")
            if (event.keyCode == BuildConfig.RECORDING_KEY) {
                onRecordingButtonPressed()
                return true   // consume — don't let system also process this key
            }
        }
        return super.dispatchKeyEvent(event)
    }

    /**
     * Toggle both recorders together. They produce two parallel MP4s:
     *   - `Movies/ShiftMind/shiftmind_<ts>_cam.mp4` — our Camera2 + audio recorder
     *   - `Movies/ScreenRecorder/<rokid-name>.mp4` — Rokid's screenstream HUD recorder
     *
     * The HUD recording uses Rokid's `com.rokid.os.master.screenstream`
     * vendor service.
     * Basenames won't match — pair by approximate wall-clock time.
     * `/pull-videos` pulls both and auto-combines them into a keyed HUD overlay.
     */
    private fun onRecordingButtonPressed() {
        if (!hasCameraPerm()) {
            Toast.makeText(this, "Grant camera permission first", Toast.LENGTH_SHORT).show()
            return
        }
        if (recorder.isRecording) {
            stopRecording()
            Toast.makeText(this, "■ Recording saved", Toast.LENGTH_SHORT).show()
            Log.i(TAG, "■ Recording saved")
            return
        }
        if (!streamer.isActive) {
            Toast.makeText(this, "Camera unavailable — reopen app", Toast.LENGTH_SHORT).show()
            Log.w(TAG, "recording blocked: streamer not active (camera stolen?)")
            return
        }
        val ts = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        // Kick off both sides in parallel. HUD side is fire-and-forget — if
        // screenstream isn't installed, the broadcast is dropped silently
        // and we still get the cam-side MP4.
        hudRecorder.start()
        recorder.start(ts)
        Toast.makeText(this, "● Recording started (cam+hud)", Toast.LENGTH_SHORT).show()
        Log.i(TAG, "● Recording started: cam_stem=$ts, hud=screenstream")
    }

    /** Stop camera recorder + tell screenstream to stop the HUD recorder. */
    private fun stopRecording() {
        if (recorder.isRecording) recorder.stop()
        hudRecorder.stop()
    }

    private fun hasCameraPerm(): Boolean = ContextCompat.checkSelfPermission(
        this, Manifest.permission.CAMERA,
    ) == PackageManager.PERMISSION_GRANTED

    private fun ensureCameraPermission() {
        if (!hasCameraPerm()) {
            cameraPermissionRequest.launch(Manifest.permission.CAMERA)
        }
    }

    private fun hasAudioPerm(): Boolean = ContextCompat.checkSelfPermission(
        this, Manifest.permission.RECORD_AUDIO,
    ) == PackageManager.PERMISSION_GRANTED

    private fun ensureAudioPermission() {
        if (!hasAudioPerm()) {
            audioPermissionRequest.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun startStreamerIfForeground() {
        if (!hasCameraPerm()) return
        // streamer.start() is idempotent + self-retries on CAMERA_IN_USE.
        streamer.start(this)
    }

    private fun startHudStateMirror() {
        lifecycleScope.launch {
            HudStateHolder.stateFlow
                .collect { state ->
                    withContext(Dispatchers.IO) {
                        hudStateClient.postState(state)
                    }
                }
        }
    }

    @Suppress("DEPRECATION")
    private fun acquireScreenWakeLock() {
        if (screenWakeLock?.isHeld == true) return
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        screenWakeLock = pm.newWakeLock(
            PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP,
            "ShiftMind::ScreenWake",
        ).apply { acquire(WAKE_LOCK_TIMEOUT_MS) }
    }

    private fun releaseScreenWakeLock() {
        screenWakeLock?.takeIf { it.isHeld }?.release()
        screenWakeLock = null
    }

    override fun onDestroy() {
        if (recorder.isRecording) stopRecording()
        hudRecorder.stopObservingStateBroadcasts()
        recordingReceiver.unregister(this)
        streamer.stop()
        releaseScreenWakeLock()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "ShiftMind.Main"
        private const val WAKE_LOCK_TIMEOUT_MS = 4L * 60L * 60L * 1000L
    }
}
