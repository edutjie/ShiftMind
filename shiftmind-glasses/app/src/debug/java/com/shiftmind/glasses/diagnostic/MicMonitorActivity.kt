package com.shiftmind.glasses.diagnostic

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Bundle
import android.util.Log
import android.view.Gravity
import android.view.WindowManager
import android.widget.TextView
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * Debug-only raw microphone monitor launched by tools/live-mic.sh.
 *
 * Captures 16 kHz PCM from a selectable logical input. Mono capture is sent
 * directly; quad capture sends one selected array channel as mono so every
 * physical/HAL channel can be auditioned independently on the laptop.
 */
class MicMonitorActivity : Activity() {
    private val running = AtomicBoolean(false)
    private var recorder: AudioRecord? = null
    private var socket: Socket? = null
    private var worker: Thread? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setShowWhenLocked(true)
        setTurnScreenOn(true)

        val deviceMode = intent.getStringExtra(EXTRA_DEVICE)?.lowercase() ?: "back"
        val channelMode = intent.getStringExtra(EXTRA_CHANNEL)?.lowercase() ?: "mono"
        val port = intent.getIntExtra(EXTRA_PORT, DEFAULT_PORT)
        showStatus("MIC MONITOR\n$deviceMode / $channelMode\nStreaming raw PCM to laptop\nCtrl-C there to stop")

        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            fail("RECORD_AUDIO permission is not granted")
            return
        }
        if (deviceMode !in DEVICE_MODES || !isValidChannelMode(channelMode)) {
            fail("invalid mode: device=$deviceMode channel=$channelMode")
            return
        }

        running.set(true)
        worker = thread(name = "ShiftMindMicMonitor") {
            stream(deviceMode, channelMode, port)
        }
    }

    override fun onDestroy() {
        running.set(false)
        runCatching { socket?.close() }
        runCatching { recorder?.stop() }
        worker?.join(500)
        runCatching { recorder?.release() }
        recorder = null
        socket = null
        worker = null
        super.onDestroy()
    }

    @SuppressLint("MissingPermission")
    private fun stream(deviceMode: String, channelMode: String, port: Int) {
        val isQuad = channelMode.startsWith("quad")
        val channels = if (isQuad) 4 else 1
        val selectedChannel = if (isQuad) channelMode.removePrefix("quad").toInt() - 1 else 0
        val channelMask = if (isQuad) QUAD_IN_MASK else AudioFormat.CHANNEL_IN_MONO

        try {
            val minBuffer = AudioRecord.getMinBufferSize(SAMPLE_RATE, channelMask, PCM_FORMAT)
            check(minBuffer > 0) { "unsupported capture config: getMinBufferSize=$minBuffer" }
            val format = AudioFormat.Builder()
                .setSampleRate(SAMPLE_RATE)
                .setEncoding(PCM_FORMAT)
                .setChannelMask(channelMask)
                .build()
            val rec = AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.MIC)
                .setAudioFormat(format)
                .setBufferSizeInBytes(minBuffer * 4)
                .build()
            check(rec.state == AudioRecord.STATE_INITIALIZED) { "AudioRecord initialization failed" }
            recorder = rec

            logAvailableInputs()
            val preferred = findInput(deviceMode)
            if (preferred != null) {
                val accepted = rec.setPreferredDevice(preferred)
                Log.i(TAG, "preferred device: mode=$deviceMode id=${preferred.id} address=${preferred.address} accepted=$accepted")
            } else {
                Log.i(TAG, "preferred device: mode=default (HAL routing)")
            }

            val connection = Socket(HOST, port).apply { tcpNoDelay = true }
            socket = connection
            val output = connection.getOutputStream()
            rec.startRecording()
            val routed = rec.routedDevice
            Log.i(
                TAG,
                "STREAMING device=$deviceMode channel=$channelMode actualId=${routed?.id} " +
                    "type=${routed?.type} address=${routed?.address} product=${routed?.productName} " +
                    "preferredId=${rec.preferredDevice?.id}",
            )

            val input = ShortArray(FRAMES_PER_CHUNK * channels)
            val monoBytes = ByteArray(FRAMES_PER_CHUNK * 2)
            while (running.get()) {
                val samplesRead = rec.read(input, 0, input.size, AudioRecord.READ_BLOCKING)
                if (samplesRead <= 0) {
                    Log.w(TAG, "AudioRecord.read returned $samplesRead")
                    break
                }
                val framesRead = samplesRead / channels
                for (frame in 0 until framesRead) {
                    val sample = input[frame * channels + selectedChannel].toInt()
                    val offset = frame * 2
                    monoBytes[offset] = (sample and 0xff).toByte()
                    monoBytes[offset + 1] = ((sample shr 8) and 0xff).toByte()
                }
                output.write(monoBytes, 0, framesRead * 2)
            }
        } catch (t: Throwable) {
            if (running.get()) Log.e(TAG, "monitor failed: ${t.message}", t)
        } finally {
            running.set(false)
            runCatching { recorder?.stop() }
            runCatching { recorder?.release() }
            runCatching { socket?.close() }
            recorder = null
            socket = null
            runOnUiThread { if (!isFinishing) finish() }
        }
    }

    private fun findInput(mode: String): AudioDeviceInfo? {
        if (mode == "default") return null
        val inputs = builtInInputs()
        return inputs.firstOrNull { it.address.equals(mode, ignoreCase = true) }
            ?: when (mode) {
                "bottom" -> inputs.firstOrNull { it.id == 21 } ?: inputs.firstOrNull()
                "back" -> inputs.firstOrNull { it.id == 23 } ?: inputs.getOrNull(1)
                else -> null
            }
    }

    private fun builtInInputs(): List<AudioDeviceInfo> {
        val manager = getSystemService(AudioManager::class.java) ?: return emptyList()
        return manager.getDevices(AudioManager.GET_DEVICES_INPUTS)
            .filter { it.isSource && it.type == AudioDeviceInfo.TYPE_BUILTIN_MIC }
    }

    private fun logAvailableInputs() {
        builtInInputs().forEach { input ->
            Log.i(
                TAG,
                "input id=${input.id} type=${input.type} address=${input.address} " +
                    "product=${input.productName} channels=${input.channelCounts.toList()} " +
                    "masks=${input.channelMasks.map { "%#x".format(it) }}",
            )
        }
    }

    private fun showStatus(message: String) {
        setContentView(TextView(this).apply {
            text = message
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.BLACK)
            textSize = 22f
            gravity = Gravity.CENTER
            setPadding(32, 32, 32, 32)
        })
    }

    private fun fail(message: String) {
        Log.e(TAG, message)
        showStatus("MIC MONITOR ERROR\n$message")
        running.set(false)
    }

    companion object {
        private const val TAG = "ShiftMind.MicMonitor"
        private const val EXTRA_DEVICE = "device"
        private const val EXTRA_CHANNEL = "channel"
        private const val EXTRA_PORT = "port"
        private const val HOST = "127.0.0.1"
        private const val DEFAULT_PORT = 18765
        private const val SAMPLE_RATE = 16_000
        private const val FRAMES_PER_CHUNK = 320 // 20 ms
        private const val PCM_FORMAT = AudioFormat.ENCODING_PCM_16BIT
        private val DEVICE_MODES = setOf("default", "bottom", "back")
        private val QUAD_IN_MASK = AudioFormat.CHANNEL_IN_LEFT or
            AudioFormat.CHANNEL_IN_RIGHT or
            AudioFormat.CHANNEL_IN_FRONT or
            AudioFormat.CHANNEL_IN_BACK

        private fun isValidChannelMode(mode: String): Boolean =
            mode == "mono" || mode in setOf("quad1", "quad2", "quad3", "quad4")
    }
}
