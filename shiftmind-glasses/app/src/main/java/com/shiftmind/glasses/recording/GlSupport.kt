package com.shiftmind.glasses.recording

import android.graphics.SurfaceTexture
import android.opengl.EGL14
import android.opengl.EGLConfig
import android.opengl.EGLContext
import android.opengl.EGLDisplay
import android.opengl.EGLExt
import android.opengl.EGLSurface
import android.opengl.GLES11Ext
import android.opengl.GLES20
import android.util.Log
import android.view.Surface

/**
 * Thin EGL14 wrappers used by the recording compositor. Kept in its own
 * file so the actual render code in [HudCompositor] stays focused on
 * scene-graph logic instead of EGL plumbing.
 *
 * Pattern follows Google's Grafika project (Apache 2.0): one EglCore per
 * thread holds the display + context + config; WindowSurface ties an
 * EGLSurface to a real producer Surface (the MediaCodec input surface in
 * our case); makeCurrent() rebinds the context before rendering; swap()
 * pushes the rendered frame to the consumer.
 *
 * We do not import Grafika source — the API shape is recreated from public
 * Android EGL docs (developer.android.com/.../EGL14).
 */
internal class EglCore {

    val display: EGLDisplay
    val context: EGLContext
    val config: EGLConfig

    init {
        display = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY)
        check(display !== EGL14.EGL_NO_DISPLAY) { "eglGetDisplay failed" }

        val version = IntArray(2)
        check(EGL14.eglInitialize(display, version, 0, version, 1)) { "eglInitialize failed" }

        // Need: GLES 2.0 (8/8/8/8 RGBA), recordable surface bit for MediaCodec.
        // EGL_RECORDABLE_ANDROID (0x3142) is the Android-specific attribute that
        // tells the EGL surface its target is an encoder; mandatory for MediaCodec
        // surface input on most drivers.
        val attribs = intArrayOf(
            EGL14.EGL_RED_SIZE, 8,
            EGL14.EGL_GREEN_SIZE, 8,
            EGL14.EGL_BLUE_SIZE, 8,
            EGL14.EGL_ALPHA_SIZE, 8,
            EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT,
            EGL_RECORDABLE_ANDROID, 1,
            EGL14.EGL_NONE,
        )
        val configs = arrayOfNulls<EGLConfig>(1)
        val numConfigs = IntArray(1)
        check(EGL14.eglChooseConfig(display, attribs, 0, configs, 0, configs.size, numConfigs, 0)) {
            "eglChooseConfig failed"
        }
        config = configs[0] ?: error("no matching EGL config")

        val ctxAttribs = intArrayOf(EGL14.EGL_CONTEXT_CLIENT_VERSION, 2, EGL14.EGL_NONE)
        context = EGL14.eglCreateContext(display, config, EGL14.EGL_NO_CONTEXT, ctxAttribs, 0)
        check(context !== EGL14.EGL_NO_CONTEXT) { "eglCreateContext failed" }
    }

    fun release() {
        if (display !== EGL14.EGL_NO_DISPLAY) {
            EGL14.eglMakeCurrent(display, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT)
            EGL14.eglDestroyContext(display, context)
            EGL14.eglReleaseThread()
            EGL14.eglTerminate(display)
        }
    }

    companion object {
        // Android EGL extension constant — not in EGL14 because it's vendor-specific.
        private const val EGL_RECORDABLE_ANDROID = 0x3142
    }
}

/**
 * EGLSurface wrapping a real producer Surface (e.g. MediaCodec input). Make
 * it current to render into the encoder, then swap() to publish the frame.
 *
 * The presentation timestamp set via [setPresentationTime] is what
 * MediaCodec uses to stamp the encoded buffer — so feed it the
 * SurfaceTexture's camera timestamp for proper sync.
 */
internal class WindowSurface(
    private val egl: EglCore,
    surface: Surface,
) {
    private val eglSurface: EGLSurface = run {
        val attribs = intArrayOf(EGL14.EGL_NONE)
        EGL14.eglCreateWindowSurface(egl.display, egl.config, surface, attribs, 0).also {
            check(it !== EGL14.EGL_NO_SURFACE) { "eglCreateWindowSurface failed (err=0x${Integer.toHexString(EGL14.eglGetError())})" }
        }
    }

    fun makeCurrent() {
        check(EGL14.eglMakeCurrent(egl.display, eglSurface, eglSurface, egl.context)) {
            "eglMakeCurrent failed (err=0x${Integer.toHexString(EGL14.eglGetError())})"
        }
    }

    /** PTS in nanoseconds — typically `surfaceTexture.timestamp`. */
    fun setPresentationTime(nanos: Long) {
        EGLExt.eglPresentationTimeANDROID(egl.display, eglSurface, nanos)
    }

    fun swap(): Boolean = EGL14.eglSwapBuffers(egl.display, eglSurface)

    fun release() {
        EGL14.eglDestroySurface(egl.display, eglSurface)
    }
}

/**
 * Compiled GL program for drawing a textured quad. One vertex+fragment
 * shader pair per texture type — external OES for camera, plain 2D for HUD.
 */
internal class TexturedQuadProgram(samplerType: SamplerType) {

    enum class SamplerType { EXTERNAL_OES, TEXTURE_2D }

    private val programId: Int
    private val aPositionLoc: Int
    private val aTexCoordLoc: Int
    private val uTexMatrixLoc: Int
    private val uTextureLoc: Int

    init {
        // The #extension directive MUST come first in the fragment shader,
        // before precision and any other statement — GLSL ES is strict about this.
        val (extensionDecl, samplerDecl) = when (samplerType) {
            SamplerType.EXTERNAL_OES -> Pair(
                "#extension GL_OES_EGL_image_external : require\n",
                "uniform samplerExternalOES uTexture;",
            )
            SamplerType.TEXTURE_2D -> Pair(
                "",
                "uniform sampler2D uTexture;",
            )
        }
        val vert = """
            uniform mat4 uTexMatrix;
            attribute vec4 aPosition;
            attribute vec4 aTexCoord;
            varying vec2 vTexCoord;
            void main() {
                gl_Position = aPosition;
                vTexCoord = (uTexMatrix * aTexCoord).xy;
            }
        """.trimIndent()
        val frag = extensionDecl + """
            precision mediump float;
            $samplerDecl
            varying vec2 vTexCoord;
            void main() {
                gl_FragColor = texture2D(uTexture, vTexCoord);
            }
        """.trimIndent()

        programId = linkProgram(vert, frag)
        aPositionLoc = GLES20.glGetAttribLocation(programId, "aPosition")
        aTexCoordLoc = GLES20.glGetAttribLocation(programId, "aTexCoord")
        uTexMatrixLoc = GLES20.glGetUniformLocation(programId, "uTexMatrix")
        uTextureLoc = GLES20.glGetUniformLocation(programId, "uTexture")
    }

    /**
     * Draw quad covering the whole viewport sampled from [textureId] with
     * [texMatrix] applied to texture coords.
     */
    fun draw(
        textureId: Int,
        textureTarget: Int,
        texMatrix: FloatArray,
        vertexBuffer: java.nio.FloatBuffer,
        texCoordBuffer: java.nio.FloatBuffer,
    ) {
        GLES20.glUseProgram(programId)

        GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
        GLES20.glBindTexture(textureTarget, textureId)
        GLES20.glUniform1i(uTextureLoc, 0)

        GLES20.glUniformMatrix4fv(uTexMatrixLoc, 1, false, texMatrix, 0)

        GLES20.glEnableVertexAttribArray(aPositionLoc)
        vertexBuffer.position(0)
        GLES20.glVertexAttribPointer(aPositionLoc, 2, GLES20.GL_FLOAT, false, 0, vertexBuffer)

        GLES20.glEnableVertexAttribArray(aTexCoordLoc)
        texCoordBuffer.position(0)
        GLES20.glVertexAttribPointer(aTexCoordLoc, 2, GLES20.GL_FLOAT, false, 0, texCoordBuffer)

        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)

        GLES20.glDisableVertexAttribArray(aPositionLoc)
        GLES20.glDisableVertexAttribArray(aTexCoordLoc)
        GLES20.glBindTexture(textureTarget, 0)
    }

    fun release() {
        GLES20.glDeleteProgram(programId)
    }

    private fun linkProgram(vertSrc: String, fragSrc: String): Int {
        val vs = compileShader(GLES20.GL_VERTEX_SHADER, vertSrc)
        val fs = compileShader(GLES20.GL_FRAGMENT_SHADER, fragSrc)
        val program = GLES20.glCreateProgram()
        GLES20.glAttachShader(program, vs)
        GLES20.glAttachShader(program, fs)
        GLES20.glLinkProgram(program)
        val status = IntArray(1)
        GLES20.glGetProgramiv(program, GLES20.GL_LINK_STATUS, status, 0)
        if (status[0] == 0) {
            val log = GLES20.glGetProgramInfoLog(program)
            GLES20.glDeleteProgram(program)
            error("link failed: $log")
        }
        GLES20.glDeleteShader(vs)
        GLES20.glDeleteShader(fs)
        return program
    }

    private fun compileShader(type: Int, src: String): Int {
        val shader = GLES20.glCreateShader(type)
        GLES20.glShaderSource(shader, src)
        GLES20.glCompileShader(shader)
        val status = IntArray(1)
        GLES20.glGetShaderiv(shader, GLES20.GL_COMPILE_STATUS, status, 0)
        if (status[0] == 0) {
            val log = GLES20.glGetShaderInfoLog(shader)
            GLES20.glDeleteShader(shader)
            error("compile failed: $log")
        }
        return shader
    }
}

/** Allocate a new GL texture for an external OES (SurfaceTexture) source. */
internal fun createOesTexture(): Int {
    val ids = IntArray(1)
    GLES20.glGenTextures(1, ids, 0)
    GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, ids[0])
    GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR)
    GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
    GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE)
    GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE)
    return ids[0]
}

/** Allocate a regular GL_TEXTURE_2D used for the HUD overlay bitmap. */
internal fun createTexture2D(): Int {
    val ids = IntArray(1)
    GLES20.glGenTextures(1, ids, 0)
    GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, ids[0])
    GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR)
    GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
    GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE)
    GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE)
    return ids[0]
}

internal fun checkGl(tag: String) {
    val err = GLES20.glGetError()
    if (err != GLES20.GL_NO_ERROR) {
        Log.e("ShiftMind.GL", "$tag: glError 0x${Integer.toHexString(err)}")
    }
}
