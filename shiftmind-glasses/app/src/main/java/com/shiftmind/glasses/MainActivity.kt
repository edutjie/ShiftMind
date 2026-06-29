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
import com.shiftmind.glasses.net.SessionAudioStreamClient
import com.shiftmind.glasses.net.SessionClient
import com.shiftmind.glasses.recording.ArRecorder
import com.shiftmind.glasses.recording.MicConfigProbe
import com.shiftmind.glasses.recording.RecordingStateReceiver
import com.shiftmind.glasses.recording.RokidHudRecorder
import com.shiftmind.glasses.recording.VoiceSessionRecorder
import com.shiftmind.glasses.service.GlassesService
import com.shiftmind.glasses.session.CustomerSessionHolder
import com.shiftmind.glasses.session.CustomerSessionState
import com.shiftmind.glasses.ui.theme.GlassHudTheme
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
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
    private val sessionClient by lazy { SessionClient() }
    private val sessionAudioStreamClient by lazy { SessionAudioStreamClient() }
    private val voiceSessionRecorder by lazy { VoiceSessionRecorder(applicationContext) }

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
    private var transcriptClearJob: Job? = null

    // Completes once the startup mic-config probe has finished (or is skipped).
    // Camera/preview start awaits this so Camera2 never opens while we're
    // probing alternate mic sources/channel masks — per CLAUDE.md, probing
    // before camera avoids wedging the HAL. No-op when AUDIO_PROBE is false.
    private val probeDone: CompletableDeferred<Unit> = CompletableDeferred()

    private val cameraPermissionRequest = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            Log.i(TAG, "Camera permission granted; starting preview stream")
            lifecycleScope.launch {
                probeDone.await()
                startStreamerIfForeground()
            }
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
        startCustomerSessionWatcher()
        maybeRunMicProbe()
        ensureCameraPermission()
        ensureAudioPermission()

        Log.i(TAG, "recording trigger key = ${BuildConfig.RECORDING_KEY} (${KeyEvent.keyCodeToString(BuildConfig.RECORDING_KEY)})")
        Log.i(TAG, "session stop key = ${BuildConfig.SESSION_KEY} (${KeyEvent.keyCodeToString(BuildConfig.SESSION_KEY)})")
        if (BuildConfig.RECORDING_KEY == BuildConfig.SESSION_KEY) {
            Log.w(TAG, "recordingKey and sessionKey are the same; set shiftmind.sessionKey to a different physical button")
        }
    }

    override fun onResume() {
        super.onResume()
        // Wait for the startup mic probe (if running) before opening Camera2.
        // When AUDIO_PROBE is off, probeDone is already complete → no delay.
        lifecycleScope.launch {
            probeDone.await()
            startStreamerIfForeground()
        }
    }

    /**
     * TEMP: run MicConfigProbe at startup, behind BuildConfig.AUDIO_PROBE and
     * only if RECORD_AUDIO is already granted. Probe runs before the camera
     * opens (see onResume / cameraPermissionRequest gating on probeDone).
     * Always completes probeDone so camera startup never hangs.
     */
    private fun maybeRunMicProbe() {
        if (!BuildConfig.AUDIO_PROBE) {
            probeDone.complete(Unit)
            return
        }
        val micGranted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
        if (!micGranted) {
            Log.w(TAG, "AUDIO_PROBE on but RECORD_AUDIO not granted yet — skipping probe this run")
            probeDone.complete(Unit)
            return
        }
        lifecycleScope.launch(Dispatchers.IO) {
            Log.i(TAG, "AUDIO_PROBE: launching MicConfigProbe (camera gated until done)")
            try {
                MicConfigProbe(applicationContext).run()
            } catch (t: Throwable) {
                Log.e(TAG, "MicConfigProbe crashed: ${t.javaClass.simpleName}: ${t.message}", t)
            } finally {
                probeDone.complete(Unit)
            }
        }
    }

    override fun onPause() {
        // If we're paused mid-recording, stop the recorders cleanly first so
        // the MP4 finalizes. Then release camera.
        if (recorder.isRecording) {
            Log.i(TAG, "activity paused while recording — finalizing MP4")
            stopRecording()
        }
        if (voiceSessionRecorder.isRecording) {
            Log.i(TAG, "activity paused while customer session active — finalizing voice session")
            stopCustomerSession()
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
            if (event.keyCode == BuildConfig.SESSION_KEY) {
                onSessionButtonPressed()
                return true
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

    private fun onSessionButtonPressed() {
        if (CustomerSessionHolder.current == null && !voiceSessionRecorder.isRecording) {
            Toast.makeText(this, "No active customer session", Toast.LENGTH_SHORT).show()
            return
        }
        stopCustomerSession()
        Toast.makeText(this, "■ Customer session saved", Toast.LENGTH_SHORT).show()
    }

    /** Stop camera recorder + tell screenstream to stop the HUD recorder. */
    private fun stopRecording() {
        if (recorder.isRecording) recorder.stop()
        hudRecorder.stop()
    }

    private fun startCustomerSessionWatcher() {
        lifecycleScope.launch {
            CustomerSessionHolder.stateFlow.collect { session ->
                if (session == null) return@collect
                startCustomerSession(session)
            }
        }
    }

    private fun startCustomerSession(session: CustomerSessionState) {
        HudStateHolder.mutate {
            it.copy(
                statusLine = "session ${session.customerId}",
                recordingActive = recorder.isRecording,
                customerCard = com.shiftmind.glasses.hud.CustomerCard(
                    name = if (session.isNewCustomer) "First visit" else session.name,
                    lastVisit = null,
                    memoryLine = session.memoryCard.ifBlank { null },
                    liveTranscript = "listening...",
                ),
            )
        }
        sessionAudioStreamClient.onTranscript = { text, _ ->
            val line = text.takeLast(140)
            if (line.isNotBlank()) {
                transcriptClearJob?.cancel()
                HudStateHolder.mutate {
                    it.copy(
                        customerCard = it.customerCard?.copy(liveTranscript = line),
                    )
                }
                // Hold the last spoken line on the HUD; only fall back to
                // "listening..." after a few seconds of silence so words don't
                // flash and vanish the moment a partial clears.
                transcriptClearJob = lifecycleScope.launch {
                    delay(TRANSCRIPT_HOLD_MS)
                    HudStateHolder.mutate {
                        it.copy(
                            customerCard = it.customerCard?.copy(liveTranscript = "listening..."),
                        )
                    }
                }
            }
        }
        sessionAudioStreamClient.start(session.sessionId)
        if (!voiceSessionRecorder.isRecording) {
            val started = voiceSessionRecorder.start { chunk ->
                sessionAudioStreamClient.sendPcm(chunk)
            }
            if (!started) {
                Log.w(TAG, "customer session active but voice recorder did not start")
            }
        }
    }

    private fun stopCustomerSession() {
        transcriptClearJob?.cancel()
        val session = CustomerSessionHolder.current
        val wav = voiceSessionRecorder.stopWav()
        val transcript = sessionAudioStreamClient.stop()
        if (session == null) {
            HudStateHolder.mutate { it.copy(recordingActive = recorder.isRecording, statusLine = "awaiting customer", customerCard = null) }
            return
        }
        HudStateHolder.mutate { it.copy(statusLine = "summarizing session", recordingActive = recorder.isRecording) }
        lifecycleScope.launch(Dispatchers.IO) {
            val ok = sessionClient.finishSession(session.sessionId, wav, transcript)
            CustomerSessionHolder.clear(session.sessionId)
            withContext(Dispatchers.Main) {
                HudStateHolder.mutate {
                    it.copy(
                        statusLine = if (ok) "awaiting customer" else "session upload failed",
                        customerCard = null,
                        recordingActive = recorder.isRecording,
                    )
                }
            }
        }
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
        if (voiceSessionRecorder.isRecording) stopCustomerSession()
        if (recorder.isRecording) stopRecording()
        hudRecorder.stopObservingStateBroadcasts()
        recordingReceiver.unregister(this)
        streamer.stop()
        releaseScreenWakeLock()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "ShiftMind.Main"
        private const val TRANSCRIPT_HOLD_MS = 6_000L
        private const val WAKE_LOCK_TIMEOUT_MS = 4L * 60L * 60L * 1000L
    }
}
