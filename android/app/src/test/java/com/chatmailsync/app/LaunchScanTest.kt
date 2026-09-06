package com.chatmailsync.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The launch-time watched-folder scan.
 *
 * What this guards is not the scan but its gate. The scan reads a folder the
 * user pointed us at, and the one switch that says whether they still want it
 * read unattended is the auto-watch toggle. A launch scan that ignored that
 * toggle would be the app doing on startup exactly what the user had turned
 * off -- so the gate is the whole feature, and it is the same gate the
 * periodic work is enqueued under.
 */
class LaunchScanTest {

    @Test
    fun `scans when the watcher is on and a folder is chosen`() {
        assertTrue(shouldScanAtLaunch(true, "content://tree/primary%3AWhatsApp"))
    }

    @Test
    fun `does not scan when auto-watch is switched off`() {
        assertFalse(shouldScanAtLaunch(false, "content://tree/primary%3AWhatsApp"))
    }

    @Test
    fun `does not scan when no folder has been chosen`() {
        assertFalse(shouldScanAtLaunch(true, null))
    }

    @Test
    fun `treats a blank folder uri as no folder`() {
        // Nothing writes one today, but a blank string is what a cleared
        // preference degrades to, and it is not a tree we can open.
        assertFalse(shouldScanAtLaunch(true, "   "))
    }
}
