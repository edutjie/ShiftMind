import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

/**
 * Read per-developer config from local.properties (git-ignored, Android-standard).
 * Falls back to the bundled defaults in local.properties.example if missing.
 * Both files live at the module root (shiftmind-glasses/).
 */
val localProps: Properties = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

fun localProp(key: String, default: String): String =
    localProps.getProperty(key)?.takeIf { it.isNotBlank() } ?: default

android {
    namespace = "com.shiftmind.glasses"
    // Per rokid_android_dev_guide.md §10: Rokid Max Pro requires minSdk 28.
    compileSdk = 36

    defaultConfig {
        applicationId = "com.shiftmind.glasses"
        minSdk = 28
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"

        // Per-developer config — set these in local.properties (NOT committed):
        //   shiftmind.serverUrl = http://192.168.1.50:8000
        //   shiftmind.previewFps = 5
        // Find your laptop IP: macOS `ipconfig getifaddr en0`, linux `hostname -I`.
        val serverUrl = localProp("shiftmind.serverUrl", "http://10.0.0.10:8000")
        val previewFps = localProp("shiftmind.previewFps", "5")
        // KeyEvent.KEYCODE_ENTER = 66 (touchpad single-tap on Rokid RG-glasses).
        // DO NOT default to KEYCODE_MENU (82) — that's the Rokid power button and
        // the native photo shortcut intercepts it before the app sees it.
        // Override with shiftmind.recordingKey=NNN in local.properties; every
        // keycode received is logged at INFO under `ShiftMind.Main`.
        val recordingKey = localProp("shiftmind.recordingKey", "66")
        // Separate key to stop the active customer voice/session flow. Keep
        // this different from shiftmind.recordingKey so AR recording can cover
        // the whole experiment while sessions start/stop inside it.
        val sessionKey = localProp("shiftmind.sessionKey", "23")
        // Extra rotation (degrees, multiple of 90) applied to camera texture.
        // Default -90 matches server preview: raw 640x480 sensor -> upright
        // 480x640 recording. If future hardware differs, try 0 / 90 / 180.
        val recordingRotation = localProp("shiftmind.recordingRotation", "-90")
        buildConfigField("String", "SERVER_URL", "\"$serverUrl\"")
        buildConfigField("int", "PREVIEW_FPS", previewFps)
        buildConfigField("int", "RECORDING_KEY", recordingKey)
        buildConfigField("int", "SESSION_KEY", sessionKey)
        buildConfigField("int", "RECORDING_ROTATION_DEG", recordingRotation)
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
        debug {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true   // AGP 8+ requires explicit opt-in for BuildConfig generation
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.kotlinx.coroutines.android)

    // Compose
    val composeBom = platform(libs.androidx.compose.bom)
    implementation(composeBom)
    implementation(libs.androidx.ui)
    implementation(libs.androidx.ui.graphics)
    implementation(libs.androidx.ui.tooling.preview)
    implementation(libs.androidx.material3)
    debugImplementation(libs.androidx.ui.tooling)

    // OkHttp: streaming JPEGs to the laptop preview endpoint.
    implementation(libs.okhttp)

    // ML Kit: on-device lightweight face detection for cursor gating.
    implementation(libs.mlkit.face.detection)

    testImplementation(kotlin("test"))
}
