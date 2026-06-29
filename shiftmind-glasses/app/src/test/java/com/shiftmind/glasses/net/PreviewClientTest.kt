package com.shiftmind.glasses.net

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class PreviewClientTest {
    @Test
    fun `active employee updates both HUD fields`() {
        val update = resolveEmployeeHudUpdate(true, "Sofia Kim", "Flagship")

        assertEquals("Sofia Kim", update.employeeName)
        assertEquals("Flagship", update.locationName)
    }

    @Test
    fun `logout clears stale HUD fields`() {
        val update = resolveEmployeeHudUpdate(false, "ignored", "ignored")

        assertNull(update.employeeName)
        assertNull(update.locationName)
    }

    @Test
    fun `active employee maps blank and null names to null`() {
        val update = resolveEmployeeHudUpdate(true, "", null)

        assertNull(update.employeeName)
        assertNull(update.locationName)
    }
}
