package com.shiftmind.glasses

import android.Manifest
import android.app.KeyguardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.util.Log
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import com.shiftmind.glasses.camera.CameraPreviewStreamer
import com.shiftmind.glasses.hud.HudScreen
import com.shiftmind.glasses.service.GlassesService
import com.shiftmind.glasses.ui.theme.GlassHudTheme

class MainActivity : ComponentActivity() {

    private val streamer by lazy { CameraPreviewStreamer(applicationContext) }

    // Have we already kicked off `streamer.start()` for the current foreground
    // session? Reset on pause so the next resume re-opens the camera.
    private var streamerStarted = false

    // Held while the activity is foreground. Forces framework display state
    // to ON — without this on Rokid, the prism is lit but AOSP reports
    // `state OFF` and the activity sits in sleep, blocking camera open.
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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // ─── Rokid prism display-wake workaround ───────────────────────────
        // The Rokid prism doesn't report its wake state back to AOSP. Without
        // these flags + an explicit wake lock, framework sees `display=OFF`
        // and treats this activity as sleeping → process state NONEXISTENT
        // → CameraService rejects openCamera with CAMERA_DISABLED.
        //
        // setTurnScreenOn + setShowWhenLocked: AMS-blessed way to force
        //   display-on at activity creation (API 27+).
        // FLAG_KEEP_SCREEN_ON: keeps it on while we're foreground.
        // KeyguardManager.requestDismissKeyguard: belt-and-suspenders in case
        //   anything ever shows lock UI on the prism.
        // PowerManager SCREEN_BRIGHT_WAKE_LOCK | ACQUIRE_CAUSES_WAKEUP:
        //   deprecated but still the only API that actually flips the
        //   framework's display state ON when the panel doesn't notify.
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        getSystemService(KeyguardManager::class.java)
            ?.requestDismissKeyguard(this, null)
        acquireScreenWakeLock()

        // FGS gives us a stable proc state across the activity's pause/resume.
        // Started here, not from Application.onCreate — Android 12+ rejects
        // FGS starts from background contexts.
        startForegroundService(Intent(this, GlassesService::class.java))

        setContent {
            GlassHudTheme {
                HudScreen()
            }
        }
        ensureCameraPermission()
    }

    override fun onResume() {
        super.onResume()
        // Small post-delay so the wake-lock + FGS land and AMS promotes our
        // proc state before openCamera. ~300ms is enough for cold start on
        // the Rokid; warm restarts succeed near-immediately.
        window.decorView.postDelayed({ startStreamerIfForeground() }, 300)
    }

    override fun onPause() {
        // Releases camera so other apps (and our next resume) can grab it.
        streamer.stop()
        streamerStarted = false
        super.onPause()
    }

    private fun ensureCameraPermission() {
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.CAMERA,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            cameraPermissionRequest.launch(Manifest.permission.CAMERA)
        }
    }

    private fun startStreamerIfForeground() {
        if (streamerStarted) return
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.CAMERA,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) return
        streamerStarted = true
        streamer.start(this)
    }

    @Suppress("DEPRECATION") // SCREEN_BRIGHT_WAKE_LOCK is the only API that works here
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
        streamer.stop()
        releaseScreenWakeLock()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "ShiftMind.Main"
        // 4-hour safety cap on the screen wake lock, matching GlassesService.
        private const val WAKE_LOCK_TIMEOUT_MS = 4L * 60L * 60L * 1000L
    }
}
