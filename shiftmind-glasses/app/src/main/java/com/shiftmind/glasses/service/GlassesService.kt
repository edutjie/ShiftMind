package com.shiftmind.glasses.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat

/**
 * Foreground service that keeps ShiftMind alive across Activity lifecycle.
 *
 * Phase 1 only owns the wake lock + notification — Camera2, audio capture,
 * and the HTTP client land in later phases. The foregroundServiceType flags
 * passed to startForeground() must match the manifest declaration, or
 * Android 14+ throws ForegroundServiceTypeException at start.
 */
class GlassesService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ShiftMind::Glasses")
            .apply { acquire(WAKE_LOCK_TIMEOUT_MS) }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Phase 1 uses only CONNECTED_DEVICE — it has no runtime-permission
        // gate. Adding CAMERA or MICROPHONE here on Android 14 throws
        // SecurityException unless the matching runtime permission is already
        // granted at this exact moment. The activity owns the camera while
        // foreground; once we move camera ownership into the service (phase
        // 5+ for audio), we can OR in the additional type flags.
        startForeground(
            NOTIFICATION_ID,
            buildNotification(),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE,
        )
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
        // stopForeground is implicit on destroy, but be explicit for clarity.
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "ShiftMind glasses",
            NotificationManager.IMPORTANCE_LOW, // silent
        ).apply {
            description = "Keeps ShiftMind running while glasses are worn."
            setShowBadge(false)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("ShiftMind")
            .setContentText("Running")
            .setSmallIcon(android.R.drawable.ic_menu_view) // placeholder; swap when assets land
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

    companion object {
        private const val CHANNEL_ID = "shiftmind_glasses_channel"
        private const val NOTIFICATION_ID = 1
        // 4-hour safety cap. The service re-acquires on restart if killed.
        private const val WAKE_LOCK_TIMEOUT_MS = 4L * 60L * 60L * 1000L
    }
}
