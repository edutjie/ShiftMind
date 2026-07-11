package com.shiftmind.glasses.recording

import android.media.AudioFormat

/**
 * Shared production capture configuration selected by live audition on the
 * RG-glasses: default MIC route, four-channel input, array channel 4.
 *
 * Downstream consumers remain 16 kHz mono. Keeping extraction here prevents
 * live ASR and AR recording from silently drifting to different microphones.
 */
internal object RokidMicCapture {
    const val INPUT_CHANNEL_COUNT = 4
    const val SELECTED_CHANNEL_INDEX = 3 // live-mic.sh `default quad4`
    const val FRAMES_PER_CHUNK = 320 // 20 ms at 16 kHz

    val channelMask: Int = AudioFormat.CHANNEL_IN_LEFT or
        AudioFormat.CHANNEL_IN_RIGHT or
        AudioFormat.CHANNEL_IN_FRONT or
        AudioFormat.CHANNEL_IN_BACK

    fun audioFormat(sampleRate: Int): AudioFormat = AudioFormat.Builder()
        .setSampleRate(sampleRate)
        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
        .setChannelMask(channelMask)
        .build()

    fun selectedSample(input: ShortArray, frame: Int): Short =
        input[frame * INPUT_CHANNEL_COUNT + SELECTED_CHANNEL_INDEX]

    /** Extract channel 4 and encode it as little-endian mono PCM. */
    fun extractSelectedChannel(input: ShortArray, samplesRead: Int, output: ByteArray): Int {
        val frameCount = minOf(samplesRead / INPUT_CHANNEL_COUNT, output.size / 2)
        for (frame in 0 until frameCount) {
            val sample = selectedSample(input, frame).toInt()
            val offset = frame * 2
            output[offset] = (sample and 0xff).toByte()
            output[offset + 1] = ((sample shr 8) and 0xff).toByte()
        }
        return frameCount * 2
    }
}
