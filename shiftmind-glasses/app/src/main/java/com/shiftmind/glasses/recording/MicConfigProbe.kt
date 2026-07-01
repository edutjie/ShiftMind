package com.shiftmind.glasses.recording

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import kotlin.math.abs
import kotlin.math.log10
import kotlin.math.sqrt

/**
 * TEMP DIAGNOSTIC — delete once the runtime mic config is locked.
 *
 * Rokid "RG-glasses" exposes a 4-mic array (Built-In Mic + Built-In Back Mic +
 * a dedicated "quad mic" input) but runs Qualcomm Fluence beamforming toward the
 * wearer, so the default MIC+MONO capture rejects far-field customer speech.
 *
 * This probe opens each candidate AudioRecord config for ~2 s at app start
 * (BEFORE the camera opens — per CLAUDE.md, alternate sources/channel masks must
 * be probed before Camera2 or the HAL can wedge) and logs per-channel RMS/peak
 * dBFS under tag `ShiftMind.MicProbe`. With a far-field speaker (customer) and
 * the wearer speaking, the per-channel spread tells us which config actually
 * hears the customer and whether the HAL returns N distinct channels or
 * duplicates one mic.
 *
 * Gated by BuildConfig.AUDIO_PROBE; launched from MainActivity.onCreate.
 */
class MicConfigProbe(private val context: Context) {

    private data class Candidate(
        val label: String,
        val source: Int,
        val channelMask: Int,
        val channels: Int,
        val preferred: AudioDeviceInfo? = null,
    )

    /** Per-channel metering result for one candidate. */
    private data class Result(
        val label: String,
        val channels: Int,
        val perChannelDbfs: List<Double>,
        val distinctSpreadDb: Double,
    )

    fun run() {
        val am = context.getSystemService(AudioManager::class.java) ?: run {
            Log.e(TAG, "AudioManager unavailable — probe aborted")
            return
        }
        val mics = am.getDevices(AudioManager.GET_DEVICES_ALL)
            .filter { it.isSource && it.type == AudioDeviceInfo.TYPE_BUILTIN_MIC }
        logDevices(mics)

        val front = mics.firstOrNull { it.productName.contains("built-in mic", ignoreCase = true) && !it.productName.contains("back", ignoreCase = true) }
            ?: mics.getOrNull(0)
        val back = mics.firstOrNull { it.productName.contains("back", ignoreCase = true) } ?: mics.getOrNull(1)
        // Dedicated quad-array device, if the HAL exposes one separately.
        val quadDevice = mics.firstOrNull {
            it.productName.contains("quad", ignoreCase = true) ||
                it.channelCounts.any { c -> c >= 4 } ||
                it.channelMasks.any { m -> java.lang.Integer.bitCount(m) >= 4 }
        }

        val candidates = buildList {
            add(Candidate("a  MIC/MONO", MediaRecorder.AudioSource.MIC, AudioFormat.CHANNEL_IN_MONO, 1))
            add(Candidate("b  MIC/STEREO", MediaRecorder.AudioSource.MIC, AudioFormat.CHANNEL_IN_STEREO, 2))
            // AudioFormat has no public CHANNEL_IN_QUAD; assemble a 4-bit input
            // mask (LEFT|RIGHT|FRONT|BACK) so the policy manager can route to a
            // 4-channel input if the HAL exposes one.
            add(Candidate("c  MIC/QUAD", MediaRecorder.AudioSource.MIC, QUAD_IN_MASK, 4))
            if (quadDevice != null) {
                add(Candidate("c2 MIC/QUAD+quadDev", MediaRecorder.AudioSource.MIC, QUAD_IN_MASK, 4, preferred = quadDevice))
            } else {
                Log.w(TAG, "no dedicated quad-array AudioDeviceInfo found — skipping c2")
            }
            add(Candidate("d  MIC/MONO+backMic", MediaRecorder.AudioSource.MIC, AudioFormat.CHANNEL_IN_MONO, 1, preferred = back))
            add(Candidate("e  MIC/MONO+frontMic", MediaRecorder.AudioSource.MIC, AudioFormat.CHANNEL_IN_MONO, 1, preferred = front))
            add(Candidate("f  VOICE_COMM/MONO", MediaRecorder.AudioSource.VOICE_COMMUNICATION, AudioFormat.CHANNEL_IN_MONO, 1))
            add(Candidate("g  DEFAULT/MONO", MediaRecorder.AudioSource.DEFAULT, AudioFormat.CHANNEL_IN_MONO, 1))
        }

        Log.i(TAG, "=== START: ${candidates.size} configs × ${DURATION_MS}ms each. Speak (wearer) + play far-field speaker NOW ===")
        val results = mutableListOf<Result?>()
        for (cand in candidates) {
            val r = probe(cand)
            results.add(r)
        }
        summarize(results.filterNotNull())
        Log.i(TAG, "=== DONE ===")
    }

    @SuppressLint("MissingPermission") // probe only launched after RECORD_AUDIO granted in MainActivity
    private fun probe(cand: Candidate): Result? {
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, cand.channelMask, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) {
            Log.w(TAG, "[${cand.label}] unsupported: getMinBufferSize=$minBuf (mask ${"%#x".format(cand.channelMask)})")
            return null
        }
        val rec = try {
            val fmt = AudioFormat.Builder()
                .setSampleRate(SAMPLE_RATE)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setChannelMask(cand.channelMask)
                .build()
            val r = AudioRecord.Builder()
                .setAudioSource(cand.source)
                .setAudioFormat(fmt)
                .setBufferSizeInBytes(minBuf * 4)
                .build()
            if (r.state != AudioRecord.STATE_INITIALIZED) {
                Log.w(TAG, "[${cand.label}] init failed state=${r.state}")
                try { r.release() } catch (_: Throwable) {}
                return null
            }
            val devName = cand.preferred?.productName
            if (cand.preferred != null) {
                val ok = r.setPreferredDevice(cand.preferred)
                Log.i(TAG, "[${cand.label}] setPreferredDevice(${devName}) -> $ok")
            }
            r
        } catch (t: Throwable) {
            Log.w(TAG, "[${cand.label}] open threw: ${t.javaClass.simpleName}: ${t.message}")
            return null
        }

        return try {
            rec.startRecording()
            val chans = cand.channels
            val shorts = ShortArray((minBuf / 2).coerceAtLeast(SAMPLE_RATE * chans / 2))
            val sumSq = DoubleArray(chans)
            val peak = IntArray(chans)
            var frames = 0L
            val deadline = System.nanoTime() + DURATION_NS
            while (System.nanoTime() < deadline) {
                val n = rec.read(shorts, 0, shorts.size)
                if (n <= 0) continue
                val frameCount = n / chans
                for (f in 0 until frameCount) {
                    for (c in 0 until chans) {
                        val s = shorts[f * chans + c].toInt()
                        sumSq[c] += s.toDouble() * s
                        if (abs(s) > peak[c]) peak[c] = abs(s)
                    }
                }
                frames += frameCount
            }
            val perChDbfs = (0 until chans).map { c ->
                val rms = if (frames > 0) sqrt(sumSq[c] / frames) else 0.0
                if (rms > 0) 20.0 * log10(rms / Short.MAX_VALUE) else -96.0
            }
            val maxR = perChDbfs.maxOrNull() ?: -96.0
            val minR = perChDbfs.minOrNull() ?: -96.0
            val spread = (maxR - minR)
            val chStr = perChDbfs.joinToString(" ") { "%.1f".format(it) }
            Log.i(TAG, "[${cand.label}] frames=$frames perCh(dBFS)=[$chStr] spread=%.1fdB distinct=${spread > DISTINCT_THRESHOLD_DB}".format(spread))
            Result(cand.label, chans, perChDbfs, spread)
        } catch (t: Throwable) {
            Log.w(TAG, "[${cand.label}] read threw: ${t.message}")
            null
        } finally {
            try { rec.stop() } catch (_: Throwable) {}
            rec.release()
        }
    }

    private fun summarize(results: List<Result>) {
        if (results.isEmpty()) {
            Log.w(TAG, "no usable configs")
            return
        }
        // Rank by the loudest channel captured (a config that hears the far-field
        // customer shows a loud channel; wearer also contributes, so this is a
        // rough guide — read the per-channel lines above for the real decision).
        val ranked = results.sortedByDescending { it.perChannelDbfs.maxOrNull() ?: -96.0 }
        Log.i(TAG, "---- RANKING by max-channel dBFS (loudest capture) ----")
        ranked.forEach { r ->
            val chStr = r.perChannelDbfs.joinToString(" ") { "%.1f".format(it) }
            Log.i(TAG, "  ${r.label}: perCh=[$chStr] spread=%.1fdB distinct=${r.distinctSpreadDb > DISTINCT_THRESHOLD_DB}".format(r.distinctSpreadDb))
        }
        val best = ranked.first()
        val quadCandidates = results.filter { it.channels >= 4 && it.distinctSpreadDb > DISTINCT_THRESHOLD_DB }
        Log.i(TAG, "WINNER(loudest)=${best.label} perChMax=%.1fdBFS".format(best.perChannelDbfs.maxOrNull() ?: -96.0))
        if (quadCandidates.isNotEmpty()) {
            Log.i(TAG, "QUAD configs with distinct channels: ${quadCandidates.joinToString { "${it.label}(spread=%.1f)".format(it.distinctSpreadDb) }} — preferred if customer sits on a non-dominant channel")
        } else {
            Log.i(TAG, "no QUAD config returned distinct channels — HAL is duplicating one mic; favor back-mic/single-channel route")
        }
    }

    private fun logDevices(mics: List<AudioDeviceInfo>) {
        if (mics.isEmpty()) {
            Log.w(TAG, "no TYPE_BUILTIN_MIC source devices found")
            return
        }
        Log.i(TAG, "BUILTIN_MIC source devices (${mics.size}):")
        mics.forEach { d ->
            Log.i(TAG, "  id=${d.id} name='${d.productName}' chCounts=${d.channelCounts.toList()} chMasks=${d.channelMasks.map { "%#x".format(it) }}")
        }
    }

    companion object {
        private const val TAG = "ShiftMind.MicProbe"
        private const val SAMPLE_RATE = 16000
        private const val DURATION_MS = 2000L
        private const val DURATION_NS = DURATION_MS * 1_000_000L
        // If max−min channel RMS is below this, the HAL is feeding the same mic
        // into every channel → no array benefit.
        private const val DISTINCT_THRESHOLD_DB = 0.5
        // 4-channel input mask (LEFT|RIGHT|FRONT|BACK) — there is no public
        // AudioFormat.CHANNEL_IN_QUAD constant, so build the mask by hand.
        private val QUAD_IN_MASK = AudioFormat.CHANNEL_IN_LEFT or
            AudioFormat.CHANNEL_IN_RIGHT or
            AudioFormat.CHANNEL_IN_FRONT or
            AudioFormat.CHANNEL_IN_BACK
    }
}
