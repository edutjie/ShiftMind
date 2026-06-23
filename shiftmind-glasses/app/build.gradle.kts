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
        buildConfigField("String", "SERVER_URL", "\"$serverUrl\"")
        buildConfigField("int", "PREVIEW_FPS", previewFps)
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

    // CameraX: ImageAnalysis use case captures YUV frames in a background thread.
    implementation(libs.androidx.camera.core)
    implementation(libs.androidx.camera.camera2)
    implementation(libs.androidx.camera.lifecycle)

    // OkHttp: streaming JPEGs to the laptop preview endpoint.
    implementation(libs.okhttp)
}
