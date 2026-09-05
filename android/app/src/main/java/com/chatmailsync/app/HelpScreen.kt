@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package com.chatmailsync.app

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withLink
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

// Kept in sync with the Windows edition's help.html by hand for now — a
// shared-markdown generator (per the screen-guides doc §9) is future work,
// not justified for this single FAQ screen yet. The questions, and their
// order, are the same on all three surfaces (this screen, help.html and
// docs/user-guide.md); only the answers are written per platform.
internal val FAQ = listOf(
    "How do I export a chat from WhatsApp?" to
        "Open the chat in WhatsApp -> tap the three-dot menu -> More -> Export chat. " +
        "Choose \"Include media\" for a .zip with photos/videos, or \"Without media\" for a plain .txt. " +
        "Then share it to this app (or use \"Import a WhatsApp export\" on Home). " +
        "One chat at a time: WhatsApp has no multi-select export, so you cannot tick several chats " +
        "and export them together — each one has to be exported from inside that chat. This app has " +
        "no such limit; once the files are here you can import and sync as many as you like at once.",
    "Why doesn't the app \"send\" my messages anywhere?" to
        "It uses IMAP APPEND, the mail command that adds a message straight into a mailbox — the " +
        "same one your mail app uses to save a draft. Nothing is sent, no sending quota is used, " +
        "and nobody else receives these emails: they only appear in your own mailbox.",
    "What can the app reach in my email account?" to
        "Nothing outside the mailbox. What you hand it is an app password, and that's a mail " +
        "credential rather than an account one — it works with the mail protocols and nothing " +
        "else. It can't sign in to your provider's website or app, can't reach your files, " +
        "photos, contacts or calendar, can't see or change your account settings, and can't " +
        "change your real password. You create it separately from your normal password, and you " +
        "can revoke it on its own, from your provider, whenever you like — that ends this " +
        "app's access without disturbing anything else you use. What it can do inside the " +
        "mailbox is the next question.",
    "What stops it reading or deleting my mail?" to
        "Inside the mailbox, the app's own code — and it is worth being plain about that. No " +
        "provider can give out a limited password: any credential that can add mail can " +
        "technically read and remove it too. What the app does with it is use four commands and " +
        "no others — list folders, create a folder, subscribe to it, and add a message. None " +
        "of them can open or remove a message, the app never puts a folder into the state where " +
        "that would even be possible, and the source is public for anyone who wants to check " +
        "at github.com/harshalambani/ChatMailSync.",
    "Where is my email app password kept?" to
        "Encrypted on this device, with a key held in the Android Keystore that never leaves the " +
        "phone's secure hardware. It's never written into the app's settings, never shown in the " +
        "password box again after you save it, and never included in any log or error message. " +
        "(The Windows edition does the same thing with Windows DPAPI, tied to your Windows account " +
        "on that PC.)",
    "The app says \"Not connected\", or authorising fails." to
        "Open Settings -> Mail account and check the host, port and email address, then enter " +
        "your app password again and save. The app password is the usual culprit — providers " +
        "revoke it if you turn off two-factor authentication, and some expire it on their own.",
    "Why doesn't it just sign in with Google or Microsoft?" to
        "Because that route is only open to apps the provider has formally reviewed, and the " +
        "review isn't free. Google caps an unreviewed app at 100 users and expires its sign-in " +
        "roughly every 7 days; getting past that means going through Google's verification, " +
        "which for anything touching Gmail needs a security assessment by an approved third " +
        "party — paid for, and repeated every year. Microsoft, Yahoo and Apple each have their " +
        "own registration and review to satisfy separately: it's one process per provider, not " +
        "one piece of work. Chat Mail Sync is free and takes no money from anyone, so there's " +
        "nothing to fund that with. An app password works on every provider in the list, doesn't " +
        "expire, and can be revoked on its own. The honest trade-off is that you type a secret " +
        "into the app instead of tapping a consent screen, which is why two of the questions " +
        "above set out exactly what that secret can and can't touch. One consequence worth " +
        "knowing: Microsoft has switched basic authentication off for work and school " +
        "(Microsoft 365) mailboxes, so an app password is refused there; personal Outlook.com " +
        "accounts are fine.",
    "I moved the app to another PC, or set it up on a new phone, and it wants the password again." to
        "That's expected, not a fault. The saved password is encrypted with a key tied to this " +
        "device's Keystore, so it doesn't travel to a new phone and doesn't survive uninstalling " +
        "the app — which is also why nobody who picks up the phone's files can read it. The " +
        "password is still valid at your provider: enter it again in Settings -> Mail account. " +
        "The same is true of the Windows edition on a new PC or a different Windows user.",
    "My file doesn't show up in the inbox." to
        "Only .txt and .zip files are accepted. Make sure you exported the chat itself (not a " +
        "screenshot or a contact card), and that the share or the import actually completed — a " +
        "share sheet dismissed early leaves nothing behind. Pull the Home list to refresh if you " +
        "imported from another app.",
    "My photos and files didn't come through." to
        "You probably exported \"Without media\". Re-export the chat choosing \"Include media\" " +
        "(this produces a .zip), then import and sync that file.",
    "Emoji reactions on messages didn't come through." to
        "WhatsApp keeps them, but it doesn't put them in the file it exports. Export chat writes " +
        "one line per message — a time, a sender and the text — and WhatsApp has chosen not " +
        "to give reactions a line of their own, so they never leave the app. Your own WhatsApp " +
        "backup does carry reactions; the export is a deliberately simpler format. That exported " +
        "file is the only thing Chat Mail Sync is ever given, so there's nothing in it to import " +
        "— and no setting on either side changes that. An emoji somebody sent as a message of " +
        "its own comes through normally; it's only the tap-and-hold reaction on someone else's " +
        "message that the export leaves behind. The same goes for anything else Export chat " +
        "omits: the app can never show more than WhatsApp put in the file.",
    "What is the watched folder for?" to
        "It saves you importing by hand. Point it at a folder, and anything WhatsApp drops there " +
        "(.txt or .zip) is picked up and queued for the next sync. Switch on \"Auto-import from " +
        "this folder\" and it checks on its own on the interval you choose; leave it off and it " +
        "only looks when you tap \"Check and sync\". Only that one folder is looked at — subfolders are " +
        "left alone — and a file is only ever picked up once, so re-checking costs you nothing. " +
        "Your original is never touched at import time; the \"After import\" setting only takes " +
        "effect once the file has actually reached your mailbox, so a sync that fails or that you " +
        "stop leaves everything where it was. The Windows edition has the same feature with one " +
        "difference worth knowing: it can only check while the app is open, and its \"delete\" " +
        "option sends the file to the Recycle Bin rather than erasing it.",
    "How do I make it sync on a schedule?" to
        "The watched folder is the schedule — there is no separate \"sync every N hours\" setting, " +
        "because with nothing new in the inbox there would be nothing to do. Point Settings -> " +
        "Watched folder at a folder, turn on \"Auto-import from this folder\", and pick an interval: " +
        "every 15 min (the default), 30 min, hour, 3 hours, 6 hours, 12 hours, or once a day. " +
        "There is no shorter option than 15 minutes: Android's background scheduler enforces that " +
        "floor and no app can go under it. Treat the interval as \"no more often than\" rather than " +
        "\"exactly\" — the system delays and batches background work to save power, so hourly means " +
        "roughly hourly. The scan needs no network; the sync it triggers does, so an offline tick " +
        "imports the files and leaves them in the inbox for the next run. \"Check and sync\" always runs " +
        "immediately regardless of the schedule. Unlike the Windows edition, this one keeps checking " +
        "with the app closed — that is what the system scheduler is for.",
    "The schedule stopped running on its own. Why?" to
        "Almost always battery optimisation: Android has put the app to sleep, and a sleeping app " +
        "gets no background ticks. Exempt it once. Stock Android: Settings -> Apps -> Chat Mail " +
        "Sync -> Battery -> Unrestricted. Samsung, which is stricter and will otherwise stop the " +
        "schedule within a day or two of non-use: Settings -> Battery -> Background usage limits — " +
        "make sure the app is not under \"Sleeping apps\" or \"Deep sleeping apps\", add it to " +
        "\"Never sleeping apps\", and turn off \"Put unused apps to sleep\" if you open the app " +
        "rarely. Xiaomi, Oppo, Vivo and OnePlus also keep a separate \"Autostart\" permission — " +
        "without it background work stops after a reboot. None of this affects manual syncs or " +
        "\"Check and sync\", which run in the foreground while you are watching; it only affects " +
        "unattended checks. (On Windows the equivalent limit is simpler: the check only runs while " +
        "the app is open.)",
    "Will I get duplicate messages if I sync the same chat again?" to
        "Every message is fingerprinted (hashed). Re-syncing the same file, or a fresh export that " +
        "overlaps an earlier one, skips anything already pushed — nothing is duplicated in your " +
        "mailbox. That record belongs to this instance of the app, though, not to your mailbox — " +
        "see the next answer.",
    "Can two instances of the app archive into the same mailbox?" to
        "They can, but they will not know about each other and you will get duplicates. This is not " +
        "about Android versus Windows: any two instances behave this way — two phones, two PCs, one " +
        "of each, or two copies of the portable Windows app in different folders. The record of what " +
        "has been archived belongs to the instance that did the archiving; nothing about it is stored " +
        "in the mailbox. A second instance signed in to the same account starts from zero knowledge " +
        "and re-files every chat you give it. This app can add mail but never remove it, so clearing " +
        "the duplicates afterwards is manual work. Use one instance per mailbox, or give each its own " +
        "account. Replacing an instance is a different case: carry the sync state across and the new " +
        "one continues where the old one stopped -- see the next answer.",
    "I am moving to a new PC or phone. How do I take my history with me?" to
        "Settings -> Backup & restore -> \"Save a backup\". It writes a small file holding the " +
        "record of what has already been sent, plus your preferences; pick anywhere you like to " +
        "put it, and get it to the new phone however you normally move a file. On the new phone, " +
        "install the app and use \"Restore from a backup\" in the same place before your first " +
        "sync. Your chats themselves are not in that file and do not need to be: they are already " +
        "in your mailbox, which is the archive. What the backup saves you is a second copy of all " +
        "of them landing there. Your mail password is deliberately not included, so the new phone " +
        "asks for it once. A backup taken on Windows restores on Android and the other way round, " +
        "and restoring merges rather than replaces, so a restore onto a phone that has already " +
        "synced something keeps both sides. Restoring the same backup twice does nothing the " +
        "second time. You will still need to re-import the export files you want to keep syncing " +
        "from.",
    "What happens if I reinstall the app or reset my device?" to
        "Your chats are safe either way: they are in your mailbox, and nothing that happens on this " +
        "phone can take them out of it. What a reset, an uninstall or \"Clear data\" destroys is the " +
        "record of what has already been sent -- and without that record the app mails every chat a " +
        "second time, into a mailbox that has no way to tell the copies apart. Two things stand " +
        "between you and that. Android's own backup now includes this app, so a restore from Google " +
        "One or Smart Switch brings the record back by itself. And Settings -> Backup & restore -> " +
        "\"Save a backup\" writes the same record to a file you keep yourself -- the only route that " +
        "works between Android and Windows, and the only one whose timing is up to you. Keep a recent " +
        "one. Your mail password is in neither: it never leaves this phone's Keystore, so you enter " +
        "it once after any restore.",
    "The sync said some media was \"too large to email\"." to
        "Every provider caps how big one email can be — 25 MB at Gmail, Outlook and Yahoo, 20 MB at " +
        "iCloud. A busy day is split across several emails to stay under that, so you'll almost never " +
        "notice. What can't be split is a single file, usually a long video, that's bigger than the " +
        "whole cap on its own — no email anywhere can carry it. The message itself is still archived " +
        "(text, sender, time, its place in the conversation) and the email shows a note in the video's " +
        "place naming the file and its size. The video isn't lost: it's still in the WhatsApp export " +
        "you imported, and still on your phone. It will be left out on every future sync too, which is " +
        "why the sync summary names it. One surprise worth knowing: a 20 MB video doesn't make a 20 MB " +
        "email — email can't carry raw files, so everything is re-encoded on the way out and grows by " +
        "about a third. The practical ceiling for one file is around 18 MB on a 25 MB provider.",
    "Where do I see everything about one chat?" to
        "Tap the chat in the chats list. Its own screen shows when it last synced, how many messages " +
        "have gone out, whether a mail thread already exists for it and which export file it came " +
        "from — with the same three actions on it: sync just that one chat, reset it, or " +
        "delete it from the list. \"Sync just this chat\" runs a normal sync limited to that " +
        "one chat instead of everything waiting in the inbox. The Windows app has the same screen: " +
        "click a chat's row in the list.",
    "I want to re-do a chat from scratch. What does Reset do?" to
        "It clears this app's local record of what's been synced for that chat. It does NOT delete " +
        "anything already in your mailbox — this app can only add mail, never remove it. The next " +
        "sync therefore files a fresh copy of the whole chat into a brand-new thread, so if the old " +
        "mail is still there you end up with two copies. That's why Reset asks you to clear the " +
        "chat's folder first, and to confirm you've done it. On Gmail, deleting the label is not " +
        "enough: the messages stay in All Mail. Open the label, select every conversation, delete " +
        "them, then empty the Bin.",
    "I removed a chat from the list by mistake." to
        "\"Delete from list\" only removes the chat from this app's list — it does not delete " +
        "anything from your mailbox. Your emails are safe. Import the export file again to bring " +
        "the chat back.",
    "The times on some messages look off." to
        "WhatsApp exports don't include a timezone — the app assumes the exporting phone's local " +
        "clock. If you export from a different timezone than the chat was recorded in, or the " +
        "phone's timezone changed between exports, times may shift. That's a limitation of the " +
        "export format, not a bug in the app.",
    "Can I keep a copy of my chat list?" to
        "Yes — export the chat list as a CSV from the chats screen and share or save it wherever " +
        "you like. It lists every chat the app knows about with its last run and status.",
    "What can't this app do?" to
        "It can't read your existing mail, send email on your behalf, or keep syncing live in the " +
        "background continuously — each sync is a one-time pass over whatever's waiting in the " +
        "inbox. It also can't remove anything from your mailbox, which is why some answers above " +
        "ask you to clear a folder by hand.",
)

// Bare hostnames as they appear in the answers above. Written out rather
// than matched with a URL regex: there is exactly one link in this FAQ, an
// answer that invites the reader to go and check the source, and a regex here
// would be a general-purpose linkifier standing in for a single known string.
internal val FAQ_LINKS = mapOf(
    "github.com/harshalambani/ChatMailSync" to "https://github.com/harshalambani/ChatMailSync",
)

/**
 * An answer with its known links made tappable.
 *
 * The FAQ entries stay plain strings -- they are compared question-for-question
 * against help.html and docs/user-guide.md by tests/test_faq_parity.py, and an
 * AnnotatedString in the table would put Compose markup into text that two
 * other surfaces have to match.
 */
internal fun linkify(text: String): AnnotatedString = buildAnnotatedString {
    var rest = text
    outer@ while (rest.isNotEmpty()) {
        // Earliest occurrence wins, and on a tie the longer hostname, so
        // one that is a prefix of another cannot take the match off it.
        val hit = FAQ_LINKS.keys
            .map { it to rest.indexOf(it) }
            .filter { it.second >= 0 }
            .minWithOrNull(
                compareBy<Pair<String, Int>> { it.second }.thenByDescending { it.first.length },
            )
        if (hit == null) {
            append(rest)
            break@outer
        }
        val (shown, at) = hit
        append(rest.substring(0, at))
        withLink(
            LinkAnnotation.Url(
                FAQ_LINKS.getValue(shown),
                TextLinkStyles(
                    style = SpanStyle(textDecoration = TextDecoration.Underline),
                ),
            ),
        ) { append(shown) }
        rest = rest.substring(at + shown.length)
    }
}

/**
 * One line of the FAQ, with its "Q" or "A" tag hanging in a fixed gutter so the
 * two halves of an entry read as a pair rather than as two paragraphs.
 */
@Composable
private fun QaLine(tag: String, text: String, style: TextStyle) {
    Row {
        Text(
            text = tag,
            style = MaterialTheme.typography.labelSmall.copy(
                fontWeight = FontWeight.Bold,
                letterSpacing = 0.5.sp,
            ),
            color = MaterialTheme.colorScheme.primary,
            modifier = Modifier
                .width(20.dp)
                .alignByBaseline(),
        )
        Text(text = linkify(text), style = style, modifier = Modifier.alignByBaseline())
    }
}

@Composable
fun HelpScreen(onBack: () -> Unit) {
    Scaffold(
        // Zero, deliberately: MainActivity's Scaffold has already padded
        // this NavHost for the status bar and the bottom bars, and insets
        // are not consumed by being turned into padding -- so a screen
        // Scaffold left on the default reserves the same strips a second
        // time. That silently cost about a row and a half of list height
        // on every screen, which is how two exports ended up below the
        // fold on the import picker.
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        topBar = {
            ChatMailTopBar(
                title = "Help & FAQ",
                backLabel = "Settings",
                onBack = onBack,
            )
        },
    ) { padding ->
        val scrollState = rememberScrollState()
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(20.dp)
                .fadingEdges(scrollState, MaterialTheme.colorScheme.background)
                .verticalScrollbar(scrollState)
                .verticalScroll(scrollState),
            verticalArrangement = Arrangement.spacedBy(20.dp),
        ) {
            FAQ.forEachIndexed { index, (question, answer) ->
                if (index > 0) {
                    HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                }
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    QaLine("Q", question, MaterialTheme.typography.titleSmall)
                    QaLine("A", answer, MaterialTheme.typography.bodyMedium)
                }
            }
        }
    }
}
