package com.shiftmind.glasses

import android.app.Application

/**
 * Phase-1 Application class. We deliberately do NOT call startForegroundService()
 * here — Android 12+ rejects FGS starts from background contexts, and
 * Application.onCreate runs before any activity is foregrounded. The service
 * is started from MainActivity.onCreate instead, where the activity itself
 * supplies the foreground context that AMS requires.
 */
class GlassesApplication : Application()

