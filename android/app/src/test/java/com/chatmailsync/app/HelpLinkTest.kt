package com.chatmailsync.app

import androidx.compose.ui.text.LinkAnnotation
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The FAQ's one tappable link.
 *
 * The fragility this guards is not the linkifier, which is six lines — it is
 * that the link is found by matching a hostname against answer prose. Reword
 * the answer that invites the reader to go and check the source, and the link
 * stops existing with nothing failing to say so.
 *
 * Plain JUnit: AnnotatedString is pure Kotlin, and linkify touches no Android
 * framework class and is not a composable.
 */
class HelpLinkTest {

    @Test
    fun `every hostname the map knows still appears in an answer`() {
        val answers = FAQ.map { it.second }
        for (host in FAQ_LINKS.keys) {
            assertTrue(
                "No FAQ answer contains \"$host\" any more, so it links to nothing.",
                answers.any { it.contains(host) },
            )
        }
    }

    @Test
    fun `a hostname in an answer becomes a url annotation`() {
        val host = "github.com/harshalambani/ChatMailSync"
        val built = linkify("the source is public for anyone who wants to check at $host.")

        assertEquals("the source is public for anyone who wants to check at $host.", built.text)

        val links = built.getLinkAnnotations(0, built.length)
        assertEquals(1, links.size)
        val annotation = links.single()
        assertEquals(FAQ_LINKS.getValue(host), (annotation.item as LinkAnnotation.Url).url)
        // The link covers the hostname and nothing either side of it.
        assertEquals(built.text.indexOf(host), annotation.start)
        assertEquals(built.text.indexOf(host) + host.length, annotation.end)
    }

    @Test
    fun `an answer with no hostname comes through unchanged and unlinked`() {
        val plain = "It can't read your existing mail."
        val built = linkify(plain)
        assertEquals(plain, built.text)
        assertTrue(built.getLinkAnnotations(0, built.length).isEmpty())
    }
}
