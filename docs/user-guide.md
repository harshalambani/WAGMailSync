# Chat Mail Sync — User Guide

A friendly, step-by-step guide to backing up your WhatsApp chats into your
mailbox. No technical knowledge needed.

---

## 1. What this app does

WhatsApp lets you **export** a chat to a file. On its own, that file is hard to
read and easy to lose.

This app takes those exported chat files and copies the conversations into
**your mailbox**, where they're:

- Searchable, like any other email.
- Organised under labels/folders — one per chat, all grouped under **WhatsApp**.
- Shown as a tidy, WhatsApp-style conversation, with photos and files included
  (if you exported them).

It **adds** the messages to your mailbox. It never sends anything, never deletes
anything, and never posts anything back to WhatsApp.

---

## 2. One-time setup: connecting to your mailbox

Before the first sync, the app needs permission to add messages to your mailbox.
It does that with an **email app password (IMAP)**, which works with Gmail,
Outlook, Yahoo, iCloud, Fastmail and any other IMAP mailbox.

### Email app password (IMAP)

An "app password" is a separate password your email provider generates for one
app. It only works for mail, and you can revoke it at any time without changing
your real password.

1. Create an app password with your provider. For Gmail, go to your Google
   Account → Security → 2-Step Verification → App passwords. (Most providers
   require two-factor authentication to be on before they will issue one.)
2. Open the app, click the **gear icon** (top-right) to open Settings. Settings
   opens inside the main window rather than in a separate one — **Back to sync**
   (top-left), or the Escape key, returns you to the sync view.
3. Under **Mail account**, click **Change…**. That opens the Mail account screen
   the same way, with **Back to settings** to come out of it.
4. Choose your provider (Gmail, Outlook, Yahoo, iCloud, Fastmail, or a custom
   IMAP server — host and port fill in automatically for the known ones).
5. Enter your email address and the app password, then **Save**.
6. The indicator turns **green** and says **Connected**.

This does not expire. To disconnect later, click **Forget saved password**.

---

## 3. Exporting a WhatsApp chat to a file

Do this on your phone, then move the file to your computer (email it to yourself,
use a USB cable, or any cloud drive).

> **One chat at a time.** WhatsApp has no multi-select export — you cannot tick
> several chats and export them together. Each chat has to be exported on its
> own, from inside that chat. The app itself has no such limit: once the files
> are on your computer you can add as many as you like at once and sync them in
> one run.

### On Android

1. Open the chat you want to back up.
2. Tap the **⋮** menu (three dots) in the top-right.
3. Tap **More** → **Export chat**.
4. Choose **Include media** (to keep photos and files) or **Without media** (text
   only — smaller, faster).
5. Choose where to save or how to share the file.

### On iPhone (iOS)

1. Open the chat you want to back up.
2. Tap the contact or group **name** at the top of the screen.
3. Scroll down and tap **Export Chat**.
4. Choose **Attach Media** or **Without Media**.
5. Choose where to save (e.g. **Save to Files**) or how to share it.

You'll get either a **`.txt`** file (text only) or a **`.zip`** file (text plus
media). The app accepts both.

---

## 4. Adding chat files to the app

Once the export file is on your computer:

- **Drag and drop** the `.txt` or `.zip` file onto the app window, **or**
- Click **Browse Files…** and pick it, **or**
- Click **Open Inbox Folder** and copy the file into that folder yourself.

Added files appear in the **Files in inbox** list, with a count like
"2 files ready to sync". You can add several chats at once.

### Letting the app fetch them for you — the watched folder

If your exports always land in the same place (your Downloads folder, or a folder
your phone syncs to), the app can collect them itself, on a schedule if you want
one. That is section 6.

---

## 5. Running a sync

1. Make sure the indicator at the top says **Connected** (green).
2. Check the options at the bottom-right:
   - **Dry run** — tick this to do a practice run that reports what *would* happen
     without changing your mailbox. Great for a first try.
   - **Chunk size** — how much of the conversation goes into each email:
     **day** (default), **hour**, or **week**. "Day" means one email per day of chat.
3. Click **▶ Sync Now**.
4. Watch the progress bar and the log at the bottom. You can click **⏹ Stop** to
   stop after the current file finishes.

When it's done, each chat appears in the list on the left with a green dot and a
message count. Synced files move out of the inbox automatically.

---

## 6. Scheduling: having it sync on its own

There is one scheduling mechanism, and it is the **watched folder**: you nominate
a folder, the app checks it on an interval, and anything new it finds is imported
and synced without you opening anything. There is no separate "sync every N
hours" setting, because with nothing new in the inbox there would be nothing for
it to do.

Both editions have the feature and the same settings. What differs is what the
operating system underneath will allow, and that difference is worth
understanding before you rely on it.

### Setting it up on Windows

1. **Settings → Watched folder → Choose…** and pick the folder.
2. That alone is enough for on-demand use: **Check watched folder and sync**
   appears on the main window and runs whenever you click it, whether or not the
   automatic check is switched on.
3. To have it look by itself, tick **Check it automatically** and choose an
   interval.
4. **After syncing** decides the fate of the *original* file in the watched
   folder.

**Interval options (Windows):** every 5 min · every 15 min (default) · every
30 min · every hour · every 3 hours · every 6 hours · every 12 hours · once a day.

**After syncing options:** *Leave in place* (default) · *Move to a "synced"
subfolder* · *Delete after import (Recycle Bin)*.

> **The check only runs while the app is open.** The Windows edition installs no
> background service and no scheduled task — the timer lives inside the running
> window. Close the app and nothing is checked until you open it again, at which
> point it checks on the next tick. If you want it checking all day, leave the
> window open (minimised is fine).

Changing the interval takes effect immediately on the existing schedule; it does
not wait for the current wait to run out, and it never leaves two timers running.

### Setting it up on Android

1. **Settings → Watched folder → Choose folder** and pick the folder (Android's
   own file picker, so it can be a cloud folder your provider exposes there).
2. **Check and sync** in the same section looks immediately, and sends whatever
   it finds, whether or not auto-import is on.
3. Turn on **Auto-import from this folder** and choose an interval.
4. **After import, synced files:** the same three choices.

**Interval options (Android):** every 15 min (default) · every 30 min · every
hour · every 3 hours · every 6 hours · every 12 hours · once a day.

Two differences from Windows, both imposed by Android rather than chosen:

- **There is no 5-minute option.** Android's background scheduler enforces a hard
  15-minute floor. No app of any kind can go under it.
- **"Delete after import" really deletes.** Android has no Recycle Bin, so there
  is nothing to recover the file from. The Windows edition recycles instead.

In exchange, Android *does* check with the app closed — that is the whole point
of doing it through the system scheduler.

### The interval is a floor, not a promise (Android)

Android treats a periodic job as "not more often than this", never "exactly
this". A tick can be delayed — by minutes on a phone in use, by hours on one that
is idle in Doze — and several may be batched together to save power. Set it to
every hour and you should expect roughly hourly, not on the hour.

The scan itself needs no network. The sync it triggers does, so if the phone is
offline when a tick fires, the files are imported and wait in the inbox for the
next run rather than failing.

### Making the schedule reliable — battery optimisation (Android)

The single most common reason a schedule "stops working" is Android's battery
optimisation putting the app to sleep. If auto-import matters to you, exempt the
app once:

- **Stock Android:** Settings → Apps → **Chat Mail Sync** → **Battery** →
  **Unrestricted**.
- **Samsung (One UI)**, which is stricter and will otherwise stop the schedule
  within a day or two of non-use: Settings → **Battery** → **Background usage
  limits** → make sure Chat Mail Sync is **not** in *Sleeping apps* or *Deep
  sleeping apps*, and add it to **Never sleeping apps**. Also turn off *Put
  unused apps to sleep* if you use the app infrequently.
- **Xiaomi / Oppo / Vivo / OnePlus:** in addition to the above, these keep a
  separate "Autostart" or "Startup manager" permission. Enable it for the app, or
  background work stops after a reboot.

None of this affects manual syncs or **Check and sync** — those run in the foreground
while you are looking at the app, so the system leaves them alone. It only
affects unattended checks.

> **Battery cost.** The check is cheap — a directory listing — but it is not
> free, and a sync that follows it uses the network. Once a day suits most
> people; every 15 minutes is for a folder that genuinely receives exports
> through the day.

### What is *not* scheduled

- **Nothing is scheduled unless you nominate a watched folder.** Files you drag
  in or import by hand sit in the inbox until you sync.
- **The inbox refresh interval in the Windows settings is not a schedule.** It
  only decides how often the file list on screen redraws (Off · 15 s · 30 s ·
  1 min · 5 min). It never syncs anything.
- **There is no continuous, live sync.** Each run is a single pass over whatever
  is waiting.

### Rules the two editions share

- Only the nominated folder is looked at. Subfolders are ignored — which is also
  why the `synced` subfolder is never re-imported.
- `.txt` and `.zip` only, the same as drag-and-drop.
- Each source file is picked up exactly once, however often the check runs.
- The **After syncing / After import** rule is applied only once a file has
  genuinely reached your mailbox. A sync that fails, or that you stop, leaves
  your originals exactly where they are.
- If a file cannot be moved or removed, the app says so and leaves it alone
  rather than losing it.
- If no mail account is connected when a check finds something, the files are
  imported and left in the inbox with a note, not thrown away.

---

## 7. Where your messages appear

**If you connected with Gmail**, open Gmail and look in the labels list (left
side). You'll find:

- A **WhatsApp** label.
- Under it, one label per chat, e.g. **WhatsApp/John Doe**, **WhatsApp/Family Group**.

**If you connected with IMAP**, open your mail app and look for a **WhatsApp**
folder, with one subfolder per chat, in the same structure.

Either way, each chat is a single **conversation thread**. Opening it shows the
messages laid out like WhatsApp, with photos shown inline and other files
attached for download.

---

## 8. Troubleshooting & FAQ

Every entry below is one question and one answer: **Q.** is the question, **A.**
is the answer, and a rule separates one from the next. The Windows in-app help
and the Android **Help & FAQ** screen carry the same questions in the same
order, each answered for its own platform.

---

### Q. How do I export a chat from WhatsApp?

**A.** On your phone, open the chat → the three-dot menu → **More** → **Export
chat**, then choose **Include media** (a `.zip`) or **Without media** (a plain
`.txt`). Section 3 has the longer version, including how to get the file onto
your PC. One chat at a time — WhatsApp has no multi-select export — but the app
itself will take as many exported files at once as you have.

---

### Q. Why doesn't the app "send" my messages anywhere?

**A.** Because nothing is being sent. The app uses the mail command that *adds*
a message to a mailbox (IMAP APPEND), which is the same thing your mail client
does when it saves a draft or files a copy of an outgoing message. Your chats
land in your own mailbox and nobody else receives anything.

---

### Q. What can the app reach in my email account?

**A.** Nothing outside the mailbox. What you hand it is an *app password*, and
that is a mail credential rather than an account one — it works with the mail
protocols and nothing else. It cannot sign in to your provider's website or app,
cannot reach your files, photos, contacts or calendar, cannot see or change your
account settings, and cannot change your real password. You create it separately
from your normal password, and you can revoke it on its own, from your provider,
whenever you like — that ends this app's access without disturbing anything
else you use. What it can do *inside* the mailbox is the next question.

---

### Q. What stops it reading or deleting my mail?

**A.** Inside the mailbox, the app's own code — and it is worth being plain
about that. No provider can issue a password limited to one mail operation: any
credential that can add mail can technically read and remove it too. What the app
does with it is use four commands and no others: list folders, create a folder,
subscribe to it, and add a message. None of them can open or remove a message,
and because the app never opens a folder for reading it never reaches the state
where that would be possible. The source is public if you or anyone else wants to
verify it: [github.com/harshalambani/ChatMailSync](https://github.com/harshalambani/ChatMailSync).

---

### Q. Where is my email app password kept?

**A.** Encrypted at rest, on the machine you entered it on. On Windows it is
encrypted with Windows DPAPI using a key derived from your Windows login, on top
of a file that only your account can open; on Android it is encrypted with a key
held in the phone's Android Keystore, which never leaves the phone's secure
hardware. Either way it is never written into the app's settings file, never
shown back to you in the password box after saving, and never included in a log
line or an error message.

---

### Q. The app says "Not connected", or authorising fails.

**A.** Double-check the email address and app password under **Settings → Mail
account → Change…**, and that the app password hasn't been revoked by your
provider. The app password is the usual culprit — providers revoke it if you
turn off two-factor authentication, and some expire it on their own.

---

### Q. Why doesn't it just sign in with Google or Microsoft?

**A.** Because that route is only open to apps the provider has formally
reviewed, and the review is not free. Google caps an unreviewed app at 100 users
and expires its sign-in roughly every 7 days; getting past that means going
through Google's verification, which for anything touching Gmail requires a
security assessment by an approved third party — paid for, and repeated every
year. Microsoft, Yahoo and Apple each have their own registration and review to
satisfy separately: it is one process per provider, not one piece of work. Chat
Mail Sync is free and takes no money from anyone, so there is nothing to fund
that with.

An app password works on every provider in the list, does not expire, and can be
revoked on its own. The honest trade-off is that you type a secret into the app
instead of clicking a consent screen, which is why two of the questions above set
out exactly what that secret can and cannot touch.

One consequence worth knowing: Microsoft has switched basic authentication off
for work and school (Microsoft 365) mailboxes, so an app password is refused
there; personal Outlook.com accounts are fine.

---

### Q. I moved the app to another PC, or set it up on a new phone, and it wants the password again.

**A.** That is expected, not a fault. The saved app password is encrypted with a
key tied to the machine it was entered on: your Windows account on that
particular PC, or the Android Keystore on that particular phone. A copy of the
app carried to a different PC, opened under a different Windows user, moved to a
new phone, or reinstalled after an uninstall cannot unlock it. Nobody who picks
up the folder or the phone can read your password either, which is the point.
Your app password is still valid at your provider: just enter it again under
**Settings → Mail account** on the new device.

---

### Q. My file doesn't show up in the inbox.

**A.** Only `.txt` and `.zip` files are accepted. Make sure you exported the chat
(not a screenshot or contact card), and that the file actually arrived — copied
over, on Windows; shared to the app or picked with **Import a WhatsApp export**,
on Android. On Windows, click the **⟳** refresh button to re-check the inbox
folder.

---

### Q. My photos and files didn't come through.

**A.** You probably exported **Without media**. Re-export the chat choosing
**Include media** / **Attach Media** (this produces a `.zip`), then sync that
file.

---

### Q. Emoji reactions on messages didn't come through.

**A.** WhatsApp keeps them, but it does not put them in the file it exports.
**Export chat** writes one line per message — a time, a sender and the text —
and WhatsApp has chosen not to give reactions a line of their own, so they never
leave the app. Your own WhatsApp backup does carry reactions; the export is a
deliberately simpler format. That exported file is the only thing Chat Mail Sync
is ever given, so there is nothing in it to import — and no setting on either
side changes that.

An emoji somebody *sent* as a message of its own comes through normally; it is
only the tap-and-hold reaction on someone else's message that the export leaves
behind. The same goes for anything else **Export chat** omits: the app can never
show more than WhatsApp put in the file.

---

### Q. What is the watched folder for?

**A.** It saves you adding files by hand. Point it at a folder — wherever your
WhatsApp exports land — and any `.txt` or `.zip` that appears there is picked up
and queued for the next sync. Only that one folder is looked at, subfolders are
left alone, and each file is picked up only once, so leaving it switched on
costs you nothing. Your original is never touched at import time: the
**After syncing** / **After import** setting takes effect only once a file has
actually reached your mailbox, so a sync that fails, or one you stop, leaves
everything exactly where it was. Section 6 covers it in full.

---

### Q. How do I make it sync on a schedule?

**A.** The watched folder *is* the schedule — there is no separate "sync every N
hours" setting, because with nothing new in the inbox there would be nothing to
do. Point it at a folder, switch on the automatic check, and pick an interval:
5 minutes to once a day on Windows, 15 minutes to once a day on Android, where
the platform imposes the floor. Section 6 has every option on both editions.

---

### Q. The schedule stopped running on its own. Why?

**A.** On **Windows**, the check only runs while the app is open — there is no
background service and no scheduled task, so a closed app has nothing running to
do the looking. Leave the window open, minimised if you like. On **Android**,
which does keep checking with the app closed, the cause is almost always battery
optimisation: the system has put the app to sleep, and a sleeping app gets no
background ticks. Exempt it once — stock Android, Samsung's sleep lists and the
Autostart permission on Xiaomi/Oppo/Vivo/OnePlus are all covered in section 6.
Neither case affects a manual sync, which runs in the foreground while you watch.

---

### Q. Will I get duplicate messages if I sync the same chat again?

**A.** Not on the same device. Every message is fingerprinted, so the app
remembers what it has already saved and only adds new messages — you can safely
re-export a chat later to pick up newer messages, and the overlap is skipped
automatically. That memory belongs to this instance of the app, though, not to
your mailbox; see the next question.

---

### Q. Can two instances of the app archive into the same mailbox?

**A.** They can, but they will not know about each other, and you will get
duplicates. This is not about Windows versus Android — **any** two instances
behave this way: two PCs, two phones, one of each, or even two copies of the
portable app on the same PC, since each copy carries its own `Data\` folder.

The record of what has already been archived lives in that instance's own
`sync_state.db` — nothing about it is stored in the mailbox. A second instance
signed in to the same account therefore starts from zero knowledge and re-files
every chat you give it, even chats the first one archived months ago. The app can
add mail but never remove it, so clearing the duplicates afterwards is manual work
only you can do.

> **Use one instance per mailbox.** If you want to archive from more than one place,
> give each instance its own mailbox or its own account. Replacing an instance is a
> different case — use **Settings -> Backup & restore** to carry the
> record across, and the new one picks up exactly where the old one stopped. See
> the next answer.

---

### Q. I am moving to a new PC or phone. How do I take my history with me?

**A.** On the old one: **Settings -> Backup & restore -> Save a backup**.
It writes a small file holding the record of what has already been sent, together
with your preferences. Put it wherever you like and move it across however you
normally move a file.

On the new one: install the app, then **Restore from a backup** in the same place,
before the first sync. Your chats are not in that file and do not need to be —
they are already in your mailbox, which is the archive. What the backup saves you
is a second copy of every one of them landing there.

Your mail password is deliberately left out, so the new machine asks for it once.
A backup taken on Windows restores on Android and the other way round. Restoring
**merges** rather than replaces, so a restore onto an app that has already synced
something keeps both sides, and restoring the same backup twice does nothing the
second time. You will still need to add your export files again on the new machine.

---

### Q. What happens if I reinstall the app or reset my device?

**A.** Your chats are safe either way — they are in your mailbox, and nothing that
happens on the device can take them out of it. What a reset, a reinstall, a wiped
machine or a deleted `Data\` folder destroys is the record of what has already
been sent, and without that record the app mails every chat a second time, into a
mailbox that has no way to tell the copies apart.

The protection is **Settings -> Backup & restore -> Save a backup**. It writes that
record to a file you keep somewhere else — not inside the app folder, which is the
thing that goes missing. Keep a recent one; the section tells you when the last one
was taken. The same file works on either platform, and on Android the system's own
backup carries the record as well. Your mail password is in neither, so you enter
it once after a restore.

---

### Q. The sync said some media was "too large to email".

**A.** Every email provider caps how big a single email can be — 25 MB at Gmail,
Outlook and Yahoo, 20 MB at iCloud, more at some others. The app works within
that cap by splitting a busy day across several emails, so in almost every case
you will never notice it. One case cannot be split: a *single* file, usually a
long video, larger than the whole cap on its own. No email anywhere can carry
it.

When that happens the message is still archived — the text, the sender, the
time, its place in the conversation — and the email carries a note where the
video would have been, naming the file and its size. Only the file itself is
left out, and it will be left out on every future sync too, which is why the
sync summary lists it by name rather than letting you discover it years later.
**The video is not lost**: it is still in the WhatsApp export you imported, and
still on your phone. If you want it in your mailbox, send it to yourself
separately, or shrink it first.

One thing that surprises people: a 20 MB video does not make a 20 MB email.
Email cannot carry raw files, so everything is re-encoded on the way out, which
adds about a third — a 20 MB video arrives as a roughly 27 MB email. That is why
the practical ceiling for a single file is around 18 MB on a 25 MB provider.

---

### Q. Where do I see everything about one chat?

**A.** Open the chat itself: on Windows click its row in the list, on Android tap
it in the chats list. Either way you get a screen for that one chat, inside the
same window, showing when it last synced, how many messages have gone out,
whether a mail thread already exists for it and which export file it came from.
The same three actions are on it: sync just that one chat, reset it, or delete
it from the list.

**Sync just this chat** runs a normal sync limited to that chat, instead of
everything waiting in the inbox — the same job as the command line's `--chat`
option. On Windows, the small icons on a chat's row in the list are shortcuts to
these same actions; hover over any of them to see what it does.

---

### Q. I want to re-do a chat from scratch. What does Reset do?

**A.** On Windows, click the **↺** (re-sync) icon next to the chat in the list;
on Android, open the chat and choose **Reset (forget sync history)**. Either way
it clears the app's record of that chat and, where it can, moves its file back to
the inbox so you can sync it again. It does **not** delete anything already in
your mailbox.

**Delete the old mail first.** This app can only add mail, never remove it, so if
the earlier messages are still in your mailbox the next sync files a second copy
alongside them. When a chat has already been archived, the app names the folder to
clear and asks you to confirm you have cleared it before it will reset.

> **On Gmail this needs care.** Gmail has no real folders, only labels — deleting
> the label just unlabels the messages and leaves them in All Mail, where the next
> sync still counts them as duplicates. Open the label, select every conversation,
> delete them, then empty the Bin.

---

### Q. I removed a chat from the list by mistake.

**A.** Removing a chat from the list — the **✕** button on Windows, **Delete from
list** on Android — does **not** delete anything from your mailbox. Your emails
are safe. Just add the export file again to bring it back.

---

### Q. The times on some messages look off.

**A.** WhatsApp exports don't include a timezone, so the app reads them against
the exporting phone's local clock. If your phone's timezone changed between
exports, or you export from a different timezone than the chat was recorded in,
some timestamps can appear shifted. This is a known limitation of WhatsApp
exports, not a bug in the app.

---

### Q. Can I keep a copy of my chat list?

**A.** Yes — click the **CSV** button above the chat list on Windows, or use the
CSV export from the chat list on Android, to get a spreadsheet of all your synced
chats.

---

### Q. What can't this app do?

**A.** It cannot read your existing mail, cannot send email on your behalf, and
cannot sync continuously in the background — each sync is a one-time pass over
whatever is waiting in the inbox. It also cannot remove anything from your
mailbox, which is why several answers above ask you to clear a folder by hand.
