package com.shiftmind.glasses.recording

import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals

class RokidMicCaptureTest {
    @Test
    fun extractsFourthChannelAsLittleEndianMono() {
        val interleaved = shortArrayOf(
            1, 2, 3, 0x1234,
            5, 6, 7, -2,
        )
        val output = ByteArray(4)

        val bytesWritten = RokidMicCapture.extractSelectedChannel(
            interleaved,
            interleaved.size,
            output,
        )

        assertEquals(4, bytesWritten)
        assertContentEquals(byteArrayOf(0x34, 0x12, 0xfe.toByte(), 0xff.toByte()), output)
    }

    @Test
    fun ignoresIncompleteTrailingFrame() {
        val interleaved = shortArrayOf(1, 2, 3, 4, 5, 6)
        val output = ByteArray(4)

        val bytesWritten = RokidMicCapture.extractSelectedChannel(
            interleaved,
            interleaved.size,
            output,
        )

        assertEquals(2, bytesWritten)
        assertContentEquals(byteArrayOf(4, 0, 0, 0), output)
    }
}
