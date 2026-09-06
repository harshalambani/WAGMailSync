"""
GUI entry point for Chat Mail Sync.

Run with:
    python gui.py

Dependencies (in addition to Phase 1 requirements):
    pip install customtkinter tkinterdnd2
"""

import csv
import json
import os
import queue
import re
import shutil
import sys
import threading
import tkinter
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from tkinterdnd2 import DND_FILES, TkinterDnD

import gui_theme
from _splash import dismiss_launcher_splash
from gui_worker import (
    SyncWorker,
    _scrub_paths,
    check_auth_status,
    check_imap_auth_status,
    connect_imap,
    resolve_imap_password,
    test_imap_connection,
)
from src.config import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAIL_BACKEND,
    IMAP_CREDENTIALS_FILE,
    IMAP_PROVIDERS,
    INBOX_DIR,
    MAIL_BACKEND_IMAP,
    PROCESSED_DIR,
    PROJECT_ROOT,
    STATE_DB_PATH,
    is_gmail_mailbox,
    is_legacy_oauth_user,
    mailbox_clear_steps,
    resolve_mail_backend,
)
from src.app_version import app_version, version_label
# The same bundle format Android reads and writes, so a backup taken on
# either front-end restores on the other.
from src import migration
# The same function the parser uses to name a chat, so the file list, the chat
# list and the emails themselves cannot disagree about who a file is from.
from src.parser import extract_chat_info
# Shared with Android: Preview and the X mean the same thing on both
# front-ends because they are the same three functions, not two ports of
# one idea. (The module is named for Android for historical reasons only.)
from src.android_api import (
    format_preview,
    preview as preview_export,
    remove_from_inbox,
)
from src.mail_client import build_imap_transport
from src.mail_client import connection_stage_plan, mailbox_folder_for
from src.progress import ProgressTracker
from src.watch_folder import (
    DEFAULT_WATCH_INTERVAL_MINUTES,
    MIN_WATCH_INTERVAL_MINUTES,
    apply_pending_synced_file_policies,
    scan_watch_folder,
)
from src.state import (
    MailboxNotClearedError,
    count_archived_messages,
    delete_chat,
    get_recent_runs,
    get_sync_summary,
    init_db,
    is_uneventful_run,
    reset_chat,
    summarize_recent_runs,
)

# ---------------------------------------------------------------------------
# Appearance
# ---------------------------------------------------------------------------

# Read saved theme preference (data/.theme) before any widgets are created.
_THEME_FILE = Path(__file__).parent / "data" / ".theme"
_saved_theme = "dark"
if _THEME_FILE.exists():
    _t = _THEME_FILE.read_text().strip()
    if _t in ("dark", "light"):
        _saved_theme = _t

ctk.set_appearance_mode(_saved_theme)

# Quiet Archive, the same palette the Android app has used since 2026-07-05.
# This has to run before the first widget is constructed -- CustomTkinter reads
# its defaults out of ThemeManager at construction time, not at draw time.
gui_theme.apply_theme(ctk)

_STATUS_COLOR = gui_theme.STATUS_COLOR

# The chat list's status chips. Three states, not four: a chat is synced,
# it failed, or it has never run. Android's ChatStatus enum deliberately has
# the same three -- a chat mid-run is a transient the list is not the place to
# chase, and a fourth chip nobody could ever click while looking at it is
# worse than none.
_CHAT_FILTERS = ("all", "synced", "failed", "never")
_CHAT_FILTER_LABELS = {
    "all": "All",
    "synced": "Synced",
    "failed": "Failed",
    "never": "Never synced",
}

# Answers "why is this list empty?" for each chip, in that chip's own terms.
_CHAT_EMPTY_CHIP_TITLES = {
    "synced": "No chats have synced yet.",
    "failed": "Nothing has failed.",
    "never": "Every chat has been synced at least once.",
}


# The same three states said as a headline rather than as a chip label, for
# the top of a single chat's screen. Word-for-word Android's ChatStatus
# descriptions, since it is the same chat in the same three states. Not the
# run headlines (_STATUS_HEADLINE): those describe one run, and a chat that
# has never run is "Not synced yet", not "Still running".
_CHAT_STATUS_HEADLINE = {
    "synced": "Synced",
    "failed": "Last sync failed",
    "never": "Not synced yet",
}


def _chat_status_of(row: dict) -> str:
    """Which chip a chat row belongs under."""
    status = row.get("last_run_status")
    if status == "failed":
        return "failed"
    if status is None or status == "":
        return "never"
    return "synced"


_LOG_MAX_LINES    = 200
_POLL_MS          = 150    # queue poll interval (ms)
_AUTH_POLL_MS     = 250    # auth queue poll interval (ms)
_AUTO_REFRESH_MS  = 30_000 # inbox auto-refresh interval (ms) — overridden by settings

_SETTINGS_FILE = Path(__file__).parent / "data" / ".settings.json"
_AUTO_REFRESH_OPTIONS = {
    "Off":    0,
    "15 s":   15_000,
    "30 s":   30_000,
    "1 min":  60_000,
    "5 min":  300_000,
}
_DEFAULT_SETTINGS = {
    "chunk_size":          "day",
    "auto_refresh_label":  "30 s",   # key into _AUTO_REFRESH_OPTIONS
    "mail_backend":        DEFAULT_MAIL_BACKEND,
    "imap_provider":       "gmail",
    "imap_host":           "",
    "imap_port":           993,
    "imap_email":          "",
    # One-time "there's a new backend option" notice (Road B, phase 2). Never
    # a second file/key -- persisted in this same .settings.json. Password is
    # intentionally NOT in this dict; it only ever lives in
    # IMAP_CREDENTIALS_FILE (see gui_worker._save_imap_credentials).
    "backend_notice_shown": False,
    # One-time "Google sign-in has been removed" notice (v2.0.0). Shown only
    # to someone who actually had it -- see _should_show_oauth_removed_notice.
    "oauth_removed_notice_shown": False,
    # Watched folder -- the desktop half of Android's WatchFolderWorker. Key
    # names deliberately match AppPrefs' so the two platforms' state is
    # readable side by side. See src/watch_folder.py for the rules; the two
    # ledgers below are bookkeeping, not preferences, and are never shown in
    # the Settings dialog.
    "watched_folder_path":     "",
    "auto_watch_enabled":      False,
    "watch_interval_minutes":  DEFAULT_WATCH_INTERVAL_MINUTES,
    "synced_file_policy":      "leave",
    "imported_source_paths":   [],
    "pending_synced_files":    {},
    # Did the last real attempt to reach the mailbox succeed? None means no
    # attempt has ever been recorded -- which is where every existing install
    # lands on upgrade, and is why this is a tri-state rather than a bool: a
    # saved credential and a credential known to work are different facts, and
    # showing the second when we only know the first is the misreport this
    # exists to stop. Android stores the same pair in AppPrefs under
    # last_connection_ok / last_connection_at. Nothing about the credential
    # itself is stored here -- a verdict and a timestamp, nothing more.
    "last_connection_ok":      None,
    "last_connection_at":      0,
    # Bookkeeping, not a preference: when a backup was last written, epoch
    # seconds, 0 for never. Only keys named here survive a reload, so an
    # undeclared one would be saved and then quietly thrown away. Android
    # keeps the same fact in AppPrefs as last_backup_at, in millis.
    "last_backup_at":          0,
}

# Android's WATCH_INTERVAL_LABELS, labels and all, plus one shorter option:
# WorkManager's 15-minute floor is a platform rule Android cannot go under,
# while a Tk timer can, and someone dropping exports into a folder on this same
# PC reasonably wants them picked up sooner than a quarter of an hour. The
# default matches Android's, so both products behave the same untouched.
_WATCH_INTERVAL_OPTIONS = {
    "Every 5 min":    5,
    "Every 15 min":   15,
    "Every 30 min":   30,
    "Every hour":     60,
    "Every 3 hours":  180,
    "Every 6 hours":  360,
    "Every 12 hours": 720,
    "Once a day":     1440,
}
# Android's labels verbatim, except that its "Delete after import" would be a
# lie here: this build recycles rather than erasing, and someone deciding
# whether to switch the option on deserves to know that beforehand.
_SYNCED_FILE_POLICY_LABELS = {
    "leave":  "Leave in place",
    "move":   'Move to a "synced" subfolder',
    "delete": "Delete after import (Recycle Bin)",
}
_SYNCED_FILE_POLICY_LABELS_REV = {v: k for k, v in _SYNCED_FILE_POLICY_LABELS.items()}


def _load_settings() -> dict:
    """Return settings dict, merging saved values over defaults."""
    settings = dict(_DEFAULT_SETTINGS)
    saved = {}
    try:
        if _SETTINGS_FILE.exists():
            saved = json.loads(_SETTINGS_FILE.read_text())
            for k in _DEFAULT_SETTINGS:
                if k in saved:
                    settings[k] = saved[k]
    except Exception:
        pass
    # Resolved separately from the plain merge above because a settings file
    # may still say "gmail_oauth", a backend that no longer exists.
    # gui_worker calls the same helper on the same file.
    settings["mail_backend"] = resolve_mail_backend(saved)
    return settings


def _save_settings(settings: dict) -> None:
    try:
        _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except Exception:
        pass


# Thirty days is a judgement, not a rule: long enough that someone who keeps up
# is never nagged, short enough that what a lost record would re-mail is about a
# month of chats. Android's Migration.BACKUP_STALE_AFTER_DAYS says the same.
_BACKUP_STALE_AFTER_DAYS = 30


def _backup_is_stale(at: int, now: "float | None" = None) -> bool:
    """True when there is no backup, or the last one is old enough to matter.

    [at] is epoch seconds -- Android's twin works in millis, because that is
    what each platform's own clock hands out; the boundary is the same.
    """
    if at <= 0:
        return True
    now = datetime.now().timestamp() if now is None else now
    return now - at > _BACKUP_STALE_AFTER_DAYS * 24 * 60 * 60


def _describe_last_backup(at: int) -> str:
    """"Last backup: 28 Aug 2026", or the plain fact that there isn't one."""
    if at <= 0:
        return "No backup saved yet."
    # Built by hand rather than with strftime: "%-d" is a glibc extension and
    # raises on Windows, which is the only platform this file runs on.
    when = datetime.fromtimestamp(at)
    return "Last backup: %d %s %d" % (when.day, when.strftime("%b"), when.year)


def _relabel_connected(text: str, lead: str) -> str:
    """Swap the leading "Connected" of a status line for [lead], keeping any
    tail. "Connected (a@b.com)" -> "Not tested (a@b.com)".

    The tail is the part worth keeping -- it says *which* account -- while the
    first word is the only part that was overstating things. Text that does not
    begin that way is left alone: it is already an explanation of a failure
    ("Sign-in expired -- reconnect") and rewriting it would lose the reason.
    """
    if text.startswith("Connected"):
        return lead + text[len("Connected"):]
    return text


def _auth_display(valid: bool, text: str, last_ok: "bool | None") -> tuple[str, str]:
    """(status key, label) for the masthead dot and its words.

    Pure, and separate from the widget call, because this is the whole of
    Batch G's Windows judgement and it is worth testing without a Tk root.
    The key is a gui_theme.STATUS_COLOR key, so the band needs no second
    mapping of its own.

    The three outcomes match Android's four states (see ConnectionStatus.kt);
    NONE and FAILED-with-no-account collapse into one here because Windows'
    label already carries the difference in words ("Not connected" vs
    "Credential error: ...").

      - no credential we can read at all      -> failed, unchanged text
      - credential, never yet tried           -> pending (amber), "Not tested"
      - credential, last attempt failed       -> failed, "No connection"
      - credential, last attempt succeeded    -> complete (green), "Connected"

    Amber is the one that did not exist before: a saved password shows as
    green "Connected" the moment it is stored, which is what made connecting
    "feel like a non-event" -- the header said the same thing before and after
    the mailbox was ever actually reached.
    """
    if not valid:
        return "failed", text
    if last_ok is None:
        return "pending", _relabel_connected(text, "Not tested")
    if last_ok:
        return "complete", text
    return "failed", _relabel_connected(text, "No connection")


def _legacy_oauth_evidence() -> bool:
    """Whether this install belonged to a Google sign-in user.

    Read from the RAW settings file, before defaults are merged in: once
    _load_settings has run, mail_backend has already been resolved to "imap"
    and the evidence is gone. Purely informational -- it decides whether the
    one-time "Google sign-in has been removed" notice is warranted, and
    nothing else.
    """
    try:
        saved = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    return is_legacy_oauth_user(saved)


def _should_show_oauth_removed_notice(settings: dict, was_oauth_user: bool) -> bool:
    """Decide whether to show the one-time "Google sign-in removed" notice.

    Shown once, and only to someone who has the evidence of having used it: a
    saved gmail_oauth backend or a leftover token.json. A fresh install never
    sees it -- there is nothing to explain to someone who never had the thing
    that was taken away, and saying it anyway advertises a path they cannot
    take.
    """
    if settings.get("oauth_removed_notice_shown", False):
        return False
    return was_oauth_user


def _should_scan_at_launch(auto_watch_enabled: bool, folder) -> bool:
    """Whether to scan the watched folder once on startup.

    Exactly the condition the periodic timer arms itself under, and that is
    the point: launch is not a licence to scan a folder someone has switched
    the watcher off for. "Check now" remains the way to force one.
    """
    return bool(auto_watch_enabled) and folder is not None


def _inbox_has_files() -> bool:
    """Whether anything is still queued in inbox/.

    The watcher needs this for the case Android hit first: a previous pass
    imported files and ledgered them, but they were never delivered (no mail
    account configured yet, say). Without it, every later check would report
    "no new files found" forever while a backlog sat in the inbox, because the
    ledger legitimately skips those sources.
    """
    try:
        return any(
            f.is_file() and f.suffix.lower() in (".txt", ".zip")
            for f in INBOX_DIR.iterdir()
        )
    except Exception:
        return False


def _app_icon_path() -> "Path | None":
    """Locate appicon.ico for both source runs and the frozen bundle.

    The exe already embeds this icon (see `icon=` in chat-mail-sync.spec), which
    is why Explorer and the Start menu have always shown it -- but a Tk window
    does not inherit its process's exe icon. It uses its own window class icon,
    which defaults to Tk's, so the title bar and taskbar showed a generic
    placeholder while every other surface showed the real logo. Nothing was
    wrong with the icon set; nobody had ever pointed the window at it.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:                                   # PyInstaller onedir bundle
        candidates.append(Path(meipass) / "appicon.ico")
        candidates.append(Path(sys.executable).parent / "appicon.ico")
        # Packaged portable layout: App\ChatMailSync\ChatMailSync.exe with the
        # icon set one level up in App\AppInfo\.
        candidates.append(Path(sys.executable).parent.parent / "AppInfo" / "appicon.ico")
    candidates.append(Path(__file__).parent / "portable" / "App" / "AppInfo" / "appicon.ico")
    for c in candidates:
        if c.exists():
            return c
    return None


def _masthead_image_path() -> "Path | None":
    """Locate the PNG the masthead draws its mark from.

    A .png rather than the .ico the title bar uses: Tk's own PhotoImage reads
    PNG natively (8.6+) and cannot read .ico at all, and the alternative --
    Pillow -- is in chat-mail-sync.spec's excludes list precisely so it does
    not get dragged into the bundle for one 38px logo.

    appicon_75.png rather than appicon_32.png, then halved at draw time:
    scaling a bitmap *up* to fill the band would show every pixel of it, and
    Tk's zoom() has no interpolation to hide that with.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "appicon_75.png")
        candidates.append(Path(sys.executable).parent / "appicon_75.png")
        candidates.append(
            Path(sys.executable).parent.parent / "AppInfo" / "appicon_75.png"
        )
    candidates.append(
        Path(__file__).parent / "portable" / "App" / "AppInfo" / "appicon_75.png"
    )
    for c in candidates:
        if c.exists():
            return c
    return None


def _help_html_path() -> "Path | None":
    """Locate help.html for both source runs and the frozen PyInstaller bundle."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:                                   # PyInstaller onedir bundle
        candidates.append(Path(meipass) / "help.html")
        candidates.append(Path(sys.executable).parent / "help.html")
    candidates.append(Path(__file__).parent / "help.html")  # running from source
    for c in candidates:
        if c.exists():
            return c
    return None


def _plain_color(color):
    """Resolve a CustomTkinter (light, dark) pair for a bare tkinter widget.

    gui_theme states every colour as a pair and CTk widgets pick from it by
    appearance mode. Plain tkinter cannot: handed the tuple it raises
    TclError. That is how a hover used to strand a blank white 200x200
    Toplevel on top of the window -- the Label raised before the window was
    recorded, so nothing was left holding it to close it.
    """
    if isinstance(color, (tuple, list)):
        return color[1] if ctk.get_appearance_mode() == "Dark" else color[0]
    return color


def _hand_cursor(widget) -> None:
    """Give a CTk widget the pointing-hand cursor, inner label included.

    configure(cursor=...) reaches the CTk frame, and the frame is the one part
    of a CTkLabel the pointer is never over: the inner tk label covers it. So
    the frame alone changes nothing visible. The inner widget is private API,
    hence the guard -- a missing cursor is cosmetic, and not worth an
    exception on a customtkinter that renames it.
    """
    try:
        widget.configure(cursor="hand2")
    except Exception:
        pass
    inner = getattr(widget, "_label", None) or getattr(widget, "_canvas", None)
    if inner is not None:
        try:
            inner.configure(cursor="hand2")
        except Exception:
            pass


class _Tooltip:
    """A hover label for a button whose whole text is one glyph.

    Eight controls on this window say ⚙ ? ☽ ☀ ⟳ ↺ ✕ and CSV, several of them
    22x20 -- a set of symbols the user has to click to learn. Android says what
    each of its equivalents does in words on the button itself; Windows has no
    room for that on a 22px target in a 236px column, so it says it on hover
    instead. The words are the ones the chat detail panel uses for the same
    action, so the shortcut and the screen it shortcuts cannot name the same
    thing differently.

    Not a pop-up in the sense the in-window panels replaced: it takes no input,
    it cannot be dismissed wrongly, and it disappears the moment the pointer
    leaves or the button is pressed.
    """

    # Long enough that sweeping the pointer across a row of buttons does not
    # flash a trail of them, short enough to feel like an answer.
    DELAY_MS = 450

    def __init__(self, widget, text: str) -> None:
        self._widget = widget
        self._text = text
        self._after_id = None
        self._window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def set_text(self, text: str) -> None:
        """Re-word a tooltip whose button has changed meaning (the theme
        toggle is the only one, and its icon changes with it)."""
        self._text = text
        self._hide()

    def _schedule(self, _event=None) -> None:
        self._cancel()
        try:
            self._after_id = self._widget.after(self.DELAY_MS, self._show)
        except tkinter.TclError:                        # widget already gone
            self._after_id = None

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except tkinter.TclError:
                pass
            self._after_id = None

    def _show(self) -> None:
        self._after_id = None
        if self._window is not None:
            return
        try:
            if not self._widget.winfo_viewable():
                return
            x = self._widget.winfo_rootx()
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        except tkinter.TclError:
            return
        win = tkinter.Toplevel(self._widget)
        # Held from here on, not after the label: an overrideredirect Toplevel
        # that nobody has a reference to cannot be closed by anything, and an
        # empty one is a bare white 200x200 square pinned topmost over the app.
        self._window = win
        win.wm_overrideredirect(True)                   # no title bar, no border
        win.wm_geometry(f"+{x}+{y}")
        try:
            win.wm_attributes("-topmost", True)
        except tkinter.TclError:
            pass
        try:
            tkinter.Label(
                win, text=self._text,
                background=_plain_color(gui_theme.SURFACE_CONTAINER_HIGH),
                foreground=_plain_color(gui_theme.ON_SURFACE),
                borderwidth=1, relief="solid",
                padx=7, pady=3,
            ).pack()
        except tkinter.TclError:
            self._hide()

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except tkinter.TclError:
                pass
            self._window = None


def _tooltip(widget, text: str):
    """Attach a hover label and hand the widget straight back.

    Returns the widget so a tooltip can be wrapped around a button at the point
    it is created, without breaking the .pack() chain these builders are
    written in.
    """
    _Tooltip(widget, text)
    return widget


# ---------------------------------------------------------------------------
# App — mixes CTk (customtkinter) with TkinterDnD drag-and-drop support
# ---------------------------------------------------------------------------

class App(ctk.CTk, TkinterDnD.DnDWrapper):

    # The header is a fixed-height strip with pack_propagate off, and in-window
    # screens are placed directly below it -- see _push_panel().
    # 72, not the old 52: the masthead stacks a 38px mark against a two-line
    # wordmark, and the band is what the panel stack positions itself below
    # (_show_panel offsets by this constant), so an unchanged 52 would clip the
    # eyebrow AND slide every panel up under it. Android's is 88dp; Windows
    # runs tighter because a desktop window has a title bar above it already.
    _HEADER_HEIGHT = 72

    def __init__(self) -> None:
        super().__init__()
        self.TkdndVersion = TkinterDnD._require(self)

        self.title("Chat Mail Sync")
        # Title bar, taskbar and Alt-Tab. Best-effort on purpose: a missing or
        # unreadable icon is a cosmetic defect and must never stop the app from
        # starting. iconbitmap() is the Windows path (it takes a real .ico, so
        # Windows picks the right size per surface); on other platforms it
        # raises TclError and the window simply keeps the default.
        _icon = _app_icon_path()
        if _icon:
            try:
                self.iconbitmap(default=str(_icon))
            except Exception:
                pass
        self.geometry("800x580")
        self.minsize(700, 500)

        # Ensure directories and DB exist.
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        init_db(STATE_DB_PATH)

        # Raw prior-state check, BEFORE loading settings (which resolves
        # mail_backend and so erases the evidence) -- used only to decide
        # whether to show the one-time OAuth-removed notice below.
        _was_oauth_user = _legacy_oauth_evidence()

        # Load persisted settings.
        _settings = _load_settings()
        self._settings: dict = _settings
        self._auto_refresh_ms: int = _AUTO_REFRESH_OPTIONS.get(
            _settings.get("auto_refresh_label", "30 s"), 30_000
        )

        # Runtime state.
        self._transport     = None
        self._worker: SyncWorker | None = None
        self._log_lines: list[str] = []
        self._theme_mode    = _saved_theme
        # Watched folder: a scan runs on a daemon thread (the folder can be a
        # slow network or cloud-synced path) and reports back through this
        # queue, so a poll never freezes the window.
        self._watch_q: queue.Queue = queue.Queue()
        self._watch_scanning = False
        self._watch_after_id = None
        self._last_run_dry_run = False
        # What the bar and its label say, derived by the shared core rather
        # than by this window -- see src/progress.py.
        self._progress = ProgressTracker()
        # In-window screens, innermost last. Android pushes SettingsScreen and
        # MailAccountScreen onto a nav stack rather than opening dialogs, and
        # this is the same idea: settings stays alive underneath while the mail
        # account is open, so coming back does not lose unsaved edits.
        self._panels: list = []

        # Build UI — footer must be packed before main so it pins to bottom.
        self._build_header()
        self._build_footer()
        self._build_main()

        # Apply saved settings to UI controls.
        self._chunk_var.set(_settings.get("chunk_size", "day"))
        self._update_signout_button_label()

        # Dismiss the PortableApps launcher splash as soon as this window is
        # actually on screen, rather than letting it run out its timer. See
        # _dismiss_splash_when_mapped().
        self.bind("<Map>", self._dismiss_splash_when_mapped)

        # Escape closes the innermost in-window screen -- what it used to do
        # when these were pop-up windows, and the habit outlives the pop-ups.
        self.bind("<Escape>", lambda _e: self._pop_panel())

        # Initial data load. The auth check is deferred rather than inline --
        # see _check_auth_deferred(); it is the one step here that can go to
        # the network, and it ran before mainloop().
        self._check_auth_deferred()
        self._refresh_chat_list()
        self._refresh_inbox_count()
        self._maybe_show_oauth_removed_notice(_was_oauth_user)

        # Schedule periodic inbox refresh (0 = Off).
        if self._auto_refresh_ms > 0:
            self.after(self._auto_refresh_ms, self._auto_refresh_inbox)

        # Watched folder. Reconcile the pending ledger first: the app may have
        # been closed between a sync finishing and its synced-file rule being
        # applied, and inbox/ still holds the answer.
        self._apply_synced_file_policies()
        self._update_watch_ui()
        # One scan now, before arming the timer. The timer's first fire is a
        # whole interval away, so a file dropped into the watched folder while
        # the app was closed sat there unnoticed -- with the app open, idle,
        # and looking straight at it -- for as long as the interval is. Gated
        # on the same switch as the timer: auto-watch off means no scan here
        # either. "Check now" stays the way to force one regardless.
        if _should_scan_at_launch(
            bool(self._settings.get("auto_watch_enabled")),
            self._watched_folder(),
        ):
            self._run_watch_scan(manual=False)
        self._schedule_watch_timer()

    # ------------------------------------------------------------------
    # Launcher splash
    # ------------------------------------------------------------------

    def _dismiss_splash_when_mapped(self, _event=None) -> None:
        """End the PortableApps splash now that this window is on screen.

        Bound to <Map>, and unbinds itself on the first fire: <Map> is emitted
        again on restore from minimise and on some monitor changes, and there
        is nothing to dismiss by then.

        The search runs on a daemon thread rather than inline. It polls for up
        to ~2 s (our window can be mapped before the splash has painted), and
        two seconds of polling inside a <Map> handler would freeze the window
        at the exact moment it becomes visible -- trading a cosmetic problem
        for a real one. Same shape as _check_auth_deferred() below.

        Nothing consumes the result: a False return is the normal outcome from
        source, on non-Windows, or whenever the launcher is not involved.
        """
        self.unbind("<Map>")
        threading.Thread(target=dismiss_launcher_splash, daemon=True).start()

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        # The masthead, not a grey strip: a postmark-blue band carrying the
        # app's mark beside a serif wordmark, which is what Android's
        # ChatMailTopBar has drawn since the identity work. Two front-ends of
        # the same product should be recognisable as the same product, and
        # until now the only thing they shared was the word "Sync".
        hdr = ctk.CTkFrame(
            self, height=self._HEADER_HEIGHT, corner_radius=0,
            fg_color=gui_theme.PRIMARY,
        )
        hdr.pack(fill="x", side="top")
        hdr.pack_propagate(False)

        brand = ctk.CTkFrame(hdr, fg_color="transparent")
        brand.pack(side="left", padx=(14, 0))

        # Best-effort, like the title-bar icon: a missing or unreadable mark is
        # cosmetic, and must never be the reason the window fails to build.
        # Held on self because Tk keeps only a weak reference to a PhotoImage
        # and a locally-scoped one is garbage-collected into a blank square.
        #
        # CustomTkinter warns that this is not a CTkImage and so will not scale
        # on a HighDPI display. Accepted, not overlooked: CTkImage is a Pillow
        # wrapper, and Pillow is excluded from the bundle (chat-mail-sync.spec)
        # -- it is not in the shipped app at all. The cost is a mark that stays
        # 38 physical pixels while text around it grows; the alternative is
        # several MB of imaging library for one logo.
        self._masthead_img = None
        _mark = _masthead_image_path()
        if _mark:
            try:
                img = tkinter.PhotoImage(file=str(_mark))
                self._masthead_img = img.subsample(2, 2)   # 75px -> 38px
            except Exception:
                self._masthead_img = None
        if self._masthead_img is not None:
            ctk.CTkLabel(
                brand, text="", image=self._masthead_img,
            ).pack(side="left", padx=(0, 10))

        words = ctk.CTkFrame(brand, fg_color="transparent")
        words.pack(side="left")

        ctk.CTkLabel(
            words,
            text="Chat Mail Sync",
            font=ctk.CTkFont(family=gui_theme.SERIF_FAMILY, size=17, weight="bold"),
            text_color=gui_theme.ON_PRIMARY,
            anchor="w",
        ).pack(anchor="w")

        # Android's masthead eyebrow, in the same small-caps-with-tracking
        # treatment. Tk has no letter-spacing, so the spacing is in the string.
        ctk.CTkLabel(
            words,
            text="W H A T S A P P   →   Y O U R   M A I L B O X",
            font=ctk.CTkFont(size=9, weight="bold"),
            text_color=gui_theme.ON_PRIMARY,
            anchor="w",
        ).pack(anchor="w")

        # Auth section (right-aligned inside header).
        auth_frame = ctk.CTkFrame(hdr, fg_color="transparent")
        auth_frame.pack(side="right", padx=12)

        self._auth_dot = ctk.CTkLabel(
            auth_frame, text="●", text_color=gui_theme.STATUS_COLOR_ON_BAND["failed"],
            font=ctk.CTkFont(size=16), width=20,
        )
        self._auth_dot.pack(side="left", padx=(0, 4))

        # Fixed width, like the dot above. This label is filled in by a
        # background check (_check_auth_deferred) and its text varies a lot --
        # "Checking…", "Connected", "No credentials.json", "Token invalid —
        # reconnect". auth_frame is packed to the right, so an auto-sized label
        # would drag the dot and the Connect button sideways every time the
        # status changed, most visibly on the settle from "Checking…" at
        # startup. 180px fits the longest of those at size 12; anchor="w" keeps
        # the text left-aligned within it rather than jittering about the
        # centre.
        self._auth_label = ctk.CTkLabel(
            auth_frame, text="Not connected",
            font=ctk.CTkFont(size=12), width=180, anchor="w",
            text_color=gui_theme.ON_PRIMARY,
        )
        self._auth_label.pack(side="left", padx=(0, 10))

        # The dot and its words open the mail account screen, the same as
        # Android's connection pill. Windows already has a Connect button an
        # inch to the right, so this is symmetry rather than rescue -- but a
        # status that names the account is the obvious thing to click when you
        # want to change the account, and it cost two bindings to make that
        # true. Cursor and tooltip say so before the click, since a plain
        # label gives no other sign it is live.
        for _w in (self._auth_dot, self._auth_label):
            _hand_cursor(_w)
            _w.bind("<Button-1>", lambda _e: self._open_mail_account())
        _Tooltip(self._auth_label, "Open mail account")

        # Everything from here down sits on the band, so all of it takes the
        # band variants: a stock CTkButton is primary-filled and would be a
        # primary rectangle on a primary strip.
        self._auth_btn = ctk.CTkButton(
            auth_frame, text="Connect", width=90, height=30,
            fg_color=gui_theme.BAND_BUTTON_FG,
            hover_color=gui_theme.BAND_BUTTON_HOVER,
            text_color=gui_theme.BAND_BUTTON_TEXT,
            command=self._on_connect_click,
        )
        self._auth_btn.pack(side="left", padx=(0, 6))

        self._signout_btn = ctk.CTkButton(
            auth_frame, text="Sign Out", width=80, height=30,
            fg_color="transparent", border_width=1,
            border_color=gui_theme.ON_PRIMARY,
            hover_color=gui_theme.BAND_GHOST_HOVER,
            text_color=gui_theme.ON_PRIMARY,
            state="disabled",
            command=self._on_signout_click,
        )
        self._signout_btn.pack(side="left", padx=(0, 8))

        # Icon shows what mode you'll switch TO: ☀ = "go light", ☽ = "go dark"
        _theme_icon = "☽" if _saved_theme == "light" else "☀"
        self._theme_btn = ctk.CTkButton(
            auth_frame, text=_theme_icon, width=32, height=30,
            fg_color="transparent", border_width=1,
            border_color=gui_theme.ON_PRIMARY,
            hover_color=gui_theme.BAND_GHOST_HOVER,
            text_color=gui_theme.ON_PRIMARY,
            font=ctk.CTkFont(size=14),
            command=self._on_toggle_theme,
        )
        self._theme_btn.pack(side="left", padx=(0, 4))
        # Kept on the button as the icon flips, for the same reason the icon
        # flips: it names the destination, not the current state.
        self._theme_tip = _Tooltip(
            self._theme_btn,
            "Switch to dark theme" if _saved_theme == "light" else "Switch to light theme",
        )

        _tooltip(ctk.CTkButton(
            auth_frame, text="⚙", width=32, height=30,
            fg_color="transparent", border_width=1,
            border_color=gui_theme.ON_PRIMARY,
            hover_color=gui_theme.BAND_GHOST_HOVER,
            text_color=gui_theme.ON_PRIMARY,
            font=ctk.CTkFont(size=14),
            command=self._open_settings,
        ), "Settings").pack(side="left")

        _tooltip(ctk.CTkButton(
            auth_frame, text="?", width=32, height=30,
            fg_color="transparent", border_width=1,
            border_color=gui_theme.ON_PRIMARY,
            hover_color=gui_theme.BAND_GHOST_HOVER,
            text_color=gui_theme.ON_PRIMARY,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._open_help,
        ), "Help").pack(side="left", padx=(4, 0))

    # ------------------------------------------------------------------
    # Footer  (packed before main so it stays pinned to the bottom)
    # ------------------------------------------------------------------

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, corner_radius=0, height=198)
        footer.pack(fill="x", side="bottom", padx=8, pady=(4, 8))
        footer.pack_propagate(False)

        # Test-run banner. A rehearsal mistaken for the real thing is the
        # worst outcome this app has -- the person believes their chats are in
        # the mailbox and they are not -- so while it is armed it says so where
        # the eye already is: directly above the button that starts the run.
        # Created here, packed and unpacked by _on_dry_run_toggle. Same words
        # and the same container colour as Android's banner on Home.
        self._dry_run_banner = ctk.CTkLabel(
            footer,
            text="  Test run is on — nothing will be sent to your mailbox",
            font=ctk.CTkFont(size=11),
            fg_color=gui_theme.SECONDARY_CONTAINER,
            text_color=gui_theme.ON_SECONDARY_CONTAINER,
            corner_radius=6, height=22, anchor="w",
        )

        # ── Sync button + progress bar row ────────────────────────────
        ctrl = ctk.CTkFrame(footer, fg_color="transparent")
        ctrl.pack(fill="x", pady=(8, 4), padx=6)
        # Kept so the banner can be packed *above* it after the fact.
        self._sync_ctrl_row = ctrl

        self._sync_btn = ctk.CTkButton(
            ctrl, text="▶  Sync Now", width=126, height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._on_sync_click,
        )
        self._sync_btn.pack(side="left", padx=(0, 6))

        self._stop_btn = ctk.CTkButton(
            ctrl, text="⏹  Stop", width=90, height=36,
            font=ctk.CTkFont(size=13),
            fg_color=gui_theme.ERROR, hover_color=gui_theme.ERROR_HOVER,
            state="disabled",
            command=self._on_stop_click,
        )
        self._stop_btn.pack(side="left", padx=(0, 10))

        self._progress_bar = ctk.CTkProgressBar(ctrl, height=14)
        self._progress_bar.pack(side="left", fill="x", expand=True)
        self._progress_bar.set(0)

        self._progress_label = ctk.CTkLabel(
            ctrl, text="", font=ctk.CTkFont(size=11),
            # Wide enough for the longest live line -- "Syncing: <chat> --
            # 1234 / 56789 messages". At the old 160px that was clipped down
            # to roughly the chat name and nothing else.
            width=300, anchor="w",
        )
        self._progress_label.pack(side="left", padx=(8, 0))

        # ── Stats bar ─────────────────────────────────────────────────
        stats_row = ctk.CTkFrame(footer, fg_color="transparent")
        stats_row.pack(fill="x", padx=10, pady=(0, 2))

        self._footer_stats_label = ctk.CTkLabel(
            stats_row, text="",
            font=ctk.CTkFont(size=11),
            text_color=gui_theme.ON_SURFACE_VARIANT,
            anchor="w",
        )
        self._footer_stats_label.pack(side="left", fill="x", expand=True)

        # Right here, and not in the header with the other screens, because
        # the box below it is the reason anyone wants it: the live log holds
        # 200 lines and empties on close, and the person staring at it asking
        # "what happened on Tuesday?" is exactly who this button is for. On
        # Android the same screen hangs off Settings and off a finished run on
        # Home -- both places where the question gets asked there.
        ctk.CTkButton(
            stats_row, text="Sync log  ›", width=88, height=22,
            font=ctk.CTkFont(size=11),
            fg_color="transparent", border_width=1,
            border_color=gui_theme.OUTLINE_VARIANT,
            hover_color=gui_theme.NEUTRAL_HOVER,
            text_color=gui_theme.ON_SURFACE_VARIANT,
            command=self._open_sync_log,
        ).pack(side="right")

        # ── Last-run status ───────────────────────────────────────────
        # The stats to the left say how much is archived; they cannot say
        # whether the last attempt to archive anything actually worked. A
        # watched folder syncs with nobody looking, so a failure could sit
        # unnoticed until someone thought to open the log. These three widgets
        # are Windows' half of Home's status block -- the same summary over
        # the same 90-day window from the same shared query
        # (state.summarize_recent_runs) as Android's SyncStatusBlock. They sit
        # here rather than in a box of their own because the answer belongs
        # beside the button that leads to the detail, not in a fourth place to
        # look.
        #
        # Packed after the button and before each other, because side="right"
        # stacks inwards: the reading order left-to-right is dot, outcome,
        # failures, [Sync log ›].
        self._footer_fail_label = ctk.CTkLabel(
            stats_row, text="",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=gui_theme.ERROR,
            anchor="e",
        )
        self._footer_fail_label.pack(side="right", padx=(0, 10))

        self._footer_status_label = ctk.CTkLabel(
            stats_row, text="",
            font=ctk.CTkFont(size=11),
            text_color=gui_theme.ON_SURFACE_VARIANT,
            anchor="e",
        )
        self._footer_status_label.pack(side="right")

        self._footer_status_dot = ctk.CTkLabel(
            stats_row, text="", text_color=gui_theme.STATUS_COLOR[None],
            font=ctk.CTkFont(size=13), width=14,
        )
        self._footer_status_dot.pack(side="right", padx=(8, 2))

        # ── Backup staleness ─────────────────────────────
        # A line, in the window, with the fix attached -- not a dialog and not
        # a banner over the sync button. Nothing here is urgent, but the cost
        # of never reading it is every chat mailed a second time after a
        # reinstall into a mailbox that cannot tell the copies apart. Android
        # carries the identical line on Home, under its status block.
        self._backup_row = ctk.CTkFrame(footer, fg_color="transparent")

        self._backup_stale_label = ctk.CTkLabel(
            self._backup_row, text="",
            font=ctk.CTkFont(size=11),
            text_color=gui_theme.ON_SURFACE_VARIANT,
            anchor="w", justify="left",
        )
        self._backup_stale_label.pack(side="left", fill="x", expand=True)

        ctk.CTkButton(
            self._backup_row, text="Back up", width=70, height=22,
            font=ctk.CTkFont(size=11),
            fg_color="transparent", border_width=1,
            border_color=gui_theme.OUTLINE_VARIANT,
            hover_color=gui_theme.NEUTRAL_HOVER,
            text_color=gui_theme.ON_SURFACE_VARIANT,
            command=self._open_settings,
        ).pack(side="right")

        self._refresh_backup_staleness()

        # ── Log textbox ───────────────────────────────────────────────
        self._log_box = ctk.CTkTextbox(
            footer, height=118,
            font=ctk.CTkFont(family="Courier New", size=11),
            wrap="word",
        )
        self._log_box.pack(fill="x", padx=6, pady=(0, 6))
        self._log_box.configure(state="disabled")

    # ------------------------------------------------------------------
    # Main area (chat list | drop zone + options)
    # ------------------------------------------------------------------

    def _build_main(self) -> None:
        main = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=8, pady=(6, 0))

        self._build_chat_panel(main)
        self._build_right_panel(main)

    # ── Left panel: chat list ──────────────────────────────────────────

    def _build_chat_panel(self, parent: ctk.CTkFrame) -> None:
        left = ctk.CTkFrame(parent, width=236, corner_radius=8)
        left.pack(side="left", fill="y", padx=(0, 6))
        left.pack_propagate(False)

        # Filter entry + refresh button on the same row.
        filter_row = ctk.CTkFrame(left, fg_color="transparent")
        filter_row.pack(fill="x", padx=8, pady=(8, 4))

        self._filter_var = ctk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._refresh_chat_list())
        ctk.CTkEntry(
            filter_row, placeholder_text="Filter chats…",
            textvariable=self._filter_var, height=32,
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))

        _tooltip(ctk.CTkButton(
            filter_row, text="⟳", width=32, height=32,
            font=ctk.CTkFont(size=15),
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._refresh_all,
        ), "Refresh the chat list").pack(side="left", padx=(0, 4))

        _tooltip(ctk.CTkButton(
            filter_row, text="CSV", width=38, height=32,
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._on_export_csv_click,
        ), "Export this list as a CSV file").pack(side="left")

        # ── Status chips ──────────────────────────────────────────────
        # The text box filters by name, which only helps someone who already
        # knows which chat they want. "Which of these failed?" was not
        # answerable at all without reading every dot in the list. Counts live
        # on the chips so the question is usually answered without a click --
        # and they count the whole list, not the current filter, so a chip
        # never reports zero merely because it is the one selected.
        # Two rows of two rather than one row of four: this panel is a fixed
        # 236px, and "Never synced (12)" on a 55px chip is a chip that says
        # "Neve…". Same four labels as Android, which has the width for one
        # scrolling row -- the wording is what has to match, not the geometry.
        self._chat_status_filter = "all"
        chips = ctk.CTkFrame(left, fg_color="transparent")
        chips.pack(fill="x", padx=8, pady=(0, 4))
        self._chat_chips: dict = {}
        for i, key in enumerate(_CHAT_FILTERS):
            btn = ctk.CTkButton(
                chips, text=_CHAT_FILTER_LABELS[key], height=24,
                font=ctk.CTkFont(size=10),
                command=lambda k=key: self._set_chat_status_filter(k),
            )
            btn.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, 3), pady=(0, 3))
            self._chat_chips[key] = btn
        chips.grid_columnconfigure((0, 1), weight=1, uniform="chip")

        self._chat_scroll = ctk.CTkScrollableFrame(left, label_text="Synced chats")
        self._chat_scroll.pack(fill="both", expand=True, padx=4, pady=(0, 6))

    # ── Right panel: drop zone + options ──────────────────────────────

    def _build_right_panel(self, parent: ctk.CTkFrame) -> None:
        right = ctk.CTkFrame(parent, corner_radius=0, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)

        # Drop zone.
        drop = ctk.CTkFrame(right, corner_radius=10, border_width=2, border_color=gui_theme.PRIMARY)
        drop.pack(fill="both", expand=True, pady=(0, 6))

        drop.drop_target_register(DND_FILES)
        drop.dnd_bind("<<Drop>>", self._on_files_dropped)

        ctk.CTkLabel(
            drop, text="⬇   Drop  .txt  or  .zip  export files here",
            font=ctk.CTkFont(size=13), text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(pady=(14, 4))

        btn_row = ctk.CTkFrame(drop, fg_color="transparent")
        btn_row.pack(pady=(0, 6))

        # One way in, not two. This used to be [Browse Files...], which opened
        # the Explorer dialog straight away; that dialog now lives *inside* the
        # import screen as its secondary "Pick a file from anywhere...", so the
        # two never compete for the same tap. Same rule as Android, where a
        # granted folder replaces the system picker rather than sitting beside
        # it. The label demotes itself once something is queued -- with files
        # waiting, Sync Now is the action, and two primary-looking buttons in
        # one box would argue about which.
        self._import_btn = ctk.CTkButton(
            btn_row, text="Choose exports to import", width=182, height=30,
            command=self._open_import_picker,
        )
        self._import_btn.pack(side="left", padx=6)

        ctk.CTkButton(
            btn_row, text="Open Inbox Folder", width=148, height=30,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=lambda: os.startfile(str(INBOX_DIR)),
        ).pack(side="left", padx=6)

        # The watched folder's "do it now" button -- Android puts the same one
        # in Settings; here it belongs beside the other two ways of getting
        # files in. Hidden entirely until a folder is chosen, so nobody meets a
        # permanently dead button. Like Android's, it runs whether or not the
        # periodic watch is switched on: choosing a folder is enough.
        #
        # The label used to stop at "Check watched folder", which was only half
        # true: a check that finds something goes straight on to sync it, via
        # _maybe_auto_sync. A button that sends mail should say it sends mail.
        # Android's says "Check and sync" -- it has a "Watched folder" heading
        # over it to supply the noun, and this one, sitting in a row of
        # unrelated buttons, does not.
        self._watch_now_btn = ctk.CTkButton(
            btn_row, text="Check watched folder and sync", width=214, height=30,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._on_check_watch_now,
        )

        # Packed by _refresh_inbox_count only once the queue is long enough to
        # be worth managing, so it is never a button with nothing behind it.
        self._queue_btn = ctk.CTkButton(
            btn_row, text="Manage queue…", width=150, height=30,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=lambda: self._push_panel(_QueuePanel),
        )

        # File list — the files currently sitting in the inbox folder, with
        # the same two per-row actions Android's queue has.
        #
        # It takes the slack in the drop zone rather than staying nailed at
        # 100px: the drop zone already expands with the window, so the fixed
        # height meant a maximised window showed six rows and a wide band of
        # nothing under them. Growing here is safe in a way it is not on
        # Android -- Sync Now lives outside this box, in the options row below,
        # so no length of list can push it off the screen. That is also why
        # this list is not capped at four the way Home's is on the phone.
        self._file_list_frame = ctk.CTkScrollableFrame(
            drop, label_text="Files in inbox", height=100,
        )
        self._file_list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        # Inline, under the list, hidden until asked for: a preview is a few
        # lines of fact about one file and does not deserve a window of its
        # own, and this app does not open pop-ups.
        self._preview_frame = ctk.CTkFrame(drop, corner_radius=8)
        self._preview_label = ctk.CTkLabel(
            self._preview_frame, text="", anchor="w", justify="left",
            font=ctk.CTkFont(size=11), text_color=gui_theme.ON_SURFACE,
        )
        self._preview_label.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=6)
        ctk.CTkButton(
            self._preview_frame, text="✕", width=26, height=26,
            fg_color="transparent", hover_color=gui_theme.NEUTRAL_HOVER,
            text_color=gui_theme.ON_SURFACE_VARIANT,
            command=self._hide_preview,
        ).pack(side="right", padx=(0, 8))

        self._inbox_label = ctk.CTkLabel(
            drop, text="", font=ctk.CTkFont(size=11), text_color=gui_theme.ON_SURFACE_VARIANT,
        )
        self._inbox_label.pack(pady=(0, 10))

        # Options row.
        opts = ctk.CTkFrame(right, corner_radius=8, height=46)
        opts.pack(fill="x")
        opts.pack_propagate(False)

        # "Test run", not "Dry run": the same words Android uses, and the ones
        # a person who has never met the term can act on. It stays on the main
        # window rather than moving into Settings as Android's did, because
        # this one is deliberately per-session -- see _on_dry_run_toggle.
        self._dry_run_var = ctk.BooleanVar(value=False)
        _tooltip(
            ctk.CTkCheckBox(
                opts, text="Test run",
                variable=self._dry_run_var, height=28,
                command=self._on_dry_run_toggle,
            ),
            "Rehearses the sync and shows what would happen. Writes nothing "
            "to your mailbox.",
        ).pack(side="left", padx=14, pady=8)

        ctk.CTkLabel(opts, text="Chunk size:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(20, 4))
        self._chunk_var = ctk.StringVar(value="day")
        ctk.CTkOptionMenu(
            opts, values=["day", "hour", "week"],
            variable=self._chunk_var, width=96, height=28,
        ).pack(side="left")

    # ------------------------------------------------------------------
    # Chat list
    # ------------------------------------------------------------------

    def _refresh_all(self) -> None:
        """Refresh both the chat list (from DB) and the inbox file count.

        Called by the ⟳ button so the user can pick up files added to
        the inbox folder after the app was already open.
        """
        self._refresh_chat_list()
        self._refresh_inbox_count()

    def _on_export_csv_click(self) -> None:
        """Export the chat sync summary to a CSV file chosen by the user."""
        try:
            rows = get_sync_summary(STATE_DB_PATH)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            return

        if not rows:
            messagebox.showinfo("Export", "No chats to export yet.")
            return

        dest = filedialog.asksaveasfilename(
            title="Save chat list as CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile="chat_mail_sync_export.csv",
        )
        if not dest:
            return  # user cancelled

        try:
            with open(dest, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=[
                    "chat_name", "status", "last_synced", "messages_synced", "source_file",
                ])
                writer.writeheader()
                for r in rows:
                    writer.writerow({
                        "chat_name":       r.get("display_name", ""),
                        "status":          r.get("last_run_status") or "",
                        "last_synced":     r.get("last_run_at") or "",
                        "messages_synced": r.get("messages_synced") or 0,
                        "source_file":     r.get("source_filename") or "",
                    })
            self._append_log(f"Exported {len(rows)} chat(s) to {dest}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def _auto_refresh_inbox(self) -> None:
        """Timer-based inbox refresh — fires every self._auto_refresh_ms when idle."""
        if self._worker is None:
            self._refresh_inbox_count()
        if self._auto_refresh_ms > 0:
            self.after(self._auto_refresh_ms, self._auto_refresh_inbox)

    def _refresh_chat_list(self) -> None:
        for w in self._chat_scroll.winfo_children():
            w.destroy()

        try:
            rows = get_sync_summary(STATE_DB_PATH)
        except Exception:
            rows = []

        # The full list, before either filter, is what the chips count and
        # what the footer totals -- one read, used for both.
        all_rows = list(rows)
        self._sync_chat_chips(all_rows)

        filt = self._filter_var.get().strip().lower()
        if filt:
            rows = [r for r in rows if filt in r["display_name"].lower()]
        status_filter = getattr(self, "_chat_status_filter", "all")
        if status_filter != "all":
            rows = [r for r in rows if _chat_status_of(r) == status_filter]

        total_chats = len(all_rows)
        total_msgs  = sum(r.get("messages_synced") or 0 for r in all_rows)

        # Most recent sync timestamp across all chats.
        last_sync_str = ""
        last_run_timestamps = [r.get("last_run_at") for r in all_rows if r.get("last_run_at")]
        if last_run_timestamps:
            try:
                latest = max(last_run_timestamps)
                dt = datetime.fromisoformat(latest)
                # Use dt.day (int) to avoid platform-specific %-d strftime flag.
                last_sync_str = f"  ·  last sync {dt.strftime('%b')} {dt.day}, {dt.strftime('%H:%M')}"
            except Exception:
                pass

        self._footer_stats_label.configure(
            text=f"{total_chats} chat{'s' if total_chats != 1 else ''}  ·  "
                 f"{total_msgs} message{'s' if total_msgs != 1 else ''} synced"
                 + last_sync_str
        )
        self._refresh_footer_status()

        if not rows:
            self._add_empty_chat_state(
                filtered=bool(filt), query=filt,
                status_filter=status_filter, had_any=bool(all_rows),
            )
            return

        for row in rows:
            self._add_chat_row(row)

    def _refresh_footer_status(self) -> None:
        """Say whether the last sync worked, and whether anything is failing.

        Blank until something has run: an empty outcome on a fresh install is
        a line explaining that there is nothing to explain. Mirrors Android,
        where SyncStatusBlock is absent for the same reason.
        """
        try:
            summary = summarize_recent_runs(90, STATE_DB_PATH)
        except Exception:
            # A summary is a convenience. Failing to read it must never be the
            # reason the chat list stops refreshing.
            summary = None

        if not summary or not summary.get("total_runs"):
            self._footer_status_dot.configure(text="")
            self._footer_status_label.configure(text="")
            self._footer_fail_label.configure(text="")
            return

        status = summary.get("last_status")
        self._footer_status_dot.configure(
            text="●", text_color=gui_theme.STATUS_COLOR.get(status, gui_theme.OUTLINE),
        )
        if status is None:
            # Runs exist but none has finished - one is going on right now,
            # and the progress bar two rows up is already saying so.
            self._footer_status_label.configure(text="Sync in progress")
        else:
            headline = "Last sync failed" if status == "failed" else "Last sync"
            self._footer_status_label.configure(
                text=f"{headline} {_relative_time(summary.get('last_completed_at'))}"
                     f"  ·  {_summary_counts_text(summary)}"
            )

        failed = summary.get("failed_runs") or 0
        self._footer_fail_label.configure(
            text=f"{failed} run{'s' if failed != 1 else ''} failed in 90 days" if failed else ""
        )

    def _refresh_backup_staleness(self) -> None:
        """Show the line only when there is something to say.

        Hidden entirely once a recent backup exists: a permanent "you are
        fine" row is one more thing to stop reading, and the row that matters
        then reads as furniture.
        """
        row = getattr(self, "_backup_row", None)
        if row is None or not row.winfo_exists():
            return
        at = int(self._settings.get("last_backup_at") or 0)
        if not _backup_is_stale(at):
            row.pack_forget()
            return
        self._backup_stale_label.configure(
            text="No backup yet. Without one, a reinstall makes the app mail "
                 "every chat again."
            if at <= 0 else
            _describe_last_backup(at) + " - old enough to be worth refreshing."
        )
        # Above the log box wherever it is shown from -- packing plain would
        # put a row that only appears later underneath it.
        box = getattr(self, "_log_box", None)
        if box is not None and box.winfo_exists():
            row.pack(fill="x", padx=10, pady=(0, 4), before=box)
        else:
            row.pack(fill="x", padx=10, pady=(0, 4))

    def _set_chat_status_filter(self, key: str) -> None:
        if key == getattr(self, "_chat_status_filter", "all"):
            return
        self._chat_status_filter = key
        self._refresh_chat_list()

    def _sync_chat_chips(self, all_rows: list) -> None:
        """Put live counts on the chips and mark the selected one."""
        counts = {"all": len(all_rows), "synced": 0, "failed": 0, "never": 0}
        for row in all_rows:
            counts[_chat_status_of(row)] += 1
        selected = getattr(self, "_chat_status_filter", "all")
        for key, btn in self._chat_chips.items():
            is_sel = key == selected
            btn.configure(
                text=f"{_CHAT_FILTER_LABELS[key]} ({counts[key]})",
                fg_color=gui_theme.PRIMARY if is_sel else "transparent",
                text_color=gui_theme.ON_PRIMARY if is_sel else gui_theme.ON_SURFACE,
                border_width=0 if is_sel else 1,
                border_color=gui_theme.OUTLINE_VARIANT,
                hover_color=gui_theme.PRIMARY_HOVER if is_sel else gui_theme.NEUTRAL_HOVER,
            )

    def _add_empty_chat_state(
        self, *, filtered: bool, query: str,
        status_filter: str = "all", had_any: bool = False,
    ) -> None:
        """Fill the empty chat list with instructions instead of blank space.

        This panel showed nothing at all on a first run -- the one moment the
        user most needs telling what to do. Same cases and the same words as
        Android's ChatsListScreen, which is where the copy was written.
        """
        wrap = ctk.CTkFrame(self._chat_scroll, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=12, pady=24)

        # A chip emptying the list is its own case: "No chats archived yet"
        # under a [Failed (0)] chip, on an inbox holding forty chats, is a
        # flat lie about the state of the app.
        if status_filter != "all" and had_any:
            ctk.CTkLabel(
                wrap, text=_CHAT_EMPTY_CHIP_TITLES[status_filter],
                font=ctk.CTkFont(size=13, weight="bold"),
                anchor="w", justify="left", wraplength=200,
            ).pack(fill="x")
            if filtered:
                ctk.CTkLabel(
                    wrap, text=f'The name filter "{query}" is also on.',
                    text_color=gui_theme.ON_SURFACE_VARIANT, wraplength=200,
                    anchor="w", justify="left",
                ).pack(fill="x", pady=(6, 10))
            ctk.CTkButton(
                wrap, text="Show all chats", width=130, height=28,
                fg_color="transparent", border_width=1,
                text_color=gui_theme.ON_SURFACE,
                command=lambda: self._set_chat_status_filter("all"),
            ).pack(anchor="w", pady=(6, 0))
            return

        if filtered:
            ctk.CTkLabel(
                wrap, text=f'No chats match "{query}".',
                font=ctk.CTkFont(size=13, weight="bold"),
                anchor="w", justify="left",
            ).pack(fill="x")
            ctk.CTkLabel(
                wrap,
                text="The filter matches chat names only, not message text.",
                text_color=gui_theme.ON_SURFACE_VARIANT, wraplength=280,
                anchor="w", justify="left",
            ).pack(fill="x", pady=(6, 10))
            ctk.CTkButton(
                wrap, text="Clear filter", width=110, height=28,
                fg_color="transparent", border_width=1,
                text_color=gui_theme.ON_SURFACE,
                # after(0) because _filter_var's trace rebuilds this list,
                # which destroys the very button whose command is running.
                # The refresh comes from the trace -- calling it here as well
                # would rebuild twice.
                command=lambda: self.after(0, lambda: self._filter_var.set("")),
            ).pack(anchor="w")
            return

        ctk.CTkLabel(
            wrap, text="No chats archived yet.",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w", justify="left",
        ).pack(fill="x")
        ctk.CTkLabel(
            wrap,
            text="In WhatsApp, open a chat and tap ⋮ → More → Export chat, "
                 "then drop the .zip or .txt here.",
            text_color=gui_theme.ON_SURFACE_VARIANT, wraplength=280,
            anchor="w", justify="left",
        ).pack(fill="x", pady=(6, 10))
        ctk.CTkButton(
            wrap, text="Browse files…", width=120, height=28,
            command=self._browse_files,
        ).pack(anchor="w")

    def _add_chat_row(self, row: dict) -> None:
        status = row.get("last_run_status")
        color  = _STATUS_COLOR.get(status, _STATUS_COLOR[None])
        chat_id        = row["chat_id"]
        display_name   = row["display_name"]
        source_filename= row.get("source_filename", "")
        synced         = status is not None

        frame = ctk.CTkFrame(self._chat_scroll, corner_radius=6)
        frame.pack(fill="x", pady=2, padx=2)

        # ── Top row: status dot + name + action buttons ────────────────
        top = ctk.CTkFrame(frame, fg_color="transparent")
        top.pack(fill="x", padx=6, pady=(5, 1))

        ctk.CTkLabel(
            top, text="●", text_color=color,
            font=ctk.CTkFont(size=12), width=16,
        ).pack(side="left")

        ctk.CTkLabel(
            top, text=display_name,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=(4, 4))


        # Resync button (only for chats that have been processed before).
        if synced:
            _tooltip(ctk.CTkButton(
                top, text="↺", width=22, height=20,
                font=ctk.CTkFont(size=11),
                fg_color="transparent", hover_color=gui_theme.PRIMARY_CONTAINER,
                text_color=gui_theme.ON_SURFACE_VARIANT,
                command=lambda cid=chat_id, dn=display_name, sf=source_filename:
                    self._on_resync_chat(cid, dn, sf),
            ), "Reset (forget sync history)").pack(side="right", padx=(0, 2))

        # Delete button.
        _tooltip(ctk.CTkButton(
            top, text="✕", width=22, height=20,
            font=ctk.CTkFont(size=11),
            fg_color="transparent", hover_color=gui_theme.ERROR_CONTAINER,
            text_color=gui_theme.ON_SURFACE_VARIANT,
            command=lambda cid=chat_id, dn=display_name, s=synced:
                self._on_delete_chat(cid, dn, s),
        ), "Delete from list").pack(side="right", padx=(0, 2))

        # ── Bottom row: last-sync date, message count, status ──────────
        bot = ctk.CTkFrame(frame, fg_color="transparent")
        bot.pack(fill="x", padx=(24, 4), pady=(0, 5))

        # Was "Jul 3  ·  142 msgs  ·  complete" -- three facts of different
        # kinds strung into one sentence, in a 236px column that clipped the
        # end of it. Now: when on the left, how much on the right, and the
        # status stays in the dot above rather than being said twice. "failed"
        # is the exception and is spelled out, in the error colour, because a
        # dot is the wrong amount of emphasis for the one row you must not
        # scroll past.
        last_run_at = row.get("last_run_at")
        when = ""
        if last_run_at:
            try:
                dt = datetime.fromisoformat(last_run_at)
                when = f"{dt.strftime('%b')} {dt.day}"
            except Exception:
                when = ""

        if status == "failed":
            left_text, left_color = "Failed", gui_theme.ERROR
            if when:
                left_text = f"Failed · {when}"
        elif when:
            left_text, left_color = when, gui_theme.ON_SURFACE_VARIANT
        else:
            left_text, left_color = "Not synced yet", gui_theme.ON_SURFACE_VARIANT

        ctk.CTkLabel(
            bot, text=left_text, anchor="w",
            font=ctk.CTkFont(size=10), text_color=left_color,
        ).pack(side="left")

        msgs = row.get("messages_synced") or 0
        if msgs:
            ctk.CTkLabel(
                bot, text=f"{msgs} msgs", anchor="e",
                font=ctk.CTkFont(size=10), text_color=gui_theme.ON_SURFACE_VARIANT,
            ).pack(side="right")

        # The row opens the chat, the way tapping a chat does on Android. Until
        # now the three glyphs were the whole of what a chat could be asked --
        # 22x20 targets with no labels, no room for the facts behind them, and
        # nothing at all for a chat that had never synced. Bound on the labels
        # too, since a click lands on whichever one is under the cursor rather
        # than on the frame; the glyph buttons swallow their own clicks, so
        # they keep working as the shortcuts they are.
        def open_chat(_event=None, r=row):
            self._open_chat_detail(r)

        for widget in (frame, top, bot, *bot.winfo_children()):
            widget.bind("<Button-1>", open_chat)
        for widget in top.winfo_children():
            if isinstance(widget, ctk.CTkLabel):
                widget.bind("<Button-1>", open_chat)

    def _open_chat_detail(self, row: dict) -> None:
        """Open one chat's own screen, mirroring Android's ChatDetailScreen."""
        self._push_panel(lambda app, master, r=row: _ChatDetailPanel(app, master, r))

    # ------------------------------------------------------------------
    # Inbox / file handling
    # ------------------------------------------------------------------

    def _refresh_inbox_count(self) -> None:
        try:
            files = sorted(
                (f for f in INBOX_DIR.iterdir()
                 if f.is_file() and f.suffix in (".txt", ".zip", "")),
                key=lambda f: f.name.lower(),
            )
            n = len(files)
        except Exception:
            files = []
            n = 0

        # Update the count label.
        text = f"{n} file{'s' if n != 1 else ''} ready to sync" if n else "Inbox is empty — drop files above"
        self._inbox_label.configure(text=text)
        self._import_btn.configure(
            text="Add more exports…" if n else "Choose exports to import"
        )

        # Repopulate the file list.
        for w in self._file_list_frame.winfo_children():
            w.destroy()
        if not files:
            ctk.CTkLabel(
                self._file_list_frame,
                text="No files in inbox.",
                font=ctk.CTkFont(size=11), text_color=gui_theme.ON_SURFACE_VARIANT,
            ).pack(anchor="w", padx=4, pady=2)
        else:
            # The chat name, not the filename. Every WhatsApp export is called
            # "WhatsApp Chat with <name>", so a column of raw filenames repeats
            # the same four words down the list and elides the only part that
            # tells one row from another -- in a 100px-high list, the part that
            # gets cut is exactly the name. Android's queue has always shown
            # the stripped name; this is the same rule, from the same shared
            # function. The real filename is one hover away.
            #
            # Preview and remove are per row here for the first time. Android
            # has had both since the queue existed, and without them the only
            # way to drop one wrongly-imported export was to open the inbox
            # folder in Explorer and delete the file by hand.
            for f in files:
                _, display = extract_chat_info(f.name)
                row = ctk.CTkFrame(self._file_list_frame, fg_color="transparent")
                row.pack(fill="x", anchor="w", padx=2, pady=1)
                _tooltip(
                    ctk.CTkLabel(
                        row, text=display, font=ctk.CTkFont(size=11), anchor="w",
                    ),
                    f.name,
                ).pack(side="left", fill="x", expand=True, padx=(4, 0))
                ctk.CTkButton(
                    row, text="✕", width=24, height=22,
                    fg_color="transparent", hover_color=gui_theme.NEUTRAL_HOVER,
                    text_color=gui_theme.ON_SURFACE_VARIANT,
                    font=ctk.CTkFont(size=11),
                    command=lambda n=f.name: self._remove_from_inbox(n),
                ).pack(side="right", padx=(0, 4))
                ctk.CTkButton(
                    row, text="Preview", width=58, height=22,
                    fg_color="transparent", hover_color=gui_theme.NEUTRAL_HOVER,
                    text_color=gui_theme.ON_SURFACE_VARIANT,
                    font=ctk.CTkFont(size=11),
                    command=lambda n=f.name: self._show_preview(n),
                ).pack(side="right", padx=(0, 2))

        # Bulk actions only appear when there is bulk to act on. Under five
        # files, removing them one ✕ at a time is faster than a screen change.
        self._queue_btn.pack_forget()
        if n > _QUEUE_BULK_THRESHOLD:
            self._queue_btn.configure(text=f"Manage queue ({n})…")
            self._queue_btn.pack(side="left", padx=6)

    def _show_preview(self, filename: str) -> None:
        """Parse one queued export and say what is in it, in place."""
        try:
            text = format_preview(preview_export(str(INBOX_DIR / filename)))
        except Exception as exc:
            # A preview is a convenience; it must never take the screen with it.
            text = f"This file could not be read: {exc}"
        self._show_preview_text(text)

    def _show_preview_text(self, text: str) -> None:
        self._preview_label.configure(text=text)
        self._preview_frame.pack(fill="x", padx=10, pady=(0, 6), before=self._inbox_label)

    def _hide_preview(self) -> None:
        self._preview_frame.pack_forget()

    def _remove_from_inbox(self, filename: str) -> None:
        """Take one file out of the queue.

        Deliberately not a recycle-bin delete of the user's own export: what
        this removes is the app's *copy* in its inbox, made at import time from
        a file still sitting wherever WhatsApp saved it. Android's ✕ has always
        meant exactly this, through the same function -- the queue screens on
        both platforms say so in as many words.
        """
        result = remove_from_inbox(filename)
        if not result.get("ok"):
            self._show_preview_text(
                f"Could not remove {filename}: {result.get('error')}"
            )
            return
        self._hide_preview()
        self._refresh_inbox_count()

    def _on_files_dropped(self, event) -> None:
        paths = self._parse_dnd_paths(event.data)
        self._copy_to_inbox(paths)

    @staticmethod
    def _parse_dnd_paths(raw: str) -> list[Path]:
        """Parse tkinterdnd2's brace-quoted path string into Path objects.

        On Windows, paths with spaces are wrapped in {braces}; plain paths
        are space-separated.
        """
        raw = raw.strip()
        if "{" in raw:
            return [Path(m.group(1)) for m in re.finditer(r"\{([^}]+)\}", raw)]
        return [Path(p) for p in raw.split()]

    def _browse_files(self) -> None:
        chosen = filedialog.askopenfilenames(
            title="Select WhatsApp export files",
            filetypes=[
                ("WhatsApp exports", "*.txt *.zip"),
                ("All files", "*.*"),
            ],
        )
        if chosen:
            self._copy_to_inbox([Path(f) for f in chosen])

    def _copy_to_inbox(self, paths: list[Path]) -> None:
        copied = 0
        skipped = 0
        for src in paths:
            if src.suffix.lower() not in (".txt", ".zip", ""):
                continue
            dest = INBOX_DIR / src.name
            if dest.exists():
                skipped += 1
                continue
            try:
                shutil.copy2(str(src), str(dest))
                copied += 1
            except Exception as exc:
                self._append_log(f"Could not copy {src.name}: {exc}")

        parts = []
        if copied:
            parts.append(f"Copied {copied} file{'s' if copied != 1 else ''} to inbox")
        if skipped:
            parts.append(f"{skipped} already present (skipped)")
        if parts:
            self._append_log(". ".join(parts) + ".")

        self._refresh_inbox_count()

    # ------------------------------------------------------------------
    # Watched folder
    #
    # The desktop half of Android's WatchFolderWorker. The rules the two share
    # live in src/watch_folder.py; what is platform-specific is only how the
    # poll is driven -- a Tk after() timer here, a WorkManager periodic job
    # there -- and the consequence that this one runs only while the app is
    # open. See PLATFORM-PARITY.md.
    # ------------------------------------------------------------------

    def _watched_folder(self) -> "Path | None":
        raw = str(self._settings.get("watched_folder_path") or "").strip()
        return Path(raw) if raw else None

    def _update_watch_ui(self) -> None:
        """Show "Check watched folder and sync" only once a folder is chosen."""
        if self._watched_folder() is not None:
            self._watch_now_btn.pack(side="left", padx=6)
        else:
            self._watch_now_btn.pack_forget()

    def _schedule_watch_timer(self) -> None:
        """(Re)arm the periodic scan. Cancels any timer already pending, so
        changing the interval in Settings takes effect on the existing
        schedule rather than running two timers at once -- the same reason
        Android's enqueue() uses UPDATE and not KEEP."""
        if self._watch_after_id is not None:
            try:
                self.after_cancel(self._watch_after_id)
            except Exception:
                pass
            self._watch_after_id = None

        if not self._settings.get("auto_watch_enabled") or self._watched_folder() is None:
            return
        minutes = max(
            int(self._settings.get("watch_interval_minutes", DEFAULT_WATCH_INTERVAL_MINUTES)),
            MIN_WATCH_INTERVAL_MINUTES,
        )
        self._watch_after_id = self.after(minutes * 60_000, self._watch_tick)

    def _watch_tick(self) -> None:
        self._watch_after_id = None
        self._run_watch_scan(manual=False)
        self._schedule_watch_timer()

    def _on_check_watch_now(self) -> None:
        """"Check now": runs immediately regardless of the periodic schedule,
        or of whether the periodic watch is even switched on."""
        self._run_watch_scan(manual=True)

    def _run_watch_scan(self, manual: bool) -> None:
        folder = self._watched_folder()
        if folder is None or self._watch_scanning:
            return

        self._watch_scanning = True
        self._watch_now_btn.configure(state="disabled", text="Checking…")

        # Copied out of settings before the thread starts; the thread must not
        # touch self._settings, which the main thread may be rewriting.
        already = list(self._settings.get("imported_source_paths", []))
        pending = dict(self._settings.get("pending_synced_files", {}))

        def _work() -> None:
            try:
                result = scan_watch_folder(folder, INBOX_DIR, already, pending)
            except Exception as exc:  # never let a scan take the app down
                self._watch_q.put({"error": str(exc)})
                return
            self._watch_q.put({"result": result, "pending": pending, "manual": manual})

        threading.Thread(target=_work, daemon=True).start()
        self.after(_POLL_MS, self._poll_watch_queue)

    def _poll_watch_queue(self) -> None:
        try:
            event = self._watch_q.get_nowait()
        except queue.Empty:
            self.after(_POLL_MS, self._poll_watch_queue)
            return

        self._watch_scanning = False
        self._watch_now_btn.configure(state="normal", text="Check watched folder and sync")

        if "error" in event:
            self._append_log(f"Watched folder: {event['error']}")
            return

        result = event["result"]
        for msg in result.errors:
            self._append_log(f"Watched folder: {msg}")

        # Ledger every source this pass accounted for, so the next tick does
        # not re-examine it. Sources that failed to copy are deliberately not
        # in there -- scan_watch_folder leaves those out to be retried.
        self._settings["imported_source_paths"] = result.ledger
        self._settings["pending_synced_files"] = event["pending"]
        _save_settings(self._settings)

        if result.imported:
            self._append_log(
                f"Watched folder: imported {result.imported_count} new "
                f"file{'s' if result.imported_count != 1 else ''}."
            )
            self._refresh_inbox_count()
        elif event["manual"]:
            # Only say so when the user asked; a silent periodic tick that
            # found nothing should stay silent.
            self._append_log("Watched folder: no new files found.")

        self._maybe_auto_sync(found_new=bool(result.imported), manual=event["manual"])

    def _maybe_auto_sync(self, found_new: bool, manual: bool) -> None:
        """Sync what the watcher just imported, without the user opening
        anything. Android made this call first, for the same reason: watched
        folders are meant to be hands-off end to end, not "import
        automatically, then still come back and press Sync"."""
        if not found_new and not _inbox_has_files():
            return
        if self._worker is not None:
            return  # a sync is already running; it will pick these up
        # A rehearsal writes nothing and needs no connection, so it is neither
        # blocked by a missing transport nor allowed to become a real send --
        # the toggle says it stays on until turned off, and this path is the
        # one the user is least likely to be watching when it does not.
        dry_run = bool(self._dry_run_var.get())
        if self._transport is None and not dry_run:
            # Same shape as WatchFolderWorker's "connect in the app to sync"
            # branch -- say it plainly rather than starting a run that is
            # certain to fail. The files stay in the inbox for the next try.
            if found_new or manual:
                self._append_log(
                    "Watched folder: files are waiting in the inbox — connect "
                    "to your mailbox to sync them."
                )
            return
        self._begin_sync(dry_run=dry_run, chunk_size=self._chunk_var.get())

    def _apply_synced_file_policies(self) -> None:
        """Act on sources whose inbox copy has since been delivered.

        Called after every real sync, and once at startup in case the app was
        closed in between. Never after a dry run: nothing was delivered, so
        moving or recycling the user's original would be plainly wrong.
        """
        pending = dict(self._settings.get("pending_synced_files", {}))
        if not pending:
            return
        remaining, messages = apply_pending_synced_file_policies(
            pending,
            INBOX_DIR,
            str(self._settings.get("synced_file_policy", "leave")),
        )
        for msg in messages:
            self._append_log(f"Watched folder: {msg}")
        if remaining != pending:
            self._settings["pending_synced_files"] = remaining
            _save_settings(self._settings)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def _on_sync_click(self) -> None:
        self._begin_sync(
            dry_run=self._dry_run_var.get(),
            chunk_size=self._chunk_var.get(),
        )

    def _sync_button_label(self) -> str:
        """What the run button should say when it is idle.

        The button is the last thing read before a run starts, so it names the
        run it is about to start rather than always claiming to sync.
        """
        return "▶  Run test sync" if self._dry_run_var.get() else "▶  Sync Now"

    def _on_dry_run_toggle(self) -> None:
        """Keep the run controls honest about which kind of run is armed.

        Deliberate parity exception, agreed rather than drifted into: Android
        moved this control into Settings because there it is *persisted*, and
        a persisted rehearsal that nobody notices means nothing ever reaches
        the mailbox. Windows' box is per-session -- it comes back unticked on
        every launch -- so it has no such foot-gun, and burying it in Settings
        would mean persisting it and importing the exact problem that move was
        meant to remove. What does carry across is the loudness: the same
        words, in the same place relative to the run button, on both.
        """
        self._sync_btn.configure(text=self._sync_button_label())
        if self._dry_run_var.get():
            self._dry_run_banner.pack(
                fill="x", padx=6, pady=(6, 0), before=self._sync_ctrl_row,
            )
        else:
            self._dry_run_banner.pack_forget()

    def _begin_sync(
        self, dry_run: bool, chunk_size: str, chat_filter: str | None = None
    ) -> None:
        """Start a sync. Split out of the button handler so the watched folder
        can start a real sync of its own without faking a click (and without
        inheriting whatever the Test run box happens to be set to -- an
        automatic run that quietly did nothing would be worse than useless).

        chat_filter narrows the run to one chat, which is what the chat detail
        panel's [Sync just this chat] asks for -- the same SyncManager filter
        cli.py exposes as --chat and Android reaches from ChatDetailScreen.
        """
        if self._worker is not None:
            return  # already running

        if not dry_run and self._transport is None:
            self._append_log("Not connected.  Connect first, or tick Test run.")
            return

        self._last_run_dry_run = dry_run

        # Reset UI state.
        self._sync_btn.configure(
            state="disabled", text="Test run…" if dry_run else "Syncing…",
        )
        self._stop_btn.configure(state="normal")
        self._progress.reset()
        self._progress_bar.set(0)
        self._progress_label.configure(text="Starting…")
        self._log_lines.clear()
        self._update_log_box()

        worker = SyncWorker(
            transport    = self._transport,
            chunk_size   = chunk_size,
            dry_run      = dry_run,
            db_path      = STATE_DB_PATH,
            inbox_dir    = INBOX_DIR,
            processed_dir= PROCESSED_DIR,
            chat_filter  = chat_filter,
        )
        self._worker = worker
        worker.start()
        self.after(_POLL_MS, self._poll_sync_queue)

    def _poll_sync_queue(self) -> None:
        # Bound to a local: _handle_sync_event() clears self._worker the moment
        # it sees "done" or "error", and the drain loop below re-read it on
        # every iteration -- so the very next get_nowait() raised
        # "AttributeError: 'NoneType' object has no attribute 'q'". That escaped
        # the Tk callback and killed the poll, which is what showed up as a
        # progress bar frozen mid-run over a log that had stopped updating.
        worker = self._worker
        if worker is None:
            return
        try:
            while self._worker is worker:
                self._handle_sync_event(worker.q.get_nowait())
        except queue.Empty:
            pass
        # Only keep polling if this call still owns the run: a finished or
        # replaced worker must not leave a second timer chain running.
        if self._worker is worker:
            self.after(_POLL_MS, self._poll_sync_queue)

    def _render_progress(self) -> None:
        """Paint bar and label from the shared ProgressState.

        Every label string and the monotonic-fraction rule live in
        src/progress.py, not here -- this window and Android's sync screen
        are showing the same run and must describe it identically, and
        keeping a second copy of the rules on each side is exactly how they
        stopped matching.
        """
        state = self._progress.state
        if state.fraction >= 0:
            self._progress_bar.set(state.fraction)
        self._progress_label.configure(text=state.line)

    def _handle_sync_event(self, event: dict) -> None:
        t = event["type"]
        # Every event goes through the tracker first, including the ones with
        # side effects below -- the label/bar it produces is the whole of
        # what those branches used to compute by hand.
        self._progress.feed(event)

        if t == "log":
            self._append_log(event["msg"])
            return

        self._render_progress()

        if t == "done":
            stats = event["stats"]
            self._append_log("─" * 48)
            self._append_log(str(stats))
            self._sync_btn.configure(state="normal", text=self._sync_button_label())
            self._stop_btn.configure(state="disabled")
            self._worker = None
            self._refresh_chat_list()
            self._refresh_inbox_count()
            if not self._last_run_dry_run:
                self._apply_synced_file_policies()

        elif t == "error":
            self._append_log(f"ERROR: {event['msg']}")
            self._sync_btn.configure(state="normal", text=self._sync_button_label())
            self._stop_btn.configure(state="disabled")
            self._worker = None
            self._refresh_chat_list()
            # After a failure too: some files in the run may still have been
            # delivered before it broke, and inbox/ is what says which.
            if not self._last_run_dry_run:
                self._apply_synced_file_policies()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _check_auth_deferred(self) -> None:
        """Run the *startup* auth check off the main thread.

        _check_auth() is synchronous, and it was once able to go to the
        network: the removed Gmail path refreshed an expired token inline from
        __init__ -- so, before mainloop() -- on google-auth's 120-second
        default timeout. A user with an expired token and a slow connection
        got no window at all until it resolved, which reads as a hung launch
        rather than a network problem. The check is file-only now, but it stays
        off the main thread: nothing on the path to the first window should be
        able to block it, and the next backend added here may not be local.

        Measured 2026-08-08 on the frozen 1.0.1 portable build, warm start
        3.0s on the IMAP backend, which is file-only and never touches the
        network in this check.

        Same shape as _silent_build_transport() below: do the blocking part on
        a daemon thread, hand the result back through a queue, and let the
        widget show an honest interim state until it arrives.
        """
        self._auth_label.configure(text="Checking…")
        auth_q: queue.Queue = queue.Queue()

        def _work() -> None:
            try:
                auth_q.put(check_auth_status())
            except Exception as exc:
                # Must post something, or the poller below reschedules forever.
                auth_q.put((False, f"Auth error: {exc}"))

        threading.Thread(target=_work, daemon=True).start()
        self.after(_AUTH_POLL_MS, lambda: self._poll_startup_auth(auth_q))

    def _poll_startup_auth(self, auth_q: "queue.Queue") -> None:
        try:
            valid, text = auth_q.get_nowait()
        except queue.Empty:
            self.after(_AUTH_POLL_MS, lambda: self._poll_startup_auth(auth_q))
            return
        self._apply_auth_status(valid, text)

    def _check_auth(self) -> None:
        valid, text = check_auth_status()
        self._apply_auth_status(valid, text)

    def _record_connection(self, ok: bool) -> None:
        """Remember the outcome of a real attempt to reach the mailbox.

        Called only where a login was genuinely tried -- never after a
        validation failure that stopped before the network, which would record
        a mailbox verdict on something the mailbox never saw. Mirrors
        ConnectionState.record() on Android.
        """
        self._settings["last_connection_ok"] = ok
        self._settings["last_connection_at"] = int(datetime.now().timestamp())
        _save_settings(self._settings)

    def _forget_connection(self) -> None:
        """The account changed underneath the verdict, so the verdict is no
        longer about the credentials in use. Mirrors ConnectionState.forget()."""
        self._settings["last_connection_ok"] = None
        self._settings["last_connection_at"] = 0
        _save_settings(self._settings)

    def _apply_auth_status(self, valid: bool, text: str) -> None:
        status, label = _auth_display(
            valid, text, self._settings.get("last_connection_ok")
        )
        self._auth_dot.configure(text_color=gui_theme.STATUS_COLOR_ON_BAND[status])
        self._auth_label.configure(text=label)
        # command restored alongside the text, not just the label: anything
        # that relabels this button must take its command with it or the
        # button ends up saying one thing and doing another.
        self._auth_btn.configure(
            text="Reconnect" if valid else "Connect", command=self._on_connect_click
        )
        self._signout_btn.configure(state=self._signout_state())
        if valid and self._transport is None:
            threading.Thread(target=self._silent_build_transport, daemon=True).start()

    def _signout_state(self) -> str:
        """"normal" if there is anything stored to sign out of.

        Not "normal only while the connection is valid", which is what this
        used to be and which had it backwards: forgetting the saved password
        is the *repair* for a broken connection, so greying it out whenever
        auth failed removed the one control that could clear a bad credential.
        It needs no working connection -- deleting a local file needs nothing
        at all.
        """
        return "normal" if IMAP_CREDENTIALS_FILE.exists() else "disabled"

    def _silent_build_transport(self) -> None:
        """Build the transport object in the background after a valid auth-status check.

        "Silent" means no dialog, not "no evidence". This used
        to end in a bare ``except Exception: pass``, and on 2026-08-12 that
        produced the worst possible outcome on a real install: the header said
        "Connected (<address>)" in green while Sync answered "Not connected",
        with nothing anywhere to explain the contradiction. The green came from
        check_auth_status(), which for IMAP went no further than "the file is
        there and parses", while Sync gates on self._transport -- so the moment
        this build failed the two disagreed, and resolve_imap_password's
        careful, actionable RuntimeError was caught and destroyed on the way
        past.

        Both ends have since been closed: check_imap_auth_status() now
        resolves the password rather than observing the file, so the header
        starts honest instead of being corrected a second later. This handler
        stays regardless -- it is what covers every *other* reason a transport
        build can fail, and the point of the fix is that a failure here is
        never silent again.

        So: still no dialog (this runs unprompted at startup, and a modal on
        launch over something the user may not be about to do would be worse
        than the log line), but the reason lands in the log pane and the header
        is corrected to match what Sync will actually do. Widget work is
        marshalled back to the Tk thread with after() -- this is a daemon
        thread and Tk is not thread-safe.
        """
        try:
            if IMAP_CREDENTIALS_FILE.exists():
                data = json.loads(IMAP_CREDENTIALS_FILE.read_text())
                password = resolve_imap_password(data)
                self._transport = build_imap_transport(
                    data["host"], data["port"], data["email"], password
                )
        except Exception as exc:
            # str(exc) is safe to show: mail_client scrubs the app password out
            # of any login/connection error before it propagates, and
            # secret_store never puts the blob or the plaintext into a message.
            msg = _scrub_paths(str(exc)) or exc.__class__.__name__
            self.after(0, lambda: self._report_transport_failure(msg))

    def _report_transport_failure(self, msg: str) -> None:
        """Show why the saved connection could not be reopened. Tk thread only."""
        self._append_log(f"Could not reopen the saved connection: {msg}")
        self._auth_dot.configure(text_color=gui_theme.STATUS_COLOR_ON_BAND["failed"])
        self._auth_label.configure(text="Not connected — reconnect")
        self._auth_btn.configure(text="Connect", command=self._on_connect_click)
        self._signout_btn.configure(state=self._signout_state())

    def _on_connect_click(self) -> None:
        """Where "Connect"/"Reconnect" goes. Never a browser flow.

        IMAP connect is credential entry, so where it lands depends on whether
        there is an account yet: a first setup goes through the wizard, which
        walks through getting an app password, while "Reconnect" on an account
        that already exists goes to Settings, since changing one field is
        faster on the form than four steps. Android's Home button branches the
        same way.
        """
        usable, _status = check_imap_auth_status()
        if usable:
            self._open_settings()
        else:
            self._push_panel(_MailWizardPanel)

    def _on_delete_chat(self, chat_id: str, display_name: str, synced: bool) -> None:
        """Remove a chat entry from the DB. Confirms first if it was ever synced."""
        if synced:
            # Same words as the chat detail panel's in-window gate and as
            # Android's dialog: this glyph is a shortcut to that action, not a
            # differently-worded second version of it.
            ok = messagebox.askyesno(
                "Delete this chat?",
                f"This removes '{display_name}' from your list entirely — unlike "
                "Reset, it won't be kept for re-syncing. Mail already in your "
                "mailbox is not deleted.\n\n"
                "It also forgets which messages were already archived, so if you "
                "ever import this export again you will get a second copy of all "
                "of them unless you delete the old mail first.",
                icon="warning",
            )
            if not ok:
                return
        self._apply_chat_delete(chat_id, display_name)

    def _apply_chat_delete(self, chat_id: str, display_name: str) -> None:
        """Drop the local record, once something has established consent.

        Split out for the same reason as _apply_chat_reset: the chat detail
        panel asks in the window rather than in a dialog and must not carry a
        second copy of what removal means.
        """
        try:
            delete_chat(chat_id, STATE_DB_PATH)
            self._append_log(f"Removed '{display_name}' from chat list.")
        except Exception as exc:
            self._append_log(f"Could not remove '{display_name}': {exc}")
        self._refresh_chat_list()

    def _on_resync_chat(self, chat_id: str, display_name: str, source_filename: str) -> None:
        """Reset sync history for a chat and move its export file back to inbox.

        Gated, because this is the one action in the app that can duplicate
        mail. The old dialog said "emails already in your mailbox are not
        affected", which was true and badly misleading: they are not affected,
        which is exactly why re-syncing files a second copy of every one of
        them. The user has to clear the mailbox side by hand first - nothing
        here can do it for them, since the app never deletes mail.
        """
        archived = count_archived_messages(chat_id, STATE_DB_PATH)
        folder = mailbox_folder_for(display_name)

        noun = "message" if archived == 1 else "messages"

        if archived == 0:
            # Nothing has ever been sent for this chat, so there is nothing to
            # duplicate and no reason to make the user go and check.
            ok = messagebox.askyesno(
                "Reset this chat?",
                f"Reset sync history for '{display_name}'?\n\n"
                "Nothing has been archived for this chat yet, so no duplicate "
                "mail can result.\n\n"
                "A new mail thread is created the next time this chat is synced.",
                icon="warning",
                default=messagebox.NO,
            )
            if not ok:
                return
        else:
            # Gate 1 - the instruction. State the number and the exact folder.
            # Steps come from src.config so this and cli.py cannot drift into
            # giving different instructions for the same destructive action.
            steps = mailbox_clear_steps(folder, is_gmail_mailbox(self._settings))
            numbered = "\n".join(f"  {i}. {s}" for i, s in enumerate(steps, 1))
            ready = messagebox.askyesno(
                "Delete the old mail first",
                f"'{display_name}' already has {archived} {noun} archived in "
                f"your mailbox, in:\n\n    {folder}\n\n"
                "Resetting makes the app forget it sent them, so the next sync "
                "files a second copy. This app can never delete mail - only "
                "you can.\n\n"
                f"{numbered}\n\n"
                "Have you already deleted that mail?",
                icon="warning",
                default=messagebox.NO,
            )
            if not ready:
                self._append_log(
                    f"Reset cancelled for '{display_name}' - clear '{folder}' in your "
                    "mail client first, then reset."
                )
                return

            # Gate 2 - the commitment. Note it does NOT claim an immediate
            # re-archive: reset only clears local state and moves the export
            # back to the inbox, and it reaches this point only because the
            # user has just said the mailbox side is clear. Asserting duplicates
            # outright would contradict that answer; the risk belongs in the
            # conditional, where it is actually true.
            confirmed = messagebox.askyesno(
                "Confirm reset",
                f"You've said this folder is now empty:\n\n    {folder}\n\n"
                "Resetting clears the app's record of this chat. No mail is sent "
                f"now - the next sync re-archives all {archived} {noun} into a "
                "fresh thread.\n\n"
                "If any of the old mail is still there, that sync gives you a "
                "second copy of it, and only you can clean it up.",
                icon="warning",
                default=messagebox.NO,
            )
            if not confirmed:
                return

        self._apply_chat_reset(chat_id, display_name, source_filename)

    def _apply_chat_reset(
        self, chat_id: str, display_name: str, source_filename: str
    ) -> bool:
        """Do the reset itself, once something has established consent.

        Split out of _on_resync_chat because the chat detail panel asks the
        same question in the window rather than in a dialog (see
        _ChatDetailPanel) and must not carry a second copy of what "reset"
        actually does -- clearing local state and putting the export back
        where the next sync will find it. Returns whether it went through.
        """
        try:
            # confirmed_mailbox_cleared is set only on the path where the user
            # answered both prompts; the archived == 0 path passes it because
            # there is provably nothing in the mailbox to clear.
            reset_chat(chat_id, STATE_DB_PATH, confirmed_mailbox_cleared=True)
        except MailboxNotClearedError as exc:
            self._append_log(f"Reset refused for '{display_name}': {exc}")
            return False
        except Exception as exc:
            self._append_log(f"Could not reset '{display_name}': {exc}")
            return False

        # Move the export file back from processed/ to inbox/ if found.
        src = PROCESSED_DIR / source_filename
        if src.exists():
            dest = INBOX_DIR / source_filename
            if dest.exists():
                self._append_log(f"Reset '{display_name}'. Export file is already in inbox.")
            else:
                try:
                    shutil.move(str(src), str(dest))
                    self._append_log(f"Reset '{display_name}'. Export file moved back to inbox.")
                except Exception as exc:
                    self._append_log(f"Reset '{display_name}' (DB cleared) but could not move file: {exc}")
        else:
            self._append_log(
                f"Reset '{display_name}'. Drop the export file in inbox to re-sync."
            )
        self._refresh_chat_list()
        self._refresh_inbox_count()
        return True

    def _on_stop_click(self) -> None:
        if self._worker is None:
            return
        self._stop_btn.configure(state="disabled", text="Stopping…")
        self._progress_label.configure(text="Stopping after current file…")
        self._worker.stop()

    def _on_signout_click(self) -> None:
        self._on_forget_imap_password_click()

    def _on_forget_imap_password_click(self) -> None:
        """Delete the saved IMAP app password locally. No network call.

        This does NOT revoke the app password at the provider -- an app
        password is a standalone credential that only the provider's own
        account-security page can revoke. The confirm dialog says so
        explicitly and points at where to do it, mirroring the existing
        destructive-action dialog pattern used by _on_delete_chat /
        _on_resync_chat (messagebox.askyesno with icon="warning").
        """
        ok = messagebox.askyesno(
            "Forget saved password?",
            "This removes the saved app password from this computer only.\n\n"
            "It does NOT revoke or delete the app password at your email "
            "provider — for Gmail, remove it under Google Account > Security > "
            "App passwords; for Outlook/Microsoft, under Security > Advanced "
            "security options. You'll need to generate a new one (or re-enter "
            "this one) to connect again.",
            icon="warning",
        )
        if not ok:
            return
        try:
            if IMAP_CREDENTIALS_FILE.exists():
                IMAP_CREDENTIALS_FILE.unlink()
        except Exception as exc:
            self._append_log(f"Could not forget saved password: {exc}")
            return

        self._transport = None
        # The verdict belonged to the password just deleted.
        self._forget_connection()
        self._auth_dot.configure(text_color=gui_theme.STATUS_COLOR_ON_BAND["failed"])
        self._auth_label.configure(text="Not connected")
        # See _apply_auth_status: relabelling must restore the command too.
        self._auth_btn.configure(
            state="normal", text="Connect", command=self._on_connect_click
        )
        self._signout_btn.configure(state="disabled")
        self._append_log("Forgot saved app password. Connect again to reconnect.")

    def _open_help(self) -> None:
        """Open help.html in the default browser; fall back to a brief dialog."""
        path = _help_html_path()
        if path is not None:
            webbrowser.open(path.as_uri())
            return
        messagebox.showinfo(
            "Help",
            "Quick start:\n\n"
            "1. Click Connect (top-right) and set up your mail account.\n"
            "2. Drag a WhatsApp .txt or .zip export onto the window.\n"
            "3. Click Sync Now.\n\n"
            "Your synced chats appear in your mailbox under the WhatsApp label.\n\n"
            "(The full help file, help.html, was not found next to the app.)"
        )

    def _open_settings(self) -> None:
        """Show settings in this window rather than as a pop-up."""
        if self._panels:
            self._panels[-1].focus_set()
            return
        self._push_panel(_SettingsPanel)

    def _open_mail_account(self) -> None:
        """Show the mail account screen -- what the masthead status opens.

        Reachable from Settings too, and from the Connect button beside it;
        this is the third door onto the same screen because the status line is
        where somebody looks when they want to know, or change, which account
        this is.
        """
        if self._panels:
            self._panels[-1].focus_set()
            return
        self._push_panel(_MailAccountPanel)

    def _open_import_picker(self) -> None:
        """Show the watched folder's exports in this window, not as a pop-up."""
        if self._panels:
            self._panels[-1].focus_set()
            return
        self._push_panel(_ImportPickerPanel)

    def _open_sync_log(self) -> None:
        """Show the 90-day run history in this window, not as a pop-up."""
        if self._panels:
            self._panels[-1].focus_set()
            return
        self._push_panel(_SyncLogPanel)

    def _push_panel(self, factory) -> None:
        """Put an in-window screen over the sync view.

        Placed rather than packed, covering everything below the header: the
        sync view and the footer keep their pack order untouched underneath,
        so going back is a destroy() and nothing else has to be rebuilt or
        re-ordered. The header stays put, as Android's top bar does.
        """
        # The geometry lives on a bare tk.Frame holder rather than on the panel
        # itself: customtkinter refuses width/height in place() (it wants them
        # on the constructor), and without the negative height a relheight of
        # 1.0 would push the panel's bottom -- where Save and Cancel sit -- off
        # the window by exactly the header's height.
        holder = tkinter.Frame(self, bd=0, highlightthickness=0)
        holder.place(
            x=0, y=self._HEADER_HEIGHT,
            relwidth=1.0, relheight=1.0, height=-self._HEADER_HEIGHT,
        )
        panel = factory(self, holder)
        panel.pack(fill="both", expand=True)
        holder.lift()
        self._panels.append(panel)

    def _pop_panel(self) -> None:
        """Close the innermost screen and hand control back to what it covered."""
        if not self._panels:
            return
        # Destroying the holder takes the panel with it -- see _push_panel.
        self._panels.pop().master.destroy()
        if self._panels:
            revealed = self._panels[-1]
            revealed.master.lift()
            # Settings shows a one-line account summary that the mail account
            # screen may just have changed.
            if hasattr(revealed, "on_reveal"):
                revealed.on_reveal()

    def _apply_settings(self, new_settings: dict) -> None:
        """Called by the settings screen on Save."""
        old_refresh_ms = self._auto_refresh_ms
        old_backend = self._settings.get("mail_backend")
        self._settings = new_settings
        self._chunk_var.set(new_settings.get("chunk_size", "day"))
        self._auto_refresh_ms = _AUTO_REFRESH_OPTIONS.get(
            new_settings.get("auto_refresh_label", "30 s"), 30_000
        )
        _save_settings(new_settings)

        # Restart the auto-refresh timer if the interval changed.
        if self._auto_refresh_ms != old_refresh_ms and self._auto_refresh_ms > 0:
            self.after(self._auto_refresh_ms, self._auto_refresh_inbox)

        # Unconditional: the folder, the interval or the on/off switch may all
        # have changed, and _schedule_watch_timer cancels before re-arming, so
        # calling it when nothing changed is harmless.
        self._update_watch_ui()
        self._schedule_watch_timer()

        self._update_signout_button_label()
        if new_settings.get("mail_backend") != old_backend:
            # Switching backends invalidates whatever transport we had cached.
            self._transport = None
            # ...and the connection verdict with it: it was about the other
            # backend's credentials entirely. _check_auth() below repaints the
            # header from the cleared value, so the switch lands on amber
            # "Not tested" rather than keeping the old backend's green.
            self._forget_connection()
            self._check_auth()

    def _update_signout_button_label(self) -> None:
        """Wide enough for "Forget saved password" so the text isn't clipped.

        Kept as a method rather than set once at build time because the label
        is a property of the active backend, and the button is rebuilt from
        several places that must not each re-derive its width.
        """
        self._signout_btn.configure(text="Forget saved password", width=170)

    def _maybe_show_oauth_removed_notice(self, was_oauth_user: bool) -> None:
        """Explain, once, that Google sign-in is gone -- to those who had it.

        Being moved onto a different mail backend without being told is the
        failure this exists to prevent: the alternative is an existing user
        opening the app to a "Not connected" header and a form demanding an
        app password they have never created, with nothing anywhere saying
        why. Informational only -- showinfo, not a question. Dismissing it
        changes nothing; the backend was already resolved to IMAP on load
        (config.resolve_mail_backend), because there is nothing else to
        resolve it to.

        Nobody else sees it. A fresh install has never had Google sign-in, and
        telling them it was removed only advertises a path they cannot take.
        """
        if not _should_show_oauth_removed_notice(self._settings, was_oauth_user):
            return
        # This modal is raised from __init__, i.e. before mainloop() and
        # without waiting for <Map>, so the splash may still be up -- and it is
        # topmost, which would park it over a dialog the user has to read and
        # dismiss. That is the worst version of the overlap this whole change
        # exists to remove, so dismiss here too. Whichever call happens second
        # finds no splash window and returns harmlessly.
        dismiss_launcher_splash()
        messagebox.showinfo(
            "Google sign-in has been removed",
            "This version connects to your mailbox with an email app password "
            "over IMAP, and Google sign-in is no longer offered.\n\n"
            "The sign-in never completed Google's app verification, so it "
            "stayed in Google's \"Testing\" mode: it expired about every 7 "
            "days and only pre-listed accounts could use it at all. An app "
            "password has neither limit and works with Gmail, Outlook, Yahoo, "
            "iCloud, Fastmail or any IMAP server.\n\n"
            "Nothing already archived is affected, and none of your sync "
            "history was touched. To carry on, open Settings (gear icon, "
            "top-right) -> Mail account and set up an app password -- the "
            "screen walks you through getting one.",
        )
        self._settings["oauth_removed_notice_shown"] = True
        _save_settings(self._settings)

    def _on_toggle_theme(self) -> None:
        new_mode = "light" if self._theme_mode == "dark" else "dark"
        self._theme_mode = new_mode
        try:
            _THEME_FILE.write_text(new_mode)
        except Exception:
            pass
        ctk.set_appearance_mode(new_mode)
        icon = "☽" if new_mode == "light" else "☀"
        self._theme_btn.configure(text=icon)
        self._theme_tip.set_text(
            "Switch to dark theme" if new_mode == "light" else "Switch to light theme"
        )
        # Rebuild dynamic sections so chat rows pick up the new theme colors.
        self._refresh_chat_list()
        self._refresh_inbox_count()

    # ------------------------------------------------------------------
    # Log helpers
    # ------------------------------------------------------------------

    def _append_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_lines.append(f"[{ts}]  {msg}")
        if len(self._log_lines) > _LOG_MAX_LINES:
            self._log_lines = self._log_lines[-_LOG_MAX_LINES:]
        self._update_log_box()

    def _update_log_box(self) -> None:
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.insert("end", "\n".join(self._log_lines))
        self._log_box.see("end")
        self._log_box.configure(state="disabled")


# ---------------------------------------------------------------------------
# Settings modal
# ---------------------------------------------------------------------------

# One entry, because there is one backend. Kept as a mapping rather than
# collapsed to a bare string so that adding a second backend is a data change
# here and in android/.../MailAccountScreen.kt's BACKEND_LABELS, not a
# structural one across two front-ends -- see PLATFORM-PARITY.md, do not edit
# one side without the other.
#
# The label names the SCOPE of the choice, not its mechanism: what a user
# needs to know is that any provider works, not that the wire protocol is
# IMAP.
_BACKEND_LABELS = {
    MAIL_BACKEND_IMAP: "Any provider (IMAP app password)",
}

_PROVIDER_LABELS = {key: info["label"] for key, info in IMAP_PROVIDERS.items()}
_PROVIDER_LABELS_REV = {v: k for k, v in _PROVIDER_LABELS.items()}

# ---------------------------------------------------------------------------
# App-password help content -- ported from android/.../MailAccountScreen.kt
# (APP_PASSWORD_HELP_URLS / APP_PASSWORD_HELP_TEXT / APP_PASSWORD_STEPS_* /
# buildAppPasswordPrompt). Kept string-for-string identical to the Android
# copy so the two apps describe the same steps in the same words -- see
# PLATFORM-PARITY.md. Do not edit one side without the other.
# ---------------------------------------------------------------------------

# Official, human-verified "create an app password" pages, one per
# IMAP_PROVIDERS key. Verified by fetching each URL and confirming it is the
# provider's own current app-password help page -- do not swap in an
# unverified link, these go stale often as providers redesign support sites.
# "custom" has no entry: there's no provider to link to, so the UI falls back
# to generic guidance instead.
APP_PASSWORD_HELP_URLS = {
    "gmail": "https://support.google.com/accounts/answer/185833",
    "outlook": "https://support.microsoft.com/en-us/account-billing/using-app-passwords-with-apps-that-don-t-support-two-step-verification-5896ed9b-4263-e681-128a-a6f2979a7944",
    "yahoo": "https://help.yahoo.com/kb/SLN15241.html",
    "icloud": "https://support.apple.com/en-us/102654",
    "fastmail": "https://www.fastmail.help/hc/en-us/articles/360058752854-App-passwords",
}

APP_PASSWORD_HELP_TEXT = {
    "gmail": "Gmail app passwords are generated from your Google Account's security settings (requires 2-Step Verification to be on).",
    # Personal Microsoft accounts only. Work and school (Microsoft 365) mailboxes
    # have basic authentication switched off, so an app password is refused there
    # whatever host is entered -- see src/config.py's IMAP_PROVIDERS note.
    "outlook": "Outlook.com app passwords are generated from your personal Microsoft account's security settings (requires two-step verification to be on). Work or school Microsoft 365 accounts can't use an app password at all.",
    "yahoo": "Yahoo app passwords are generated from your Yahoo Account security page.",
    "icloud": "iCloud app-specific passwords are generated at appleid.apple.com, under Sign-In and Security.",
    "fastmail": "Fastmail app passwords are generated from Settings > Password & Security in your Fastmail account.",
}

# Bump this string (to the month/year you actually re-checked the steps
# below) any time APP_PASSWORD_STEPS_GMAIL or APP_PASSWORD_STEPS_OUTLOOK is
# edited. It's rendered next to the steps so a user whose provider has since
# changed its menus knows to trust the "Search for steps" / help-page button
# over this in-app text rather than assume the app is simply wrong.
APP_PASSWORD_STEPS_REVIEWED = "August 2026"

# Derived from support.google.com/accounts/answer/185833. That page does not
# itself enumerate numbered steps; it states the 2-Step Verification
# prerequisite and links myaccount.google.com/apppasswords as the place app
# passwords are created and managed. The steps below are written from those
# confirmed facts only -- nothing here is invented UI copy that wasn't on
# the page.
APP_PASSWORD_STEPS_GMAIL = [
    "Turn on 2-Step Verification for your Google Account first — the app password option stays hidden until it's on.",
    "Go to myaccount.google.com/apppasswords (in a browser) and sign in.",
    "Create a new app password there — Google gives you a 16-character code.",
    "Paste that 16-character code into the \"App password\" field below (not your normal Google password).",
]

# Derived from support.microsoft.com's "Using app passwords with apps that
# don't support two-step verification" page, which describes: two-step
# verification must be on; go to Advanced security options; scroll to the
# App passwords section; select the option to create one; use it wherever
# the app would normally ask for your Microsoft account password.
APP_PASSWORD_STEPS_OUTLOOK = [
    "Turn on two-step verification for your Microsoft account first — app passwords are only offered once it's on.",
    "Go to your Microsoft account's Advanced security options (account.microsoft.com) and sign in.",
    "Scroll to the \"App passwords\" section and choose to create one.",
    "Paste the generated app password into the \"App password\" field below (not your normal Microsoft password).",
    "If this is a work or school (Microsoft 365) account, see the note below — IMAP may be disabled by the admin regardless.",
]


def _build_app_password_prompt(provider_key: str, provider_label: str, host: str) -> str:
    """Builds the provider-specific question a user can copy into an AI
    assistant or paste into a web search to get current, provider-specific
    app-password steps. Deliberately takes only provider_key/provider_label/
    host -- the email address and app password must NEVER be interpolated
    into this string. It gets copied to the clipboard and/or opened in a
    browser search, both of which are effectively public once triggered, so
    leaking either credential here would be a real exposure, not a cosmetic
    one. If you're editing this function to add more context, keep that
    boundary -- provider name and host only. Mirrors Android's
    buildAppPasswordPrompt() in MailAccountScreen.kt; keep both in sync.
    """
    year = datetime.now().year
    if provider_key == "custom":
        provider_phrase = f"my email provider at {host}" if host.strip() else "my email provider"
    else:
        provider_phrase = provider_label
    return (
        f"How do I create an app password for {provider_phrase} in {year} to use with a third-party IMAP "
        "email app? Tell me whether I need to turn on two-factor authentication first, the exact page or "
        "menu path where I generate the app password, and the IMAP server name and port to use. Give me "
        "the current steps and link the official help page."
    )


class _Panel(ctk.CTkFrame):
    """An in-window screen: a titled bar with a way back, then the content.

    These were separate Toplevels until the stack of pop-ups they produced --
    settings over the main window, mail account over settings -- became the
    complaint. Android never had them: SettingsScreen and MailAccountScreen are
    pushed onto a nav stack with a back arrow in the top bar, and this is the
    same arrangement. The App owns the stack (_push_panel/_pop_panel); a panel
    only knows how to close itself.
    """

    def __init__(self, app: "App", master, title: str, back_to: str) -> None:
        # Two parents, deliberately: `master` is the placed holder this panel
        # fills, `app` is who it talks to (settings, the panel stack). They are
        # different objects -- see App._push_panel.
        super().__init__(master, corner_radius=0)
        self._app = app

        # The same band as the main window's masthead, one notch shorter and
        # without the mark: a pushed screen replaces the whole window below the
        # title bar, so if this bar were a plain grey strip the app would appear
        # to change identity every time you opened Settings. Android's pushed
        # screens keep ChatMailTopBar for exactly that reason -- they just swap
        # the badge for the labelled back.
        bar = ctk.CTkFrame(self, height=48, corner_radius=0, fg_color=gui_theme.PRIMARY)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        # A bare arrow was reported as neither intuitive nor obvious: on Android
        # the arrow is read in the context of a system-wide back gesture that
        # the desktop has no equivalent of. So the button says where it lands
        # -- "Back to sync", "Back to settings" -- and looks like a button
        # rather than a glyph. Escape does the same thing (see App.__init__).
        ctk.CTkButton(
            bar, text=f"←  {back_to}", height=30,
            font=ctk.CTkFont(size=13),
            fg_color="transparent", border_width=1,
            border_color=gui_theme.ON_PRIMARY,
            hover_color=gui_theme.BAND_GHOST_HOVER,
            text_color=gui_theme.ON_PRIMARY,
            command=self._close,
        ).pack(side="left", padx=(14, 12))
        ctk.CTkLabel(
            bar, text=title, anchor="w",
            font=ctk.CTkFont(family=gui_theme.SERIF_FAMILY, size=16, weight="bold"),
            text_color=gui_theme.ON_PRIMARY,
        ).pack(side="left")

    def _close(self) -> None:
        self._app._pop_panel()

    # ── Shared detail-page furniture ───────────────────────────────────
    # On _Panel rather than on one panel, because the run detail screen and
    # the chat detail screen are the same kind of page -- a labelled fact per
    # line under a ruled heading -- and two copies of that is how two screens
    # end up with different label widths on the same window.

    def _section_rule(self, parent, title: str) -> None:
        ctk.CTkLabel(
            parent, text=title, anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(fill="x", pady=(10, 2))
        ctk.CTkFrame(parent, height=1, fg_color=gui_theme.OUTLINE_VARIANT).pack(
            fill="x", pady=(0, 6)
        )

    def _field(self, parent, label: str, value: str, *, value_color=None) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=1)
        ctk.CTkLabel(
            row, text=label, anchor="w", width=190,
            font=ctk.CTkFont(size=12), text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(side="left")
        ctk.CTkLabel(
            row, text=value, anchor="w", font=ctk.CTkFont(size=12),
            text_color=value_color or gui_theme.ON_SURFACE,
        ).pack(side="left", fill="x", expand=True)


class _SettingsPanel(_Panel):
    """Settings — chunk size, auto-refresh interval, watched folder, and a way
    in to the mail account.

    Laid out in the same compartments as Android's SettingsScreen: a titled
    section per topic, separated by a rule, inside one scrolling body, with the
    mail account on its own screen (_MailAccountPanel, mirroring Android's
    MailAccountScreen). Android scrolls its settings column too
    (`verticalScroll(rememberScrollState())`), and this does the same, so the
    content never has to fit the space it is given.
    """

    def __init__(self, app: "App", master) -> None:
        super().__init__(app, master, "Settings", "Back to sync")

        pad = {"padx": 20, "pady": 8}
        settings = app._settings

        # ── Buttons ──────────────────────────────────────────────────
        # Packed against the bottom and outside the scrolling body: that is
        # what makes "Save is off-screen" structurally impossible rather than
        # something the layout has to keep measuring for.
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=20, pady=(8, 12))

        self._save_btn = ctk.CTkButton(
            btn_row, text="Save", width=100, height=32,
            command=self._on_save,
        )
        self._save_btn.pack(side="right", padx=(6, 0))

        ctk.CTkButton(
            btn_row, text="Cancel", width=80, height=32,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._close,
        ).pack(side="right")

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)

        # ── Mail account ─────────────────────────────────────────────
        # First, and a way in rather than the thing itself, exactly as on
        # Android: the account form is the longest block here and the one
        # revisited least once it works, so making everyone scroll past it to
        # reach the watched folder was the wrong trade. What stays behind is a
        # one-line status, so "am I connected, and as whom?" is still answered
        # without opening anything.
        self._section(body, "Mail account", first=True)
        acc_row = ctk.CTkFrame(body, fg_color="transparent")
        acc_row.pack(fill="x", padx=20, pady=(6, 0))
        self._account_summary = ctk.CTkLabel(
            acc_row, text="", anchor="w", font=("", 11),
            text_color=gui_theme.ON_SURFACE_VARIANT,
        )
        # Left-aligned and adjacent, not expand-then-pin-right. As a pop-up this
        # row was only ever as wide as the dialog, so a right-pinned button sat
        # close to its label; in the main window the same code threw it to the
        # far edge, a whole screen away from the status it acts on -- and out of
        # line with every other control here, which starts at the left margin.
        # The fixed label width keeps it from wandering as the summary text
        # changes length between "Not connected" and a full address.
        self._account_summary.configure(width=320)
        self._account_summary.pack(side="left")
        ctk.CTkButton(
            acc_row, text="Change…", width=90, height=30,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._open_mail_account,
        ).pack(side="left", padx=(12, 0))
        self._render_account_summary()

        # ── Syncing ──────────────────────────────────────────────────
        self._section(body, "Syncing")

        row1 = ctk.CTkFrame(body, fg_color="transparent")
        row1.pack(fill="x", **pad)
        ctk.CTkLabel(row1, text="Chunk size:", width=130, anchor="w").pack(side="left")
        self._chunk_var = ctk.StringVar(value=settings.get("chunk_size", "day"))
        ctk.CTkOptionMenu(
            row1, values=["day", "hour", "week"],
            variable=self._chunk_var, width=120, height=30,
        ).pack(side="left")

        # ── Auto-refresh interval ────────────────────────────────────
        row2 = ctk.CTkFrame(body, fg_color="transparent")
        row2.pack(fill="x", **pad)
        ctk.CTkLabel(row2, text="Auto-refresh:", width=130, anchor="w").pack(side="left")
        self._refresh_var = ctk.StringVar(value=settings.get("auto_refresh_label", "30 s"))
        ctk.CTkOptionMenu(
            row2, values=list(_AUTO_REFRESH_OPTIONS.keys()),
            variable=self._refresh_var, width=120, height=30,
        ).pack(side="left")

        # ── Watched folder ───────────────────────────────────────────
        # Off by default and opt-in, exactly as on Android: polling a folder
        # in the background is the user's call to make, not ours.
        self._section(body, "Watched folder")

        wrow = ctk.CTkFrame(body, fg_color="transparent")
        wrow.pack(fill="x", **pad)
        ctk.CTkLabel(wrow, text="Watched folder:", width=130, anchor="w").pack(side="left")
        self._watch_path_label = ctk.CTkLabel(
            wrow, text="", anchor="w", font=("", 11),
            text_color=gui_theme.ON_SURFACE_VARIANT, width=160,
        )
        self._watch_path_label.pack(side="left")
        ctk.CTkButton(
            wrow, text="Choose…", width=76, height=30,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._on_choose_watch_folder,
        ).pack(side="left", padx=(4, 0))
        ctk.CTkButton(
            wrow, text="Clear", width=54, height=30,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._on_clear_watch_folder,
        ).pack(side="left", padx=(4, 0))

        self._watched_path = str(settings.get("watched_folder_path", "") or "")
        # Set when the folder is changed or cleared: the "already imported"
        # ledger describes the old folder and would silently suppress files in
        # the new one. The pending-delivery ledger is *not* reset with it --
        # those entries hold absolute source paths and still deserve their
        # synced-file rule wherever they came from.
        self._reset_watch_ledgers = False
        self._render_watch_path()

        wrow2 = ctk.CTkFrame(body, fg_color="transparent")
        wrow2.pack(fill="x", **pad)
        self._auto_watch_var = ctk.BooleanVar(value=bool(settings.get("auto_watch_enabled", False)))
        ctk.CTkCheckBox(
            wrow2, text="Check it automatically", height=28,
            variable=self._auto_watch_var,
        ).pack(side="left")
        current_minutes = int(
            settings.get("watch_interval_minutes", DEFAULT_WATCH_INTERVAL_MINUTES)
        )
        self._watch_interval_var = ctk.StringVar(
            value=next(
                (k for k, v in _WATCH_INTERVAL_OPTIONS.items() if v == current_minutes),
                "Every 15 min",
            )
        )
        ctk.CTkOptionMenu(
            wrow2, values=list(_WATCH_INTERVAL_OPTIONS.keys()),
            variable=self._watch_interval_var, width=130, height=30,
        ).pack(side="left", padx=(8, 0))

        # The Windows half of Batch E. Android warns about the two system
        # settings that can stop its background worker; Windows has neither, but
        # it has the same failure -- an automatic check that quietly never runs
        # -- for its own reason: the timer is a Tk after() call inside this
        # window, so there is nothing to run once the window is closed. The
        # constraint is stated next to the switch it constrains rather than only
        # in the help page, which is where it was.
        ctk.CTkLabel(
            body,
            text=(
                "Checks run only while this window is open. There is no "
                "background service — close the app and nothing is checked "
                "until you open it again (minimised is fine)."
            ),
            wraplength=380, justify="left", anchor="w",
            text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        ).pack(fill="x", padx=20, pady=(0, 4))

        wrow3 = ctk.CTkFrame(body, fg_color="transparent")
        wrow3.pack(fill="x", **pad)
        ctk.CTkLabel(wrow3, text="After syncing:", width=130, anchor="w").pack(side="left")
        current_policy = str(settings.get("synced_file_policy", "leave"))
        self._synced_policy_var = ctk.StringVar(
            value=_SYNCED_FILE_POLICY_LABELS.get(
                current_policy, _SYNCED_FILE_POLICY_LABELS["leave"]
            )
        )
        ctk.CTkOptionMenu(
            wrow3, values=list(_SYNCED_FILE_POLICY_LABELS.values()),
            variable=self._synced_policy_var, width=250, height=30,
        ).pack(side="left")

        ctk.CTkLabel(
            body,
            text=(
                "Only applies to files that came from the watched folder, and "
                "only once they have actually reached your mailbox."
            ),
            wraplength=380, justify="left", anchor="w",
            text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        ).pack(fill="x", padx=20, pady=(0, 4))

        # ── Backup & restore ──────────────────────────────────────────
        # Android's twin, section for section and sentence for sentence. Worth
        # being explicit about what it is for, because "backup" in an archiving
        # app invites the wrong reading: the mailbox is the archive and is
        # already safe on a mail server. What is only here is the record of what
        # has already been sent -- lose that and nothing is lost, everything is
        # simply mailed a second time, into a mailbox with no way to tell the
        # copies apart.
        # Headed "Move to a new phone or PC" until v1.17.0, which hid it
        # from everyone who was not moving: the same file is what gets you
        # back after a reinstall or a wiped machine.
        self._section(body, "Backup & restore")
        ctk.CTkLabel(
            body,
            text=(
                "Saves what this app knows about what it has already sent. Keep "
                "one, and a reinstall or another device carries on from here "
                "instead of mailing everything a second time. Your chats are "
                "already safe in your mailbox \u2014 this is not a copy "
                "of them."
            ),
            wraplength=380, justify="left", anchor="w",
            text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        ).pack(fill="x", padx=20, pady=(4, 2))

        mrow = ctk.CTkFrame(body, fg_color="transparent")
        mrow.pack(fill="x", **pad)
        self._backup_save_btn = ctk.CTkButton(
            mrow, text="Save a backup\u2026", width=130, height=30,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._on_save_backup,
        )
        self._backup_save_btn.pack(side="left")
        self._backup_restore_btn = ctk.CTkButton(
            mrow, text="Restore from a backup\u2026", width=180, height=30,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._on_restore_backup,
        )
        self._backup_restore_btn.pack(side="left", padx=(8, 0))

        # A backup nobody can date is a backup nobody trusts, and "I think I
        # did one" is exactly the belief that costs a mailbox a second copy of
        # everything. Android carries the identical line.
        self._backup_age = ctk.CTkLabel(
            body, text="", wraplength=380, justify="left", anchor="w",
            text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        )
        self._backup_age.pack(fill="x", padx=20, pady=(4, 0))
        self._refresh_backup_age()

        # In place, under the buttons -- not a message box. Everything this can
        # say is an outcome to read and none of it needs a decision, so a box
        # demanding to be dismissed would only add a click.
        self._backup_status = ctk.CTkLabel(
            body, text="", wraplength=380, justify="left", anchor="w",
            text_color=gui_theme.ON_SURFACE, font=("", 11),
        )
        self._backup_status.pack(fill="x", padx=20, pady=(0, 2))
        ctk.CTkLabel(
            body,
            text=(
                "Your mail password is never included in a backup."
            ),
            wraplength=380, justify="left", anchor="w",
            text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        ).pack(fill="x", padx=20, pady=(0, 4))

        # ── About / Help ───────────────────────────────────────────────
        # Named and placed to match Android's last section. The version is
        # here rather than in the main window because that is where Android
        # puts it, and because "which version am I on?" is a question asked
        # once, on purpose.
        self._section(body, "About / Help")
        self._version_label = ctk.CTkLabel(
            body, text=version_label(), font=("", 11),
            text_color=gui_theme.ON_SURFACE_VARIANT, anchor="w",
        )
        self._version_label.pack(fill="x", padx=20, pady=(4, 8))

    # ------------------------------------------------------------------
    # Mail account (its own window -- Android's MailAccountScreen)
    # ------------------------------------------------------------------

    def _render_account_summary(self) -> None:
        """One backend-neutral line: who we are connected as, or that we are
        not. Mirrors the summary Android computes for its "Mail account" nav
        row -- the email when there is a usable credential, "Not connected"
        otherwise.

        "Usable" is asked of gui_worker rather than answered here. This line
        used to settle it with IMAP_CREDENTIALS_FILE.exists(), which is the
        same mistake the main header made until v1.9.1: a file left behind by
        an install whose password this machine can no longer decrypt would
        still print an address, in a screen whose entire job is to tell you
        which account you are on.
        """
        settings = self._app._settings
        email = str(settings.get("imap_email") or "").strip()
        usable, _status = check_imap_auth_status()
        self._account_summary.configure(
            text=email if (email and usable) else "Not connected"
        )

    def _open_mail_account(self) -> None:
        # Pushed over this screen, which stays alive underneath: any settings
        # edits made before coming here are still there on the way back.
        self._app._push_panel(_MailAccountPanel)

    def on_reveal(self) -> None:
        """Called by App._pop_panel when the mail account screen closes over
        this one -- the summary line it shows may have just changed."""
        self._render_account_summary()

    # ------------------------------------------------------------------
    # Section headings
    # ------------------------------------------------------------------

    def _section(self, parent, title: str, first: bool = False) -> None:
        """A titled compartment, one per topic, as on Android -- where the
        same job is done by a `Text(style = titleMedium)` and a
        HorizontalDivider between sections."""
        if not first:
            ctk.CTkFrame(
                parent, height=1, fg_color=gui_theme.OUTLINE_VARIANT,
            ).pack(fill="x", padx=20, pady=(14, 0))
        ctk.CTkLabel(
            parent, text=title, anchor="w", font=("", 13, "bold"),
        ).pack(fill="x", padx=20, pady=(10, 0))

    # ------------------------------------------------------------------
    # Watched folder
    # ------------------------------------------------------------------

    def _render_watch_path(self) -> None:
        """Show the chosen folder, tail-first. A full path does not fit this
        dialog, and the leaf folder is the part that identifies it."""
        if not self._watched_path:
            self._watch_path_label.configure(text="None chosen")
            return
        text = self._watched_path
        if len(text) > 26:
            text = "…" + text[-25:]
        self._watch_path_label.configure(text=text)

    def _on_choose_watch_folder(self) -> None:
        chosen = filedialog.askdirectory(
            title="Choose a folder to watch for WhatsApp exports",
            initialdir=self._watched_path or None,
        )
        if not chosen:
            return
        # A previous folder's ledger says nothing about a new one, and keeping
        # it would only mean stale entries accumulating in the settings file.
        if self._watched_path and Path(chosen) != Path(self._watched_path):
            self._reset_watch_ledgers = True
        self._watched_path = str(Path(chosen))
        self._render_watch_path()

    def _on_clear_watch_folder(self) -> None:
        self._watched_path = ""
        self._auto_watch_var.set(False)
        self._reset_watch_ledgers = True
        self._render_watch_path()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Backup & restore
    # ------------------------------------------------------------------

    def _set_backup_busy(self, busy: bool, message: str) -> None:
        state = "disabled" if busy else "normal"
        self._backup_save_btn.configure(state=state)
        self._backup_restore_btn.configure(state=state)
        self._backup_status.configure(text=message)

    # Thirty days: long enough that someone who keeps up is never nagged,
    # short enough that what a lost record would re-mail is about a month of
    # chats. Android's Migration.BACKUP_STALE_AFTER_DAYS says the same.
    _BACKUP_STALE_AFTER_DAYS = 30

    def _record_backup_saved(self) -> None:
        """Remember when, so the app can say how old the last backup is.

        Deliberately not *where*: the file goes wherever the save dialog put
        it, which may not exist any more, and a stale path claiming to be a
        backup is worse than no path at all.
        """
        self._app._settings["last_backup_at"] = int(datetime.now().timestamp())
        _save_settings(self._app._settings)
        self._refresh_backup_age()
        # The line on Home is about this exact fact; leaving it lying after a
        # save is the app telling the user their work did not count.
        self._app._refresh_backup_staleness()

    def _record_backup_cover(self, at: int) -> None:
        """Same stamp as [_record_backup_saved], dated from a restored bundle.

        Never moved backwards: restoring an old bundle onto a machine that has
        a newer one must not make it look less protected than it is.
        """
        if at <= int(self._app._settings.get("last_backup_at") or 0):
            return
        self._app._settings["last_backup_at"] = at
        _save_settings(self._app._settings)
        self._refresh_backup_age()
        self._app._refresh_backup_staleness()

    def _refresh_backup_age(self) -> None:
        if not hasattr(self, "_backup_age") or not self._backup_age.winfo_exists():
            return
        at = int(self._app._settings.get("last_backup_at") or 0)
        stale = _backup_is_stale(at)
        self._backup_age.configure(
            text=_describe_last_backup(at),
            text_color=gui_theme.ERROR if stale else gui_theme.ON_SURFACE_VARIANT,
        )

    def _run_backup_job(self, work) -> None:
        """Run [work] off the UI thread and report its sentence in place.

        Off the thread because it opens SQLite databases and walks every row of
        the ledger; winfo_exists because the panel can be closed while it runs,
        and Tk raises on any widget touched after that.
        """
        def _done(message: str) -> None:
            if self.winfo_exists():
                self._set_backup_busy(False, message)

        def _thread() -> None:
            try:
                message = work()
            except Exception as exc:  # noqa: BLE001 - shown, not swallowed
                message = f"That did not work: {_scrub_paths(str(exc))}"
            self.after(0, lambda: _done(message))

        threading.Thread(target=_thread, daemon=True).start()

    def _on_save_backup(self) -> None:
        dest = filedialog.asksaveasfilename(
            title="Save a Chat Mail Sync backup",
            defaultextension=migration.BUNDLE_SUFFIX,
            filetypes=[("Chat Mail Sync backup", "*" + migration.BUNDLE_SUFFIX),
                       ("All files", "*.*")],
            initialfile="chat-mail-sync-"
                        + datetime.now().strftime("%Y-%m-%d-%H%M")
                        + migration.BUNDLE_SUFFIX,
        )
        if not dest:
            return  # user cancelled
        self._set_backup_busy(True, "Saving\u2026")

        # The panel's unsaved edits are deliberately not used: what gets backed
        # up is what this install is actually running on, not what someone has
        # half-typed into the form above and may yet cancel.
        settings = dict(self._app._settings)

        def work() -> str:
            summary = migration.export_bundle(
                PROJECT_ROOT, Path(dest), settings, app_version()
            )
            if not summary.get("ok"):
                return str(summary.get("error") or "The backup could not be saved.")
            counts = summary.get("counts") or {}
            chats = int(counts.get("chats") or 0)
            hashes = int(counts.get("hashes") or 0)
            return (
                f"Backup saved \u2014 {chats} chat{'' if chats == 1 else 's'}, "
                f"{hashes} message{'' if hashes == 1 else 's'} already sent. "
                "Your mail password is not in it; the restored device asks once."
            )

        def work_and_stamp() -> str:
            message = work()
            # Only on the way out of a save that reported ok -- a stamp written
            # when the button was clicked would tell someone they are covered
            # when the file never got written.
            if message.startswith("Backup saved"):
                self.after(0, self._record_backup_saved)
            return message

        self._run_backup_job(work_and_stamp)

    def _on_restore_backup(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Restore from a Chat Mail Sync backup",
            filetypes=[("Chat Mail Sync backup", "*" + migration.BUNDLE_SUFFIX),
                       ("All files", "*.*")],
        )
        if not chosen:
            return  # user cancelled
        self._set_backup_busy(True, "Restoring\u2026")

        def work() -> str:
            result = migration.import_bundle(PROJECT_ROOT, Path(chosen))
            if not result.get("ok"):
                return str(result.get("error") or "That backup could not be restored.")
            # A restore leaves this machine protected by the file it was
            # restored from, so the last-backup stamp moves to that file's own
            # creation date -- otherwise a freshly rebuilt install reads "No
            # backup yet" over the top of the history it just declined to
            # re-send. Recorded for an already-imported bundle too: it still
            # covers this machine, and a second attempt is exactly when someone
            # is checking whether they are protected.
            made = migration.created_at_epoch(str(result.get("created_at") or ""))
            if made > 0:
                self.after(0, lambda: self._record_backup_cover(made))
            if result.get("already_imported"):
                return "That backup has already been restored here. Nothing changed."
            self.after(0, lambda: self._apply_restored_settings(result.get("settings") or {}))
            chats = int(result.get("chats_added") or 0)
            hashes = int(result.get("hashes_added") or 0)
            # The password sentence is a next step, not a fact, and it is only a
            # next step when there is no connection yet -- restoring onto a
            # machine that is already connected was asking for something the app
            # already had.
            finish = (
                "" if self._transport is not None
                else " Enter your mail password once to finish."
            )
            return (
                f"Restored {chats} chat{'' if chats == 1 else 's'} and "
                f"{hashes} message{'' if hashes == 1 else 's'} of history \u2014 "
                f"those will not be sent again.{finish}"
            )

        self._run_backup_job(work)

    def _apply_restored_settings(self, restored: dict) -> None:
        """Write the restored preferences straight through, and move the form
        on top of them.

        Saved here rather than left for the Save button: a restore is not a
        settings edit the user might cancel, and half of what came back
        (the mail backend and server details) belongs to the mail account
        screen, which this one does not write for. The widgets that *do* show a
        restored value are moved with it, so Save cannot then put the old one
        back.
        """
        if not self.winfo_exists():
            return
        merged = dict(self._app._settings)
        merged.update(restored)
        self._app._settings = merged
        _save_settings(merged)

        if "chunk_size" in restored:
            self._chunk_var.set(str(restored["chunk_size"]))
        if "watch_interval_minutes" in restored:
            minutes = int(restored["watch_interval_minutes"])
            label = next(
                (k for k, v in _WATCH_INTERVAL_OPTIONS.items() if v == minutes), None
            )
            if label:
                self._watch_interval_var.set(label)
        if "synced_file_policy" in restored:
            label = _SYNCED_FILE_POLICY_LABELS.get(str(restored["synced_file_policy"]))
            if label:
                self._synced_policy_var.set(label)
        self._render_account_summary()

    def _on_save(self) -> None:
        # Start from a full copy of the existing settings so keys this dialog
        # doesn't manage -- the mail account's own, backend_notice_shown, and
        # so on -- are preserved rather than dropped on save.
        new_settings = dict(self._app._settings)
        new_settings["chunk_size"] = self._chunk_var.get()
        new_settings["auto_refresh_label"] = self._refresh_var.get()

        new_settings["watched_folder_path"] = self._watched_path
        new_settings["auto_watch_enabled"] = bool(
            self._auto_watch_var.get() and self._watched_path
        )
        new_settings["watch_interval_minutes"] = _WATCH_INTERVAL_OPTIONS.get(
            self._watch_interval_var.get(), DEFAULT_WATCH_INTERVAL_MINUTES
        )
        new_settings["synced_file_policy"] = _SYNCED_FILE_POLICY_LABELS_REV.get(
            self._synced_policy_var.get(), "leave"
        )
        if self._reset_watch_ledgers:
            new_settings["imported_source_paths"] = []

        self._app._apply_settings(new_settings)
        self._close()


class _MailAccountPanel(_Panel):
    """The mail account on its own screen, as Android's MailAccountScreen.

    It owns everything about how mail is sent -- backend, IMAP server details,
    app password, and the app-password help -- and saves them itself, so the
    settings screen it opens from never has to know about any of it.
    """

    def __init__(self, app: "App", master) -> None:
        super().__init__(app, master, "Mail account", "Back to settings")

        pad = {"padx": 20, "pady": 8}
        settings = app._settings

        # Same arrangement as the settings screen and for the same reason:
        # Save and Cancel live outside the scrolling body, so the expanded
        # app-password help -- which runs to more text than fits here -- cannot
        # push them out of reach. That failure is why the help block was moved
        # behind a toggle in the first place.
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(side="bottom", fill="x", padx=20, pady=(8, 12))

        self._save_btn = ctk.CTkButton(
            btn_row, text="Save", width=100, height=32,
            command=self._on_save,
        )
        self._save_btn.pack(side="right", padx=(6, 0))

        ctk.CTkButton(
            btn_row, text="Cancel", width=80, height=32,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._close,
        ).pack(side="right")

        # Test connection: parity with Android's MailAccountScreen, which has
        # had one since the IMAP backend shipped while this window had none at
        # all -- so the only way to find out whether the details worked was to
        # Save them. Left-packed, away from Save/Cancel: it is not a commit
        # action, and sitting next to Save is how people press it expecting
        # the window to close. Starts disabled and follows the three fields it
        # needs -- see _update_test_btn_state.
        self._test_btn = ctk.CTkButton(
            btn_row, text="Test connection", width=130, height=32,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._on_test_connection,
            state="disabled",
        )
        self._test_btn.pack(side="left")

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)

        # Backend line, IMAP form and help all live in _mail_frame with
        # nothing packed after them. That is load-bearing: pack_forget drops a
        # widget out of the packing order and a later bare pack() appends it at
        # the end of its parent, so while these shared a parent with Save/Cancel
        # any show/hide re-packed the form underneath its own buttons. Their
        # own container makes the order true by construction.
        self._mail_frame = ctk.CTkFrame(body, fg_color="transparent")
        self._mail_frame.pack(fill="x")

        # One instance per mailbox.
        #
        # Stated here, and stated first, because this screen is where a second
        # instance gets pointed at a mailbox the first one is already archiving
        # into -- which is the exact moment the mistake is made. The record of
        # what has been sent lives in this instance's own sync_state.db, not in
        # the mailbox, so the second one starts from zero knowledge and re-files
        # every chat it is given. Nothing downstream can catch that: the
        # de-duplication the rest of the app does is per-instance by
        # construction, and this app can add mail but never remove it, so the
        # user is the only one who can clean up afterwards.
        #
        # "Instance", not "device" or "platform": two PCs, two phones, and two
        # copies of the portable app in different folders on one PC are all the
        # same failure, since each copy carries its own Data\. Naming
        # Windows-vs-Android would read as an exhaustive list and quietly bless
        # the other cases.
        #
        # Packed before row3 and never pack_forget'd, and deliberately
        # outside _imap_frame: the limitation is a property of the local state
        # file, not of the mail backend.
        #
        # Weighting, decided deliberately: this is ONE quiet line on a screen
        # the user visits perhaps twice, in the same muted style as every other
        # note in the app -- not a dialog, not a banner, not a warning colour,
        # and not repeated on the sync screen. A caveat that interrupts work it
        # does not apply to gets dismissed unread, and then it is not protecting
        # anyone. It carries its weight by being in the right place at the right
        # moment; the full explanation lives in the user guide and help.
        ctk.CTkLabel(
            self._mail_frame,
            text=(
                "One instance per mailbox. What has already been archived is "
                "remembered by this copy of the app, not by your mailbox, so "
                "any second instance using the same account — another PC, "
                "a phone, or a second copy here — will archive the same "
                "chats again, unless you carry that record across with "
                "Settings -> Backup & restore."
            ),
            wraplength=360, justify="left", anchor="w",
            text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        ).pack(fill="x", padx=20, pady=(8, 0))

        row3 = ctk.CTkFrame(self._mail_frame, fg_color="transparent")
        row3.pack(fill="x", **pad)
        ctk.CTkLabel(row3, text="Connect via:", width=130, anchor="w").pack(side="left")
        # A statement of fact, not a choice: there is one backend, and a
        # one-item dropdown is a worse lie than a label because it implies
        # there is something else behind it. Google sign-in was removed in
        # v2.0.0 -- see src/config.py for why, and docs/RESTORING-OAUTH.md if
        # it ever has to come back.
        ctk.CTkLabel(
            row3, text=_BACKEND_LABELS[MAIL_BACKEND_IMAP], anchor="w",
        ).pack(side="left")

        # ── IMAP fields (shown only when backend == imap) ──────────────
        self._imap_frame = ctk.CTkFrame(self._mail_frame, fg_color="transparent")

        # The way back into the guided setup. The wizard runs by itself the
        # first time, but it is the only place that walks somebody through
        # getting an app password in order, so it has to stay reachable
        # afterwards -- a switched provider, or a password the provider has
        # since revoked, puts an existing user back at exactly the problem it
        # was written for. Offered here rather than replacing this form, which
        # is the faster path for fixing a typo. Same button, same words, as
        # Android's MailAccountScreen.
        ctk.CTkButton(
            self._imap_frame, text="Set up again, step by step", height=30,
            fg_color="transparent", border_width=1, text_color=gui_theme.ON_SURFACE,
            command=lambda: self._app._push_panel(_MailWizardPanel),
        ).pack(fill="x", padx=20, pady=(0, 8))

        prow = ctk.CTkFrame(self._imap_frame, fg_color="transparent")
        prow.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkLabel(prow, text="Provider:", width=130, anchor="w").pack(side="left")
        current_provider = settings.get("imap_provider", "gmail")
        self._provider_var = ctk.StringVar(
            value=_PROVIDER_LABELS.get(current_provider, _PROVIDER_LABELS["gmail"])
        )
        ctk.CTkOptionMenu(
            prow, values=list(_PROVIDER_LABELS.values()),
            variable=self._provider_var, width=190, height=30,
            command=lambda _v: self._on_provider_changed(),
        ).pack(side="left")

        hrow = ctk.CTkFrame(self._imap_frame, fg_color="transparent")
        hrow.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkLabel(hrow, text="Host:", width=130, anchor="w").pack(side="left")
        self._host_entry = ctk.CTkEntry(hrow, width=190, height=30)
        self._host_entry.insert(0, settings.get("imap_host", "") or "")
        self._host_entry.pack(side="left")

        prow2 = ctk.CTkFrame(self._imap_frame, fg_color="transparent")
        prow2.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkLabel(prow2, text="Port:", width=130, anchor="w").pack(side="left")
        self._port_entry = ctk.CTkEntry(prow2, width=190, height=30)
        self._port_entry.insert(0, str(settings.get("imap_port", 993)))
        self._port_entry.pack(side="left")

        erow = ctk.CTkFrame(self._imap_frame, fg_color="transparent")
        erow.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkLabel(erow, text="Email address:", width=130, anchor="w").pack(side="left")
        self._email_entry = ctk.CTkEntry(erow, width=190, height=30)
        self._email_entry.insert(0, settings.get("imap_email", "") or "")
        self._email_entry.pack(side="left")

        pwrow = ctk.CTkFrame(self._imap_frame, fg_color="transparent")
        pwrow.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkLabel(pwrow, text="App password:", width=130, anchor="w").pack(side="left")
        self._password_entry = ctk.CTkEntry(pwrow, width=190, height=30, show="*")
        self._password_entry.pack(side="left")

        note_text = (
            "Leave blank to keep the currently saved password. "
            "The password is never shown or logged."
        )
        ctk.CTkLabel(
            self._imap_frame, text=note_text, wraplength=340,
            justify="left", text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        ).pack(fill="x", padx=20, pady=(0, 4))

        # wraplength/anchor because this label now carries the staged
        # connection-test result, which is a sentence rather than the two
        # words ("Testing connection…") it used to hold.
        self._status_label = ctk.CTkLabel(
            self._imap_frame, text="", text_color=gui_theme.ON_SURFACE_VARIANT,
            wraplength=340, justify="left", anchor="w",
        )
        self._status_label.pack(fill="x", padx=20, pady=(0, 4))

        # KeyRelease, not a StringVar trace: these entries were built without
        # textvariables and adding them now would mean re-plumbing every read
        # in _on_save. The button state is cosmetic gating, so the one-event
        # lag on paste-by-menu is not worth that churn.
        for entry in (self._host_entry, self._port_entry, self._email_entry):
            entry.bind("<KeyRelease>", lambda _e: self._update_test_btn_state())

        self._apply_host_field_state()
        self._update_test_btn_state()
        self._imap_frame.pack(fill="x")

        # ── App-password help (collapsed by default) ───────────────────
        # This used to sit inside _imap_frame, between the password field
        # and the Save/Cancel row -- and got rejected for exactly the
        # problem Android hit first: expanded, it runs to more text than
        # this whole window, and it pushed the one control every user
        # needs (Save) off the bottom. It stays behind a toggle, mirroring
        # where Android ended up after the same correction; Save/Cancel now
        # sit outside the scrolling body, so no amount of help text can
        # reach them.
        self._help_expanded = False
        self._help_container = ctk.CTkFrame(self._mail_frame, fg_color="transparent")

        self._help_toggle_btn = ctk.CTkButton(
            self._help_container, text="Not sure how to get an app password?",
            fg_color="transparent", hover_color=gui_theme.NEUTRAL_HOVER,
            text_color=gui_theme.ON_SURFACE, font=("", 11),
            anchor="w", height=24,
            command=self._toggle_help,
        )
        self._help_toggle_btn.pack(fill="x", padx=20, pady=(0, 4))

        # Content frame -- left unpacked (collapsed) until _toggle_help
        # packs it; its children are rebuilt by _render_help_content each
        # time it's shown or the provider changes, since the steps/notes/
        # links are all provider-specific.
        self._help_frame = ctk.CTkFrame(self._help_container, fg_color="transparent")

        self._help_container.pack(fill="x")

    def on_reveal(self) -> None:
        """Called by App._pop_panel when the wizard closes back onto this one.

        The wizard writes provider/host/port/email itself, so without this the
        form underneath would still be showing what was there before -- and its
        Save reads straight from these widgets, which would quietly undo the
        setup the user just completed.
        """
        settings = self._app._settings
        self._provider_var.set(
            _PROVIDER_LABELS.get(
                settings.get("imap_provider", "gmail"), _PROVIDER_LABELS["gmail"]
            )
        )
        self._email_entry.delete(0, "end")
        self._email_entry.insert(0, settings.get("imap_email", "") or "")
        # Rewrites host and port from the provider preset, and re-disables the
        # host field for a known provider.
        self._apply_host_field_state()
        if self._provider_var.get() == _PROVIDER_LABELS["custom"]:
            self._host_entry.delete(0, "end")
            self._host_entry.insert(0, settings.get("imap_host", "") or "")
            self._port_entry.delete(0, "end")
            self._port_entry.insert(0, str(settings.get("imap_port", 993)))
        self._status_label.configure(text="", text_color=gui_theme.ON_SURFACE_VARIANT)
        self._update_test_btn_state()

    # ------------------------------------------------------------------
    # Provider-driven host/port autofill
    # ------------------------------------------------------------------

    def _on_provider_changed(self) -> None:
        self._apply_host_field_state()
        if self._help_expanded:
            # Steps/notes/links are all keyed off the provider, so re-render
            # rather than leaving the previous provider's help on screen.
            self._render_help_content()

    def _apply_host_field_state(self) -> None:
        provider_key = _PROVIDER_LABELS_REV.get(self._provider_var.get(), "gmail")
        info = IMAP_PROVIDERS.get(provider_key, IMAP_PROVIDERS["custom"])
        if provider_key == "custom":
            self._host_entry.configure(state="normal")
        else:
            # state="normal" first is required, not defensive: a disabled Tk
            # entry silently drops delete/insert. Coming from another
            # non-custom provider the field is already disabled, so this used
            # to no-op and leave the previous provider's host in place --
            # every non-Gmail user got imap.gmail.com, and _on_save reads
            # straight from this widget, so the wrong host was saved too.
            self._host_entry.configure(state="normal")
            self._host_entry.delete(0, "end")
            self._host_entry.insert(0, info["host"] or "")
            self._host_entry.configure(state="disabled")
            self._port_entry.delete(0, "end")
            self._port_entry.insert(0, str(info["port"]))
        self._update_test_btn_state()

    # ------------------------------------------------------------------
    # Test connection
    # ------------------------------------------------------------------

    def _update_test_btn_state(self) -> None:
        """Enable [Test connection] only when there is something to test.

        Called from two places because the fields move under two different
        hands: the user typing, and the provider menu autofilling host and
        port.
        """
        if getattr(self, "_test_running", False):
            return
        usable = (
            bool(self._host_entry.get().strip())
            and bool(self._port_entry.get().strip())
            and bool(self._email_entry.get().strip())
        )
        self._test_btn.configure(state="normal" if usable else "disabled")

    def _on_test_connection(self) -> None:
        provider_key = _PROVIDER_LABELS_REV.get(self._provider_var.get(), "gmail")
        info = IMAP_PROVIDERS.get(provider_key, IMAP_PROVIDERS["custom"])
        host = self._host_entry.get().strip() or (info["host"] or "")
        try:
            port = int(self._port_entry.get().strip())
        except ValueError:
            port = info["port"]
        email = self._email_entry.get().strip()

        # A typed password is tested as typed; an empty field falls back to
        # the saved credential, matching what Save does. Without the
        # fallback, testing a working saved account would always report a
        # rejected login -- the field is deliberately never pre-filled.
        password = self._password_entry.get()
        if not password:
            password = self._saved_imap_password()
            if not password:
                self._status_label.configure(
                    text="Enter the app password to test, or save one first."
                )
                return

        self._test_running = True
        self._test_btn.configure(state="disabled")
        self._status_label.configure(text="Testing connection…")
        result_q: queue.Queue = queue.Queue()
        threading.Thread(
            target=test_imap_connection,
            args=(result_q, host, port, email, password),
            daemon=True,
        ).start()
        self.after(150, lambda: self._poll_test_connection(result_q))

    def _saved_imap_password(self) -> str:
        """The stored app password, or "" if there isn't one we can read.

        Swallows every failure on purpose: this is a convenience fallback
        for the test button, and a credential file that cannot be decrypted
        is a separate problem with its own message elsewhere. The password is
        never returned to the UI, only handed to the worker thread.
        """
        try:
            if not IMAP_CREDENTIALS_FILE.exists():
                return ""
            data = json.loads(IMAP_CREDENTIALS_FILE.read_text(encoding="utf-8"))
            return resolve_imap_password(data) or ""
        except Exception:
            return ""

    def _poll_test_connection(self, result_q: "queue.Queue") -> None:
        try:
            event = result_q.get_nowait()
        except queue.Empty:
            self.after(150, lambda: self._poll_test_connection(result_q))
            return

        # The worker posts each stage as it finishes as well as the final
        # verdict. This button has one line to say it in, so it names the stage
        # just reached and keeps waiting -- "Signing in…" is a better answer to
        # "why is this taking so long" than a static "Testing connection…". The
        # wizard has room for the whole list and shows all five.
        if event.get("type") == "test_stage":
            if event["ok"]:
                self._status_label.configure(
                    text=f"{event['label']}…",
                    text_color=gui_theme.ON_SURFACE_VARIANT,
                )
            self.after(50, lambda: self._poll_test_connection(result_q))
            return

        self._test_running = False
        self._update_test_btn_state()
        # A staged test is a real login attempt, so its verdict is as good as
        # any -- and this is the one button whose whole purpose is to answer
        # "does this work", which until now it answered only in a line of text
        # that vanished with the panel. The header keeps the answer.
        self._app._record_connection(bool(event["ok"]))
        self._app._check_auth()
        # Shown in place rather than in a messagebox: the whole point of the
        # staged test is to read the failure while looking at the fields that
        # caused it, and a modal covers them. Also honours the standing "no
        # pop-ups -- in the main window" rule for this window's own results.
        self._status_label.configure(
            text=event["msg"],
            text_color=gui_theme.TERTIARY if event["ok"] else gui_theme.ERROR,
        )

    # ------------------------------------------------------------------
    # App-password help (collapsible, under the IMAP form)
    # ------------------------------------------------------------------

    def _toggle_help(self) -> None:
        self._help_expanded = not self._help_expanded
        if self._help_expanded:
            self._help_toggle_btn.configure(text="Hide app password help")
            self._render_help_content()
            self._help_frame.pack(fill="x", padx=20, pady=(0, 8))
        else:
            self._help_toggle_btn.configure(text="Not sure how to get an app password?")
            self._help_frame.pack_forget()

    def _render_help_content(self) -> None:
        """Rebuild _help_frame's children for the currently selected
        provider. Called on expand and again whenever the provider changes
        while expanded -- simplest to throw the old widgets away and
        rebuild rather than track per-provider diffs for what is, at most,
        a handful of labels and two button rows."""
        for child in self._help_frame.winfo_children():
            child.destroy()

        provider_key = _PROVIDER_LABELS_REV.get(self._provider_var.get(), "gmail")
        provider_label = _PROVIDER_LABELS.get(provider_key, provider_key)
        host = self._host_entry.get().strip()
        help_url = APP_PASSWORD_HELP_URLS.get(provider_key)
        help_text = APP_PASSWORD_HELP_TEXT.get(provider_key)

        def secondary(text: str) -> None:
            # anchor="w" as well as justify="left": justify only aligns lines
            # within the text block, while anchor places that block inside the
            # label, which fill="x" has stretched to the full frame width. With
            # the default centre anchor, every step whose longest line is
            # shorter than the frame got its own indent, so a numbered list
            # rendered as a ragged zig-zag.
            ctk.CTkLabel(
                self._help_frame, text=text, wraplength=340, anchor="w",
                justify="left", text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
            ).pack(fill="x", pady=(0, 4))

        if provider_key == "custom":
            secondary(
                "Turn on two-factor authentication in your email account first, then look "
                "for \"App passwords\" or \"App-specific passwords\" in its security settings."
            )
        elif help_text:
            secondary(help_text)

        # Inline numbered steps -- only for the two providers whose official
        # pages were actually read and translated into steps here (Gmail,
        # Outlook). Every other provider relies on the help-page link and
        # the prompt buttons below instead of guessed steps.
        inline_steps = {"gmail": APP_PASSWORD_STEPS_GMAIL, "outlook": APP_PASSWORD_STEPS_OUTLOOK}.get(provider_key)
        if inline_steps:
            for i, step in enumerate(inline_steps, start=1):
                secondary(f"{i}. {step}")
            secondary(
                f"Steps checked {APP_PASSWORD_STEPS_REVIEWED}. If they don't match what you "
                "see, use the buttons below to get the current version."
            )

        # Provider-specific gotchas that aren't obvious from the generic
        # help text above, surfaced only when they're relevant.
        if provider_key == "outlook":
            secondary(
                "Work or school Microsoft 365 accounts often have IMAP access disabled by "
                "the organisation's administrator — if so, even a correct app password "
                "will be rejected."
            )
        if provider_key == "icloud":
            secondary(
                "This must be an app-specific password generated at appleid.apple.com, not "
                "your main Apple ID password."
            )

        # A live-current fallback (and the primary path for providers with
        # no inline steps) that doesn't depend on any URL staying valid.
        # The prompt text itself never contains the email/password -- see
        # _build_app_password_prompt's own doc comment for why that
        # boundary matters here specifically.
        link_row = ctk.CTkFrame(self._help_frame, fg_color="transparent")
        link_row.pack(fill="x", pady=(4, 0))
        ctk.CTkButton(
            link_row, text="Copy question", width=110, height=26,
            fg_color="transparent", border_width=1, text_color=gui_theme.ON_SURFACE,
            command=lambda: self._copy_prompt(provider_key, provider_label, host),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            link_row, text="Search for steps", width=120, height=26,
            fg_color="transparent", border_width=1, text_color=gui_theme.ON_SURFACE,
            command=lambda: self._search_prompt(provider_key, provider_label, host),
        ).pack(side="left")

        self._help_copied_label = ctk.CTkLabel(
            self._help_frame, text="", text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        )
        self._help_copied_label.pack(fill="x", pady=(2, 0))

        # Lower-emphasis third option: the static, pre-verified link.
        # Precise when current, but only as fresh as the last time someone
        # re-verified it -- the two buttons above don't have that expiry
        # problem.
        if help_url:
            ctk.CTkButton(
                self._help_frame, text=f"Open {provider_label}'s help page",
                height=26, fg_color="transparent", border_width=1,
                text_color=gui_theme.ON_SURFACE,
                command=lambda: webbrowser.open(help_url),
            ).pack(fill="x", pady=(4, 0))

    def _copy_prompt(self, provider_key: str, provider_label: str, host: str) -> None:
        prompt = _build_app_password_prompt(provider_key, provider_label, host)
        # clipboard_clear/append + update (not update_idletasks) is the Tk
        # idiom for a clipboard write that survives after this window --
        # and the app itself, in the "Copy question" case -- closes.
        self.clipboard_clear()
        self.clipboard_append(prompt)
        self.update()
        if hasattr(self, "_help_copied_label"):
            self._help_copied_label.configure(text="Copied — paste it into an AI assistant.")

    def _search_prompt(self, provider_key: str, provider_label: str, host: str) -> None:
        import urllib.parse
        prompt = _build_app_password_prompt(provider_key, provider_label, host)
        webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(prompt))

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _on_save(self) -> None:
        # Start from a full copy of the existing settings so the keys this
        # window doesn't manage -- everything the Settings window owns, plus
        # backend_notice_shown and friends -- survive rather than being
        # dropped. Settings can be open behind this one, but it only writes on
        # its own Save, so neither can silently revert the other.
        new_settings = dict(self._app._settings)

        # Written on every Save, not carried through: a settings file left
        # over from the OAuth era still says "gmail_oauth", and saving this
        # screen is the moment that stops being true of it.
        backend = MAIL_BACKEND_IMAP
        new_settings["mail_backend"] = backend

        password = self._password_entry.get()

        if backend == MAIL_BACKEND_IMAP:
            provider_key = _PROVIDER_LABELS_REV.get(self._provider_var.get(), "gmail")
            info = IMAP_PROVIDERS.get(provider_key, IMAP_PROVIDERS["custom"])
            host = self._host_entry.get().strip() or (info["host"] or "")
            try:
                port = int(self._port_entry.get().strip())
            except ValueError:
                port = info["port"]
            email = self._email_entry.get().strip()

            if provider_key == "custom" and not host:
                messagebox.showerror("Mail account", "Enter a host for a custom IMAP server.")
                return
            if not email:
                messagebox.showerror("Mail account", "Enter the email address to connect with.")
                return

            new_settings["imap_provider"] = provider_key
            new_settings["imap_host"] = host
            new_settings["imap_port"] = port
            new_settings["imap_email"] = email

            if password:
                # A password was typed -- validate it before persisting
                # anything, so a bad password never silently overwrites a
                # working saved credential. Runs in a background thread;
                # the password itself never gets logged or echoed back.
                self._save_btn.configure(state="disabled")
                # text_color reset explicitly: a previous [Test connection]
                # result may have left this label red or green, and a stale
                # colour under new text reads as a verdict on the new text.
                self._status_label.configure(
                    text="Testing connection…", text_color=gui_theme.ON_SURFACE_VARIANT,
                )
                result_q: queue.Queue = queue.Queue()
                threading.Thread(
                    target=connect_imap,
                    args=(result_q, host, port, email, password),
                    daemon=True,
                ).start()
                self.after(150, lambda: self._poll_imap_test(result_q, new_settings))
                return
            # No password typed: keep whatever credentials file already
            # exists (if any) and just persist the non-secret fields.

        self._app._apply_settings(new_settings)
        self._close()

    def _poll_imap_test(self, result_q: "queue.Queue", new_settings: dict) -> None:
        try:
            event = result_q.get_nowait()
        except queue.Empty:
            self.after(150, lambda: self._poll_imap_test(result_q, new_settings))
            return

        if event["type"] == "auth_ok":
            self._status_label.configure(text="")
            self._app._transport = event["transport"]
            # Recorded after _apply_settings, not before: a backend switch in
            # the same Save clears the verdict on its way through, and this
            # attempt is the newer fact of the two.
            self._app._apply_settings(new_settings)
            self._app._record_connection(True)
            self._app._check_auth()
            self._close()
        else:
            self._save_btn.configure(state="normal")
            self._status_label.configure(text="")
            # Recorded even though nothing was persisted -- connect_imap
            # validates before it saves, so the stored account is untouched.
            # The user just watched the mailbox refuse a login, and a header
            # still claiming "Connected" through that is the exact
            # contradiction Batch G exists to remove. Matches Android's
            # saveImapSettings, which records both outcomes.
            self._app._record_connection(False)
            self._app._check_auth()
            messagebox.showerror(
                "Could not connect",
                f"Could not connect with those details:\n\n{event['msg']}",
            )


# The four wizard steps, in order. Titles only -- each step draws itself in
# _MailWizardPanel._render. Same four, in the same order, with the same words
# as Android's MailSetupWizard.kt (PLATFORM-PARITY.md).
_WIZARD_TITLES = (
    "Who hosts your email?",
    "Get an app password",
    "Sign in",
    "Connecting",
)


class _MailWizardPanel(_Panel):
    """The guided four-step path to a working mailbox, as Android's
    MailSetupWizardScreen.

    It sits next to -- not instead of -- _MailAccountPanel's single-page form.
    The form is the faster path once you know what the fields mean; this is for
    the part that actually stops people, which is not the form at all but
    getting an app password out of their provider, hence that being a step of
    its own. Reachable on first setup and afterwards from the account screen,
    because a revoked password or a changed provider puts an existing user back
    at exactly the problem it was written for.

    Never asks which backend to use: a new account is IMAP, and the demoted
    Gmail sign-in stays reachable only from the full account screen. Draws in
    the main window like every other panel -- no pop-ups -- and the way back is
    the labelled bar button.
    """

    def __init__(self, app: "App", master) -> None:
        super().__init__(app, master, "Set up your mailbox", "Back")

        settings = app._settings
        self._step = 0
        self._provider_key = settings.get("imap_provider", "gmail")
        self._email = settings.get("imap_email", "") or ""
        self._password = ""
        self._custom_host = ""
        self._custom_port = "993"
        # No dict of stage results here on purpose. There used to be one,
        # documented as the thing the marks were drawn from -- and nothing
        # ever wrote to it, because the marks are set on the live widgets as
        # each event arrives and step 4 always starts a fresh check on entry,
        # which reports all five again. A second copy of that state would only
        # be a copy that can disagree.
        self._connecting = False
        self._outcome: "tuple[bool, str] | None" = None

        self._step_label = ctk.CTkLabel(
            self, text="", anchor="w",
            text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        )
        self._step_label.pack(fill="x", padx=20, pady=(10, 0))

        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._body.pack(fill="both", expand=True)

        self._render()

    # ------------------------------------------------------------------
    # Step plumbing
    # ------------------------------------------------------------------

    def _goto(self, step: int) -> None:
        # A check in flight owns the widgets it is writing into, so the step
        # buttons that could move out from under it are simply not drawn while
        # it runs (see step 4). This is the belt to that's braces.
        if self._connecting:
            return
        self._step = step
        self._render()

    def _provider_info(self) -> dict:
        return IMAP_PROVIDERS.get(self._provider_key, IMAP_PROVIDERS["custom"])

    def _effective_host(self) -> str:
        if self._provider_key == "custom":
            return self._custom_host.strip()
        return self._provider_info()["host"] or ""

    def _effective_port(self) -> int:
        if self._provider_key == "custom":
            try:
                return int(self._custom_port.strip())
            except ValueError:
                return 993
        return self._provider_info()["port"]

    def _secondary(self, parent, text: str) -> None:
        ctk.CTkLabel(
            parent, text=text, wraplength=360, anchor="w", justify="left",
            text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        ).pack(fill="x", padx=20, pady=(0, 4))

    def _render(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        # Destroying the children does not move the scroll position, so a step
        # entered from a scrolled-down one would open partway through itself --
        # arriving at step 2 already past "I have my app password", which is the
        # one button that step exists to offer. Every step starts at its top.
        self._body._parent_canvas.yview_moveto(0)
        self._step_label.configure(
            text=f"Step {self._step + 1} of 4 — {_WIZARD_TITLES[self._step]}"
        )
        (self._render_provider, self._render_help,
         self._render_credentials, self._render_connect)[self._step]()

    # ------------------------------------------------------------------
    # Step 1 — provider
    # ------------------------------------------------------------------

    def _render_provider(self) -> None:
        self._secondary(
            self._body,
            "Pick the service your email address belongs to. It decides where "
            "Chat Mail Sync files your chats, and how you get the password it needs.",
        )
        var = ctk.StringVar(value=_PROVIDER_LABELS.get(self._provider_key, "Gmail"))

        def picked(_v=None) -> None:
            self._provider_key = _PROVIDER_LABELS_REV.get(var.get(), "gmail")
            self._render()

        row = ctk.CTkFrame(self._body, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(8, 8))
        ctk.CTkLabel(row, text="Provider:", width=110, anchor="w").pack(side="left")
        ctk.CTkOptionMenu(
            row, values=list(_PROVIDER_LABELS.values()),
            variable=var, width=190, height=30, command=picked,
        ).pack(side="left")

        # For a known provider the server settings are a fact to be told, not a
        # question to be asked: they are already in IMAP_PROVIDERS, and getting
        # them wrong is a failure the user cannot diagnose. Only "Other (IMAP)"
        # gets real fields, and those are on step 3 with everything else that
        # has to be typed.
        if self._provider_key != "custom":
            info = self._provider_info()
            self._secondary(
                self._body,
                f"Chat Mail Sync will use {info['host']}, port {info['port']}. "
                "Nothing to set up.",
            )

        ctk.CTkButton(
            self._body, text="Next", height=32, command=lambda: self._goto(1),
        ).pack(fill="x", padx=20, pady=(8, 4))

    # ------------------------------------------------------------------
    # Step 2 — app password help
    # ------------------------------------------------------------------

    def _render_help(self) -> None:
        # The primary button sits ABOVE the instructions on purpose. Anybody
        # who already has an app password -- and second time through, most
        # people do -- should not have to scroll past a page written for their
        # first time to say so.
        ctk.CTkButton(
            self._body, text="I have my app password", height=32,
            command=lambda: self._goto(2),
        ).pack(fill="x", padx=20, pady=(8, 8))
        self._secondary(
            self._body,
            "An app password is a separate password your provider issues for one "
            "app. It is not your normal password, and you can revoke it without "
            "touching your account.",
        )
        self._render_help_body(self._body)
        ctk.CTkButton(
            self._body, text="Back a step", height=30,
            fg_color="transparent", border_width=1, text_color=gui_theme.ON_SURFACE,
            command=lambda: self._goto(0),
        ).pack(fill="x", padx=20, pady=(10, 4))

    def _render_help_body(self, parent) -> None:
        """The per-provider app-password instructions.

        Deliberately the same content the account screen shows under its
        collapsed help toggle -- both read APP_PASSWORD_HELP_TEXT / _STEPS_* and
        _build_app_password_prompt, so the two places cannot end up telling
        people different things. The prompt never carries the email address or
        the password; see _build_app_password_prompt.
        """
        provider_key = self._provider_key
        provider_label = _PROVIDER_LABELS.get(provider_key, provider_key)
        host = self._effective_host()
        help_url = APP_PASSWORD_HELP_URLS.get(provider_key)
        help_text = APP_PASSWORD_HELP_TEXT.get(provider_key)

        if provider_key == "custom":
            self._secondary(
                parent,
                "Turn on two-factor authentication in your email account first, then look "
                "for \"App passwords\" or \"App-specific passwords\" in its security settings.",
            )
        elif help_text:
            self._secondary(parent, help_text)

        inline_steps = {
            "gmail": APP_PASSWORD_STEPS_GMAIL,
            "outlook": APP_PASSWORD_STEPS_OUTLOOK,
        }.get(provider_key)
        if inline_steps:
            for i, step in enumerate(inline_steps, start=1):
                self._secondary(parent, f"{i}. {step}")
            self._secondary(
                parent,
                f"Steps checked {APP_PASSWORD_STEPS_REVIEWED}. If they don't match what you "
                "see, use the buttons below to get the current version.",
            )
        if provider_key == "outlook":
            self._secondary(
                parent,
                "Work or school Microsoft 365 accounts often have IMAP access disabled by "
                "the organisation's administrator — if so, even a correct app password "
                "will be rejected.",
            )
        if provider_key == "icloud":
            self._secondary(
                parent,
                "This must be an app-specific password generated at appleid.apple.com, not "
                "your main Apple ID password.",
            )

        link_row = ctk.CTkFrame(parent, fg_color="transparent")
        link_row.pack(fill="x", padx=20, pady=(4, 0))
        ctk.CTkButton(
            link_row, text="Copy question", width=110, height=26,
            fg_color="transparent", border_width=1, text_color=gui_theme.ON_SURFACE,
            command=lambda: self._copy_prompt(provider_key, provider_label, host),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            link_row, text="Search for steps", width=120, height=26,
            fg_color="transparent", border_width=1, text_color=gui_theme.ON_SURFACE,
            command=lambda: self._search_prompt(provider_key, provider_label, host),
        ).pack(side="left")

        self._help_copied_label = ctk.CTkLabel(
            parent, text="", text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        )
        self._help_copied_label.pack(fill="x", padx=20, pady=(2, 0))

        if help_url:
            ctk.CTkButton(
                parent, text=f"Open {provider_label}'s help page",
                height=26, fg_color="transparent", border_width=1,
                text_color=gui_theme.ON_SURFACE,
                command=lambda: webbrowser.open(help_url),
            ).pack(fill="x", padx=20, pady=(4, 0))

    def _copy_prompt(self, provider_key: str, provider_label: str, host: str) -> None:
        prompt = _build_app_password_prompt(provider_key, provider_label, host)
        self.clipboard_clear()
        self.clipboard_append(prompt)
        self.update()
        if hasattr(self, "_help_copied_label"):
            self._help_copied_label.configure(text="Copied — paste it into an AI assistant.")

    def _search_prompt(self, provider_key: str, provider_label: str, host: str) -> None:
        import urllib.parse
        prompt = _build_app_password_prompt(provider_key, provider_label, host)
        webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(prompt))

    # ------------------------------------------------------------------
    # Step 3 — the credentials
    # ------------------------------------------------------------------

    def _render_credentials(self) -> None:
        if self._provider_key == "custom":
            hrow = ctk.CTkFrame(self._body, fg_color="transparent")
            hrow.pack(fill="x", padx=20, pady=(8, 8))
            ctk.CTkLabel(hrow, text="Host:", width=110, anchor="w").pack(side="left")
            host_entry = ctk.CTkEntry(hrow, width=190, height=30)
            host_entry.insert(0, self._custom_host)
            host_entry.pack(side="left")

            prow = ctk.CTkFrame(self._body, fg_color="transparent")
            prow.pack(fill="x", padx=20, pady=(0, 8))
            ctk.CTkLabel(prow, text="Port:", width=110, anchor="w").pack(side="left")
            port_entry = ctk.CTkEntry(prow, width=190, height=30)
            port_entry.insert(0, self._custom_port)
            port_entry.pack(side="left")
        else:
            host_entry = port_entry = None
            self._secondary(
                self._body,
                f"Signing in to {_PROVIDER_LABELS.get(self._provider_key, self._provider_key)} "
                f"at {self._effective_host()}, port {self._effective_port()}.",
            )

        erow = ctk.CTkFrame(self._body, fg_color="transparent")
        erow.pack(fill="x", padx=20, pady=(8, 8))
        ctk.CTkLabel(erow, text="Email address:", width=110, anchor="w").pack(side="left")
        email_entry = ctk.CTkEntry(erow, width=190, height=30)
        email_entry.insert(0, self._email)
        email_entry.pack(side="left")

        pwrow = ctk.CTkFrame(self._body, fg_color="transparent")
        pwrow.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkLabel(pwrow, text="App password:", width=110, anchor="w").pack(side="left")
        # Never pre-filled, not even from a saved credential -- same rule as
        # the account screen's field.
        password_entry = ctk.CTkEntry(pwrow, width=190, height=30, show="*")
        password_entry.pack(side="left")

        self._secondary(
            self._body,
            "The password is stored encrypted on this PC only. It is never shown "
            "again, and never written to a log.",
        )
        self._error_label = ctk.CTkLabel(
            self._body, text="", wraplength=360, anchor="w", justify="left",
            text_color=gui_theme.ERROR, font=("", 11),
        )
        self._error_label.pack(fill="x", padx=20, pady=(0, 4))

        def go_connect() -> None:
            if host_entry is not None:
                self._custom_host = host_entry.get()
                self._custom_port = port_entry.get()
            self._email = email_entry.get().strip()
            self._password = password_entry.get()
            if not self._effective_host():
                self._error_label.configure(text="Enter a host for a custom IMAP server.")
                return
            if not self._email or not self._password:
                self._error_label.configure(
                    text="Enter the email address and app password to connect with."
                )
                return
            self._goto(3)

        ctk.CTkButton(
            self._body, text="Connect", height=32, command=go_connect,
        ).pack(fill="x", padx=20, pady=(4, 4))
        ctk.CTkButton(
            self._body, text="I still need an app password", height=30,
            fg_color="transparent", border_width=1, text_color=gui_theme.ON_SURFACE,
            command=lambda: self._goto(1),
        ).pack(fill="x", padx=20, pady=(0, 4))

    # ------------------------------------------------------------------
    # Step 4 — the staged connection
    # ------------------------------------------------------------------

    def _render_connect(self) -> None:
        self._secondary(
            self._body,
            "Checking the connection to "
            f"{_PROVIDER_LABELS.get(self._provider_key, self._provider_key)}.",
        )
        # All five drawn up front, greyed, then lit as the worker reports in: a
        # list that grows a line at a time hides the one fact that makes it
        # useful, which is how much is left. The names and labels come from the
        # Python core so Windows and Android tick off the same five things.
        self._stage_labels = {}
        for stage in connection_stage_plan():
            row = ctk.CTkFrame(self._body, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=(0, 2))
            mark = ctk.CTkLabel(row, text="…", width=28, anchor="w",
                                text_color=gui_theme.ON_SURFACE_VARIANT)
            mark.pack(side="left")
            text = ctk.CTkLabel(row, text=stage["label"], anchor="w",
                                text_color=gui_theme.ON_SURFACE_VARIANT)
            text.pack(side="left")
            self._stage_labels[stage["name"]] = (mark, text)

        self._wizard_status = ctk.CTkLabel(
            self._body, text="", wraplength=360, anchor="w", justify="left",
            text_color=gui_theme.ON_SURFACE_VARIANT, font=("", 11),
        )
        self._wizard_status.pack(fill="x", padx=20, pady=(8, 4))
        self._wizard_buttons = ctk.CTkFrame(self._body, fg_color="transparent")
        self._wizard_buttons.pack(fill="x")

        self._start_check()

    def _start_check(self) -> None:
        self._outcome = None
        self._connecting = True
        result_q: queue.Queue = queue.Queue()
        threading.Thread(
            target=test_imap_connection,
            args=(result_q, self._effective_host(), self._effective_port(),
                  self._email, self._password),
            daemon=True,
        ).start()
        self.after(100, lambda: self._poll_wizard_check(result_q))

    def _poll_wizard_check(self, result_q: "queue.Queue") -> None:
        # winfo_exists: the panel can be closed with the check still running,
        # and Tk raises on any widget touched after that.
        if not self.winfo_exists():
            return
        try:
            event = result_q.get_nowait()
        except queue.Empty:
            self.after(100, lambda: self._poll_wizard_check(result_q))
            return

        if event.get("type") == "test_stage":
            widgets = self._stage_labels.get(event["name"])
            if widgets:
                mark, text = widgets
                colour = gui_theme.TERTIARY if event["ok"] else gui_theme.ERROR
                mark.configure(text="✓" if event["ok"] else "✕", text_color=colour)
                text.configure(text_color=gui_theme.ON_SURFACE)
            self.after(50, lambda: self._poll_wizard_check(result_q))
            return

        self._connecting = False
        self._app._record_connection(bool(event["ok"]))
        self._app._check_auth()
        if event["ok"]:
            # The check proved the details; this is what actually writes them
            # down. Reusing connect_imap rather than persisting here keeps the
            # single "validate, then save" path the account screen uses -- the
            # credential file is written in exactly one place in this app.
            self._wizard_status.configure(
                text="Connected. Saving your details…", text_color=gui_theme.ON_SURFACE_VARIANT,
            )
            save_q: queue.Queue = queue.Queue()
            threading.Thread(
                target=connect_imap,
                args=(save_q, self._effective_host(), self._effective_port(),
                      self._email, self._password),
                daemon=True,
            ).start()
            self.after(100, lambda: self._poll_wizard_save(save_q))
        else:
            self._wizard_status.configure(text=event["msg"], text_color=gui_theme.ERROR)
            self._show_retry_buttons()

    def _poll_wizard_save(self, save_q: "queue.Queue") -> None:
        if not self.winfo_exists():
            return
        try:
            event = save_q.get_nowait()
        except queue.Empty:
            self.after(100, lambda: self._poll_wizard_save(save_q))
            return

        if event["type"] == "auth_ok":
            new_settings = dict(self._app._settings)
            new_settings["mail_backend"] = MAIL_BACKEND_IMAP
            new_settings["imap_provider"] = self._provider_key
            new_settings["imap_host"] = self._effective_host()
            new_settings["imap_port"] = self._effective_port()
            new_settings["imap_email"] = self._email
            self._app._transport = event["transport"]
            self._app._apply_settings(new_settings)
            self._app._record_connection(True)
            self._app._check_auth()
            # The password is not kept on the panel any longer than the save
            # that needed it.
            self._password = ""
            self._wizard_status.configure(
                text=f"Connected — {self._email} is set up.", text_color=gui_theme.TERTIARY,
            )
            ctk.CTkButton(
                self._wizard_buttons, text="Done", height=32, command=self._close,
            ).pack(fill="x", padx=20, pady=(4, 4))
        else:
            self._wizard_status.configure(text=event["msg"], text_color=gui_theme.ERROR)
            self._show_retry_buttons()

    def _show_retry_buttons(self) -> None:
        # Back to the fields rather than a blind retry: a failure at the
        # sign-in stage is almost always a typo, or the normal password used
        # where the app password belongs.
        ctk.CTkButton(
            self._wizard_buttons, text="Check the details and try again", height=32,
            command=lambda: self._goto(2),
        ).pack(fill="x", padx=20, pady=(4, 4))
        ctk.CTkButton(
            self._wizard_buttons, text="Get a new app password", height=30,
            fg_color="transparent", border_width=1, text_color=gui_theme.ON_SURFACE,
            command=lambda: self._goto(1),
        ).pack(fill="x", padx=20, pady=(0, 4))


# ---------------------------------------------------------------------------
# Sync log  (90 days of runs, and one run in full)
# ---------------------------------------------------------------------------

# The same two words Android's SyncLogScreen uses. "watched_folder" is the
# stored value, not something to put in front of anyone.
_TRIGGER_LABELS = {"manual": "Manual", "watched_folder": "Watched folder"}

# Says what happened, not what the column contains: "complete" is a state
# name, and the run detail is opened by someone asking a question.
_STATUS_HEADLINE = {
    "complete": "Finished",
    "failed": "Failed",
    "pending": "Still running",
}


def _format_run_time(iso: "str | None", *, long: bool = False) -> str:
    """An ISO timestamp as a person would say it. Never raises.

    Rows are drawn from whatever is in the DB, and a stored timestamp that
    cannot be parsed is a cosmetic problem -- it must not be the reason the
    log fails to open. dt.day rather than the %-d strftime flag, which is not
    portable (see _refresh_chat_list).
    """
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return str(iso)
    if long:
        return f"{dt.strftime('%b')} {dt.day}, {dt.strftime('%Y')} at {dt.strftime('%H:%M')}"
    return f"{dt.strftime('%b')} {dt.day}, {dt.strftime('%H:%M')}"


def _run_counts_text(run: dict) -> str:
    """The one-line summary of what a run moved.

    A run that uploaded nothing says so in words. "0 synced, 0 skipped" is
    technically the same information and reads as a malfunction.
    """
    synced = run.get("messages_synced") or 0
    skipped = run.get("messages_skipped") or 0
    if run.get("status") == "failed":
        return f"{synced} synced before it failed" if synced else "Nothing uploaded"
    if not synced and not skipped:
        return "Nothing new"
    parts = [f"{synced} synced"]
    if skipped:
        parts.append(f"{skipped} already there")
    return ", ".join(parts)


def _run_duration(run: dict) -> str:
    """How long a run took, or "" when that cannot be worked out."""
    started, completed = run.get("started_at"), run.get("completed_at")
    if not started or not completed:
        return ""
    try:
        seconds = int(
            (datetime.fromisoformat(completed) - datetime.fromisoformat(started)).total_seconds()
        )
    except Exception:
        return ""
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    hours, rest_min = divmod(minutes, 60)
    return f"{hours} h {rest_min} min" if rest_min else f"{hours} h"


def _relative_time(raw: str | None) -> str:
    """"just now" / "2 hours ago" / "yesterday", falling back to the full date
    once a count of days stops meaning anything.

    The status line asks "how long ago?"; the sync log answers "at what time?".
    Mirrors SyncLogScreen.kt's relativeTime so the two front-ends round the
    same way.
    """
    if not raw:
        return "at an unknown time"
    try:
        then = datetime.fromisoformat(raw)
    except Exception:
        return raw
    minutes = int((datetime.now() - then).total_seconds() // 60)
    if minutes < 0:
        # A clock that has moved backwards since the run, not a run in the
        # future. Nothing sensible to say in relative terms, so don't try.
        return f"on {then.strftime('%b')} {then.day}"
    if minutes < 2:
        return "just now"
    if minutes < 60:
        return f"{minutes} minutes ago"
    if minutes < 120:
        return "an hour ago"
    if minutes < 60 * 24:
        return f"{minutes // 60} hours ago"
    if minutes < 60 * 48:
        return "yesterday"
    if minutes < 60 * 24 * 7:
        return f"{minutes // (60 * 24)} days ago"
    return f"on {then.strftime('%b')} {then.day}"


def _summary_counts_text(summary: dict) -> str:
    """What the last finished run moved, in the same words the log uses for a
    single run -- see _run_counts_text, which this deliberately mirrors."""
    synced = summary.get("last_messages_synced") or 0
    skipped = summary.get("last_messages_skipped") or 0
    if summary.get("last_status") == "failed":
        return f"{synced} synced before it failed" if synced else "nothing uploaded"
    if not synced and not skipped:
        return "nothing new"
    if skipped:
        return f"{synced} synced, {skipped} already there"
    return f"{synced} synced"


def _first_line(text: str) -> str:
    """The first line of an error, for a row that has room for one line."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= 120 else line[:117] + "…"


def _human_size(num_bytes: int) -> str:
    """A file size a person can read at a glance, not to the byte."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes} bytes"


def _short_when(epoch_seconds: float) -> str:
    """A file's date, in the same shape the rest of the window uses.

    dt.day rather than the %-d strftime flag, which is not portable.
    """
    dt = datetime.fromtimestamp(epoch_seconds)
    return f"{dt.strftime('%b')} {dt.day}, {dt.strftime('%Y')}"


class _ImportPickerPanel(_Panel):
    """Pick exports out of the watched folder, by chat name.

    Android built this screen because the system file picker truncates every
    export to "WhatsApp Chat with Bijal…", hiding the only part that tells one
    file from another. Windows has no such defect -- askopenfilenames is a full
    Explorer window with names, sizes and dates -- so this is parity of
    capability, not a port of the fix. What Windows genuinely lacked was any
    way to take *some* files out of the watched folder: the check-and-sync button
    is all-or-nothing, and the Explorer dialog opens wherever Explorer last was
    rather than where the exports live.

    The three states are Android's, deliberately: no folder yet -> choose one;
    folder gone -> say so and offer to re-pick, because an empty list reads as
    "you have no exports" and is a dead end; otherwise the list, newest first,
    with anything already queued shown but not selectable.
    """

    _EXTENSIONS = (".txt", ".zip")

    def __init__(self, app: "App", master) -> None:
        super().__init__(app, master, "Import exports", "Back to sync")

        self._vars: dict = {}
        self._show_all = ctk.BooleanVar(value=False)

        # Actions first, and pinned to the bottom, so a long list cannot push
        # [Import] off the window -- the same arrangement as the settings and
        # mail-account screens, for the same reason.
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(side="bottom", fill="x", padx=20, pady=(8, 12))

        self._import_btn = ctk.CTkButton(
            actions, text="Import", width=140, height=32,
            state="disabled", command=self._on_import,
        )
        self._import_btn.pack(side="left")

        self._change_btn = ctk.CTkButton(
            actions, text="Change folder", width=126, height=32,
            fg_color="transparent", border_width=1,
            border_color=gui_theme.OUTLINE_VARIANT,
            hover_color=gui_theme.NEUTRAL_HOVER,
            text_color=gui_theme.ON_SURFACE,
            command=self._on_choose_folder,
        )
        self._change_btn.pack(side="left", padx=8)

        # The folder belongs to WhatsApp, not to us: an export saved while this
        # screen is open does not announce itself, and re-entering the screen to
        # see it is a strange thing to have to know. Rescanning is the whole of
        # _render(), so this is one line of behaviour and no new state.
        ctk.CTkButton(
            actions, text="Refresh", width=90, height=32,
            fg_color="transparent", border_width=1,
            border_color=gui_theme.OUTLINE_VARIANT,
            hover_color=gui_theme.NEUTRAL_HOVER,
            text_color=gui_theme.ON_SURFACE,
            command=self._render,
        ).pack(side="left")

        # Kept, and kept clearly secondary: exports saved somewhere other than
        # the watched folder still have to get in somehow.
        ctk.CTkButton(
            actions, text="Pick a file from anywhere…", width=196, height=32,
            fg_color="transparent", border_width=0,
            hover_color=gui_theme.NEUTRAL_HOVER,
            text_color=gui_theme.ON_SURFACE_VARIANT,
            command=self._on_pick_anywhere,
        ).pack(side="right")

        self._body = ctk.CTkFrame(self, fg_color="transparent")
        self._body.pack(fill="both", expand=True, padx=20, pady=(14, 0))

        self._render()

    # -- State ---------------------------------------------------------

    def _folder(self):
        raw = str(self._app._settings.get("watched_folder_path") or "").strip()
        return Path(raw) if raw else None

    def _queued_names(self) -> set:
        try:
            return {f.name for f in INBOX_DIR.iterdir() if f.is_file()}
        except Exception:
            return set()

    def _render(self) -> None:
        # Carried across the rebuild: Refresh exists to add a newly-saved
        # export to the list, and losing the ticks you had already made while
        # doing it would be a worse trade than not offering Refresh at all.
        keep = {p.name for p in self._selected()}
        for w in self._body.winfo_children():
            w.destroy()
        self._vars = {}
        self._preselect = keep
        self._import_btn.configure(state="disabled", text="Import")

        folder = self._folder()
        if folder is None:
            self._change_btn.configure(text="Choose folder")
            self._no_folder_block()
            return

        self._change_btn.configure(text="Change folder")
        try:
            entries = [f for f in folder.iterdir() if f.is_file()]
        except Exception:
            self._unreachable_block()
            return

        show_all = self._show_all.get()
        hidden = 0
        files = []
        for f in entries:
            if f.suffix.lower() in self._EXTENSIONS or show_all:
                files.append(f)
            else:
                hidden += 1
        files.sort(key=self._modified_at, reverse=True)

        self._list_block(folder, files, hidden)
        self._update_import_btn()

    @staticmethod
    def _modified_at(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except Exception:
            return 0.0

    # -- Blocks --------------------------------------------------------

    def _headline(self, text: str, detail: str) -> None:
        ctk.CTkLabel(
            self._body, text=text, anchor="w",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=gui_theme.ON_SURFACE,
        ).pack(fill="x", pady=(4, 4))
        ctk.CTkLabel(
            self._body, text=detail, anchor="w", justify="left", wraplength=560,
            font=ctk.CTkFont(size=12), text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(fill="x")

    def _no_folder_block(self) -> None:
        self._headline(
            "Choose your exports folder",
            "Point the app at the folder WhatsApp exports are saved into — "
            "usually Downloads. You do this once; after that every export you "
            "save there shows up in this list.",
        )

    def _unreachable_block(self) -> None:
        self._headline(
            "Can't reach that folder any more",
            "It may have been renamed, moved or deleted. Nothing you have "
            "already synced is affected — choose the folder again to carry on "
            "importing from it.",
        )
        ctk.CTkLabel(
            self._body, text=str(self._folder()), anchor="w",
            font=ctk.CTkFont(size=11), text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(fill="x", pady=(8, 0))

    def _list_block(self, folder: Path, files: list, hidden: int) -> None:
        ctk.CTkLabel(
            self._body, text=str(folder), anchor="w",
            font=ctk.CTkFont(size=11), text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(fill="x", pady=(0, 6))

        if not files:
            self._headline(
                "No exports in this folder",
                "In WhatsApp, open a chat, then ⋮ → More → Export chat, and "
                "save the file into this folder.",
            )
        else:
            scroll = ctk.CTkScrollableFrame(self._body, fg_color="transparent")
            scroll.pack(fill="both", expand=True)
            queued = self._queued_names()
            for f in files:
                self._row(scroll, f, f.name in queued)

        if hidden:
            ctk.CTkCheckBox(
                self._body,
                text=f"Show everything in this folder ({hidden} other file"
                     f"{'s' if hidden != 1 else ''} hidden)",
                variable=self._show_all, height=24,
                font=ctk.CTkFont(size=11),
                command=self._render,
            ).pack(anchor="w", pady=(8, 0))

    def _row(self, parent, path: Path, queued: bool) -> None:
        _, display = extract_chat_info(path.name)
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=1)

        var = ctk.BooleanVar(value=path.name in getattr(self, "_preselect", set()))
        box = ctk.CTkCheckBox(
            row, text=display, variable=var, height=24,
            font=ctk.CTkFont(size=12),
            command=self._update_import_btn,
        )
        if queued:
            # Shown rather than hidden: a file missing from the list would send
            # someone back to WhatsApp to export it a second time.
            box.configure(state="disabled")
        else:
            self._vars[path] = var
        _tooltip(box, path.name).pack(side="left")

        try:
            stat = path.stat()
            detail = f"{_human_size(stat.st_size)}  ·  {_short_when(stat.st_mtime)}"
        except Exception:
            detail = ""
        if queued:
            detail = (detail + "  ·  " if detail else "") + "already waiting to sync"
        ctk.CTkLabel(
            row, text=detail, anchor="e",
            font=ctk.CTkFont(size=11), text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(side="right", padx=(8, 4))

    # -- Actions -------------------------------------------------------

    def _selected(self) -> list:
        return [p for p, var in self._vars.items() if var.get()]

    def _update_import_btn(self) -> None:
        n = len(self._selected())
        self._import_btn.configure(
            state="normal" if n else "disabled",
            text="Import" if n <= 1 else f"Import {n} files",
        )

    def _on_import(self) -> None:
        chosen = self._selected()
        if not chosen:
            return
        # _copy_to_inbox skips anything already in the inbox and logs what it
        # did, so the counting and the reporting stay in one place.
        self._app._copy_to_inbox(chosen)
        self._close()

    def _on_pick_anywhere(self) -> None:
        self._close()
        self._app._browse_files()

    def _on_choose_folder(self) -> None:
        current = self._folder()
        chosen = filedialog.askdirectory(
            title="Choose the folder your WhatsApp exports are saved in",
            initialdir=str(current) if current else None,
            parent=self,
        )
        if not chosen:
            return
        new_settings = dict(self._app._settings)
        new_settings["watched_folder_path"] = str(Path(chosen))
        if current is not None and Path(chosen) != current:
            # A previous folder's ledger says nothing about a new one -- the
            # same reset the settings screen does when the folder changes.
            new_settings["imported_source_paths"] = []
        self._app._apply_settings(new_settings)
        self._render()


# Above this many queued files, Home offers the bulk queue screen. Below it,
# the per-row X is simply faster than changing screens. Android uses the same
# number for a different job -- there it is how many rows Home shows at all,
# because on a phone the queue can push Sync Now off the bottom of the screen
# and here it cannot.
_QUEUE_BULK_THRESHOLD = 4


class _QueuePanel(_Panel):
    """Everything waiting to sync, with the actions that only make sense in bulk.

    Android reaches the same screen from "Show all N" because its Home card is
    capped at four rows; Windows reaches it from "Manage queue" because its
    list is not capped and does not need to be. Different doors, same room --
    what would break parity is one platform being able to drop twenty exports
    in a stroke and the other not.
    """

    def __init__(self, app: "App", master) -> None:
        super().__init__(app, master, "Sync queue", "Back to sync")

        self._vars: dict = {}

        # Actions pinned to the bottom before the list is built, so a long
        # queue cannot push them off -- the same arrangement as the import
        # screen, for the same reason.
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(side="bottom", fill="x", padx=20, pady=(8, 12))

        self._remove_btn = ctk.CTkButton(
            actions, text="Remove from queue", width=170, height=32,
            state="disabled",
            fg_color=gui_theme.ERROR_CONTAINER,
            hover_color=gui_theme.ERROR_CONTAINER,
            text_color=gui_theme.ON_ERROR_CONTAINER,
            command=self._on_remove,
        )
        self._remove_btn.pack(side="left")

        self._select_btn = ctk.CTkButton(
            actions, text="Select all", width=100, height=32,
            fg_color="transparent", border_width=1,
            border_color=gui_theme.OUTLINE_VARIANT,
            hover_color=gui_theme.NEUTRAL_HOVER,
            text_color=gui_theme.ON_SURFACE,
            command=self._on_select_all,
        )
        self._select_btn.pack(side="left", padx=8)

        ctk.CTkButton(
            actions, text="Add more exports…", width=160, height=32,
            fg_color="transparent", border_width=0,
            hover_color=gui_theme.NEUTRAL_HOVER,
            text_color=gui_theme.ON_SURFACE_VARIANT,
            command=self._on_add_more,
        ).pack(side="right")

        # Says what removal does and does not do, permanently rather than on
        # confirmation: nothing here has been sent yet, and the export in the
        # folder it came from is untouched. Without that, "remove" reads as if
        # it might be deleting the user's own file.
        ctk.CTkLabel(
            self, text=(
                "Removing takes files out of this queue only. The exports they were "
                "imported from are not touched, and nothing already synced is affected."
            ),
            anchor="w", justify="left", wraplength=600,
            font=ctk.CTkFont(size=11), text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(side="bottom", fill="x", padx=20, pady=(0, 4))

        self._preview_label = ctk.CTkLabel(
            self, text="", anchor="w", justify="left", wraplength=600,
            font=ctk.CTkFont(size=11), text_color=gui_theme.ON_SURFACE,
        )
        self._preview_label.pack(side="bottom", fill="x", padx=20, pady=(0, 4))

        self._headline = ctk.CTkLabel(
            self, text="", anchor="w",
            font=ctk.CTkFont(size=13), text_color=gui_theme.ON_SURFACE,
        )
        self._headline.pack(fill="x", padx=20, pady=(14, 6))

        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._body.pack(fill="both", expand=True, padx=20)

        self._render()

    # -- State ---------------------------------------------------------

    def _files(self) -> list:
        try:
            return sorted(
                (f for f in INBOX_DIR.iterdir()
                 if f.is_file() and f.suffix in (".txt", ".zip", "")),
                key=lambda f: f.name.lower(),
            )
        except Exception:
            return []

    def _checked(self) -> list:
        return [name for name, var in self._vars.items() if var.get()]

    # -- Render --------------------------------------------------------

    def _render(self) -> None:
        for w in self._body.winfo_children():
            w.destroy()
        self._vars = {}

        files = self._files()
        total = sum(self._size_of(f) for f in files)
        self._headline.configure(
            text=(
                f"{len(files)} file{'s' if len(files) != 1 else ''} - {_human_size(total)}"
                if files else "The queue is empty."
            )
        )

        if not files:
            ctk.CTkLabel(
                self._body,
                text="Imported exports appear here until they are synced.",
                anchor="w", font=ctk.CTkFont(size=12),
                text_color=gui_theme.ON_SURFACE_VARIANT,
            ).pack(fill="x", pady=4)
            self._update_buttons()
            return

        for f in files:
            _, display = extract_chat_info(f.name)
            row = ctk.CTkFrame(self._body, fg_color="transparent")
            row.pack(fill="x", pady=2)

            var = ctk.BooleanVar(value=False)
            self._vars[f.name] = var
            ctk.CTkCheckBox(
                row, text="", width=24, variable=var,
                command=self._update_buttons,
            ).pack(side="left")

            names = ctk.CTkFrame(row, fg_color="transparent")
            names.pack(side="left", fill="x", expand=True)
            _tooltip(
                ctk.CTkLabel(
                    names, text=display, anchor="w", font=ctk.CTkFont(size=12),
                ),
                f.name,
            ).pack(fill="x")
            ctk.CTkLabel(
                names, text=_human_size(self._size_of(f)), anchor="w",
                font=ctk.CTkFont(size=10), text_color=gui_theme.ON_SURFACE_VARIANT,
            ).pack(fill="x")

            ctk.CTkButton(
                row, text="Preview", width=70, height=26,
                fg_color="transparent", border_width=0,
                hover_color=gui_theme.NEUTRAL_HOVER,
                text_color=gui_theme.ON_SURFACE_VARIANT,
                font=ctk.CTkFont(size=11),
                command=lambda n=f.name: self._on_preview(n),
            ).pack(side="right")

        self._update_buttons()

    @staticmethod
    def _size_of(path: Path) -> int:
        try:
            return path.stat().st_size
        except Exception:
            return 0

    def _update_buttons(self) -> None:
        n = len(self._checked())
        self._remove_btn.configure(
            state="normal" if n else "disabled",
            text=f"Remove {n} from queue" if n else "Remove from queue",
        )
        self._select_btn.configure(
            text="Clear" if self._vars and n == len(self._vars) else "Select all",
            state="normal" if self._vars else "disabled",
        )

    # -- Actions -------------------------------------------------------

    def _on_select_all(self) -> None:
        want = not (self._vars and len(self._checked()) == len(self._vars))
        for var in self._vars.values():
            var.set(want)
        self._update_buttons()

    def _on_preview(self, filename: str) -> None:
        try:
            text = format_preview(preview_export(str(INBOX_DIR / filename)))
        except Exception as exc:
            text = f"This file could not be read: {exc}"
        self._preview_label.configure(text=text)

    def _on_remove(self) -> None:
        # Snapshotted before the first removal: _render() rebuilds self._vars,
        # and iterating it while it is being replaced would drop every other
        # file.
        doomed = list(self._checked())
        failed = []
        for name in doomed:
            if not remove_from_inbox(name).get("ok"):
                failed.append(name)
        self._preview_label.configure(
            text=(
                "Could not remove: " + ", ".join(failed) if failed
                else f"Removed {len(doomed)} file{'s' if len(doomed) != 1 else ''} from the queue."
            )
        )
        self._app._refresh_inbox_count()
        self._render()

    def _on_add_more(self) -> None:
        # Replaces this panel rather than stacking on it: the import screen is
        # a sibling of this one, not a step inside it, and Back from a stack
        # two deep lands somewhere nobody asked to be.
        self._app._pop_panel()
        self._app._push_panel(_ImportPickerPanel)


class _SyncLogPanel(_Panel):
    """Ninety days of sync runs, and a way into any one of them.

    Until now the only history Windows had was the footer's live textbox: 200
    lines, in memory, gone the moment the app closed. Anyone asking "did last
    Tuesday's sync actually go?" had no way to find out. Android has had this
    screen (SyncLogScreen) since the state DB grew sync_runs; this is the same
    data, from the same shared query -- state.get_recent_runs() -- with the
    same 90-day window, so neither front-end can quietly show a different
    history than the other.

    Two things make it readable rather than merely complete:

    * Routine no-op runs fold away. A watched folder produces one row per chat
      per tick whether or not anything moved, and the runs worth finding are
      the ones that uploaded something or failed. Consecutive uneventful runs
      collapse *in place* -- a counted row you can unfold -- rather than being
      dropped or floated to the bottom, so the chronology stays honest.
    * The filter is [All] / [Errors] with live counts on the chips, so
      "were there any failures?" is answered before you press anything.

    The two are deliberately orthogonal: under Errors nothing is uneventful by
    definition (a failed run is never folded), so the fold simply has nothing
    to do rather than needing to be reasoned about.
    """

    _FILTERS = ("all", "errors")
    _FILTER_LABELS = {"all": "All", "errors": "Errors"}

    def __init__(self, app: "App", master) -> None:
        super().__init__(app, master, "Sync log", "Back to sync")

        self._filter = "all"
        # Group index -> True once the user has unfolded that run of no-op
        # runs. Keyed by the run_id the group starts at rather than by
        # position, so a refresh that inserts newer runs above doesn't reopen
        # or re-fold an unrelated group.
        self._expanded: set = set()

        try:
            self._runs = [dict(r) for r in get_recent_runs(90, STATE_DB_PATH)]
        except Exception:
            self._runs = []

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(12, 6))

        ctk.CTkLabel(
            head, text="Last 90 days", anchor="w",
            font=ctk.CTkFont(size=11), text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(side="left")

        self._chip_row = ctk.CTkFrame(self, fg_color="transparent")
        self._chip_row.pack(fill="x", padx=20, pady=(0, 8))
        self._chips: dict = {}
        for key in self._FILTERS:
            btn = ctk.CTkButton(
                self._chip_row, text=self._FILTER_LABELS[key], width=96, height=28,
                font=ctk.CTkFont(size=11),
                command=lambda k=key: self._set_filter(k),
            )
            btn.pack(side="left", padx=(0, 6))
            self._chips[key] = btn

        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._body.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        self._render()

    # ── Filtering ──────────────────────────────────────────────────────

    def _visible_runs(self) -> list:
        if self._filter == "errors":
            return [r for r in self._runs if r.get("status") == "failed"]
        return list(self._runs)

    def _set_filter(self, key: str) -> None:
        if key == self._filter:
            return
        self._filter = key
        self._render()

    def _sync_chips(self) -> None:
        """Put the live count in each chip and mark the selected one.

        Counts come from the whole 90 days, not from what is currently shown:
        a chip that read "Errors (0)" only because Errors was already selected
        would be answering its own question.
        """
        counts = {
            "all": len(self._runs),
            "errors": sum(1 for r in self._runs if r.get("status") == "failed"),
        }
        for key, btn in self._chips.items():
            selected = key == self._filter
            btn.configure(
                text=f"{self._FILTER_LABELS[key]}  ({counts[key]})",
                fg_color=gui_theme.PRIMARY if selected else "transparent",
                text_color=gui_theme.ON_PRIMARY if selected else gui_theme.ON_SURFACE,
                border_width=0 if selected else 1,
                border_color=gui_theme.OUTLINE,
                hover_color=gui_theme.PRIMARY_HOVER if selected else gui_theme.SURFACE_CONTAINER_HIGH,
            )

    # ── Rendering ──────────────────────────────────────────────────────

    def _render(self) -> None:
        for w in self._body.winfo_children():
            w.destroy()
        self._sync_chips()

        runs = self._visible_runs()
        if not runs:
            self._render_empty()
            return

        # Walk the list in order, folding each *consecutive* stretch of
        # uneventful runs. In place, because the alternative -- a global
        # "hide no-ops" switch -- silently changes what "the run above this
        # one" means, which is exactly the reading the log exists to support.
        i = 0
        while i < len(runs):
            if not is_uneventful_run(runs[i]):
                self._render_run_row(runs[i])
                i += 1
                continue
            j = i
            while j < len(runs) and is_uneventful_run(runs[j]):
                j += 1
            group = runs[i:j]
            key = group[0].get("run_id")
            if key in self._expanded:
                for run in group:
                    self._render_run_row(run, muted=True)
                self._render_fold_row(key, len(group), expanded=True)
            else:
                self._render_fold_row(key, len(group), expanded=False)
            i = j

    def _render_empty(self) -> None:
        wrap = ctk.CTkFrame(self._body, fg_color="transparent")
        wrap.pack(fill="x", padx=8, pady=24)
        if self._filter == "errors":
            title, detail = (
                "No failed runs in the last 90 days.",
                "Everything that ran, finished. Switch to All to see the full history.",
            )
        else:
            title, detail = (
                "No syncs in the last 90 days.",
                "Every sync run lands here — what was uploaded, when, and anything "
                "that failed. Nothing has run yet.",
            )
        ctk.CTkLabel(
            wrap, text=title, anchor="w", justify="left",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(fill="x")
        ctk.CTkLabel(
            wrap, text=detail, anchor="w", justify="left", wraplength=460,
            text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(fill="x", pady=(6, 0))

    def _render_fold_row(self, key, count: int, *, expanded: bool) -> None:
        """The counted stand-in for a stretch of runs that changed nothing."""
        label = (
            f"Hide {count} run{'s' if count != 1 else ''} with nothing new"
            if expanded else
            f"{count} run{'s' if count != 1 else ''} with nothing new  —  Show"
        )
        ctk.CTkButton(
            self._body, text=label, height=26, anchor="w",
            font=ctk.CTkFont(size=11),
            fg_color="transparent", border_width=0,
            text_color=gui_theme.ON_SURFACE_VARIANT,
            hover_color=gui_theme.SURFACE_CONTAINER_HIGH,
            command=lambda k=key: self._toggle_group(k),
        ).pack(fill="x", padx=6, pady=1)

    def _toggle_group(self, key) -> None:
        if key in self._expanded:
            self._expanded.discard(key)
        else:
            self._expanded.add(key)
        self._render()

    def _render_run_row(self, run: dict, *, muted: bool = False) -> None:
        """One run: dot + chat + when on the first line, counts + how on the
        second.

        This replaces a single "complete · Manual · 12 synced, 3 skipped · 3
        Aug" string. Four unrelated facts separated by middots read as one
        sentence and scan as none of them -- the status is a colour, the time
        belongs on the right edge where the eye goes to compare rows, and the
        counts are the only part worth reading in full.
        """
        status = run.get("status")
        color = gui_theme.STATUS_COLOR.get(
            status if status in gui_theme.STATUS_COLOR else None
        )
        frame = ctk.CTkFrame(self._body, corner_radius=6)
        frame.pack(fill="x", padx=4, pady=2)

        # The whole row opens the run, not just a link inside it: a 26px
        # target inside a 44px row is the kind of thing that only works for
        # whoever built it. Bound on the children too -- a click lands on
        # whichever label is under the cursor, not on the frame.
        def open_run(_event=None, r=run):
            self._app._push_panel(lambda app, master, rr=r: _SyncRunPanel(app, master, rr))

        top = ctk.CTkFrame(frame, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(6, 0))

        ctk.CTkLabel(
            top, text="●", text_color=color, width=16,
            font=ctk.CTkFont(size=12),
        ).pack(side="left")

        ctk.CTkLabel(
            top, text=run.get("display_name") or "Unknown chat", anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=gui_theme.ON_SURFACE_VARIANT if muted else gui_theme.ON_SURFACE,
        ).pack(side="left", fill="x", expand=True, padx=(4, 8))

        ctk.CTkLabel(
            top, text=_format_run_time(run.get("started_at")), anchor="e",
            font=ctk.CTkFont(size=11), text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(side="right")

        bot = ctk.CTkFrame(frame, fg_color="transparent")
        bot.pack(fill="x", padx=(28, 8), pady=(0, 6))

        ctk.CTkLabel(
            bot, text=_run_counts_text(run), anchor="w",
            font=ctk.CTkFont(size=11), text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(side="left")

        ctk.CTkLabel(
            bot, text=_TRIGGER_LABELS.get(run.get("trigger"), run.get("trigger") or ""),
            anchor="e", font=ctk.CTkFont(size=11),
            text_color=gui_theme.ON_SURFACE_VARIANT,
        ).pack(side="right")

        if status == "failed" and run.get("error_message"):
            ctk.CTkLabel(
                frame, text=_first_line(run["error_message"]), anchor="w",
                justify="left", wraplength=440,
                font=ctk.CTkFont(size=11), text_color=gui_theme.ERROR,
            ).pack(fill="x", padx=(28, 8), pady=(0, 6))

        for widget in (frame, top, bot, *top.winfo_children(), *bot.winfo_children()):
            widget.bind("<Button-1>", open_run)


class _SyncRunPanel(_Panel):
    """One run, in full: what it touched, when, and why it stopped.

    The list row has room for four facts; a run has a dozen, and the ones that
    matter when something went wrong -- how long it took, how many messages
    were parsed versus actually uploaded, the whole error rather than its
    first line -- are exactly the ones a single row cannot carry. Nothing here
    is fetched: get_recent_runs() already returns every sync_runs column, so
    this is the same row the list drew, shown at length.
    """

    def __init__(self, app: "App", master, run: dict) -> None:
        super().__init__(app, master, run.get("display_name") or "Sync run", "Back to sync log")
        self._run = run

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(12, 12))

        status = run.get("status") or "pending"
        color = gui_theme.STATUS_COLOR.get(
            status if status in gui_theme.STATUS_COLOR else None
        )

        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(
            head, text="●", text_color=color, width=18, font=ctk.CTkFont(size=14),
        ).pack(side="left")
        ctk.CTkLabel(
            head, text=_STATUS_HEADLINE.get(status, status), anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left", padx=(4, 0))

        if status == "failed" and run.get("error_message"):
            box = ctk.CTkFrame(body, corner_radius=6, fg_color=gui_theme.ERROR_CONTAINER)
            box.pack(fill="x", pady=(0, 12))
            ctk.CTkLabel(
                box, text=run["error_message"], anchor="w", justify="left",
                wraplength=430, text_color=gui_theme.ON_ERROR_CONTAINER,
                font=ctk.CTkFont(size=11),
            ).pack(fill="x", padx=10, pady=8)

        self._section_rule(body, "Messages")
        self._field(body, "Parsed from the export", f"{run.get('messages_parsed') or 0}")
        self._field(body, "Uploaded to your mailbox", f"{run.get('messages_synced') or 0}")
        # Named rather than left as a bare "skipped" count: skipped means
        # already in the mailbox from an earlier run, and read cold a skipped
        # count looks like something went missing.
        self._field(
            body, "Already there, so skipped", f"{run.get('messages_skipped') or 0}",
        )

        self._section_rule(body, "Timing")
        self._field(body, "Started", _format_run_time(run.get("started_at"), long=True))
        self._field(
            body, "Finished",
            _format_run_time(run.get("completed_at"), long=True)
            if run.get("completed_at") else "—  (did not finish)",
        )
        duration = _run_duration(run)
        if duration:
            self._field(body, "Took", duration)

        self._section_rule(body, "Run")
        self._field(body, "Chat", run.get("display_name") or "Unknown chat")
        self._field(
            body, "Started by",
            _TRIGGER_LABELS.get(run.get("trigger"), run.get("trigger") or "—"),
        )
        if run.get("last_synced_ts"):
            self._field(body, "Newest message synced", str(run["last_synced_ts"]))
        self._field(body, "Run number", str(run.get("run_id") or "—"))


class _ChatDetailPanel(_Panel):
    """One chat, in full: what has been archived for it, and what can be done.

    Android has had this screen since chats got their own route
    (ChatDetailScreen); Windows had three unlabelled 22x20 glyphs on the list
    row and nothing else -- no place to see when a chat last synced, whether a
    mail thread exists for it, or how many messages went out, and no way at all
    to sync a single chat. The facts, the four actions and the wording of the
    reset gate are the same ones Android shows, because the chat is the same
    chat and the consequences are the same consequences.

    The one deliberate difference is where the destructive gate appears. On
    Android it is an AlertDialog; here it renders inside the panel, because
    stacking a modal over an in-window screen is exactly the pop-up habit these
    panels replaced. The questions asked, their order and their text are
    unchanged -- and the steps come from src.config's mailbox_clear_steps, so
    this panel, the row's glyph and the CLI cannot drift into giving different
    instructions for the same irreversible action.
    """

    def __init__(self, app: "App", master, row: dict) -> None:
        super().__init__(app, master, row.get("display_name") or "Chat", "Back to sync")
        self._row = dict(row)
        self._chat_id = row["chat_id"]
        self._display_name = row.get("display_name") or self._chat_id
        self._source_filename = row.get("source_filename", "")

        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._body.pack(fill="both", expand=True, padx=18, pady=(12, 12))

        # What a gate, if any, is currently asking. 0 = nothing pending; 1 and
        # 2 are the reset gates (the instruction, then the commitment);
        # "delete" is the removal confirm. One at a time by construction:
        # opening any gate replaces whatever was open.
        self._gate = 0
        self._archived = 0
        self._folder = ""
        self._message = ""

        self._render()

    # ── Rendering ──────────────────────────────────────────────────────

    def _render(self) -> None:
        for w in self._body.winfo_children():
            w.destroy()

        row = self._row
        status = _chat_status_of(row)
        color = _STATUS_COLOR.get(row.get("last_run_status"), _STATUS_COLOR[None])

        head = ctk.CTkFrame(self._body, fg_color="transparent")
        head.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            head, text="●", text_color=color, width=18, font=ctk.CTkFont(size=14),
        ).pack(side="left")
        ctk.CTkLabel(
            head, text=_CHAT_STATUS_HEADLINE.get(status, status), anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left", padx=(4, 0))

        self._section_rule(self._body, "This chat")
        self._field(self._body, "Messages synced", str(row.get("messages_synced") or 0))
        self._field(
            self._body, "Last sync",
            _format_run_time(row.get("last_run_at"), long=True)
            if row.get("last_run_at") else "Never",
        )
        # Not "has a Gmail thread": under IMAP the stored id is the RFC 822
        # Message-ID we generated, which is what threads the archive there.
        self._field(
            self._body, "Mail thread exists",
            "Yes" if row.get("gmail_thread_id") else "No",
        )
        if self._source_filename:
            self._field(self._body, "Export file", self._source_filename)

        self._section_rule(self._body, "Actions")
        if self._gate:
            self._render_gate()
        else:
            self._render_actions()

        if self._message:
            ctk.CTkLabel(
                self._body, text=self._message, anchor="w", justify="left",
                wraplength=430, font=ctk.CTkFont(size=11),
                text_color=gui_theme.ON_SURFACE_VARIANT,
            ).pack(fill="x", pady=(12, 0))

    def _action(self, text: str, command, *, danger: bool = False, enabled: bool = True):
        """Full-width and labelled, in the order Android lists them."""
        btn = ctk.CTkButton(
            self._body, text=text, height=34, anchor="w",
            font=ctk.CTkFont(size=12),
            fg_color="transparent", border_width=1,
            border_color=gui_theme.ERROR if danger else gui_theme.OUTLINE,
            text_color=gui_theme.ERROR if danger else gui_theme.ON_SURFACE,
            hover_color=gui_theme.ERROR_CONTAINER if danger else gui_theme.SURFACE_CONTAINER_HIGH,
            command=command,
        )
        btn.pack(fill="x", pady=3)
        if not enabled:
            btn.configure(state="disabled")
        return btn

    def _render_actions(self) -> None:
        # No "Open in Gmail" action. Under IMAP the stored gmail_thread_id is
        # the RFC 822 Message-ID this app generated (that is what IMAP threads
        # on), not a Gmail thread id, so the mail.google.com/#all/<id> deep
        # link would point at a thread Gmail has never heard of -- and at
        # Gmail at all for someone archiving to Outlook or Fastmail. There is
        # no cross-provider equivalent, so the action is gone rather than
        # disabled.
        syncing = self._app._worker is not None
        self._action(
            "Current sync is on" if syncing else "Sync just this chat",
            self._on_sync_this_chat,
            enabled=not syncing,
        )

        # Not "re-sync from scratch": this syncs nothing. It clears the record
        # so that a *later* sync starts over.
        self._action("Reset (forget sync history)", self._open_reset_gate, danger=True)
        self._action("Delete from list", self._open_delete_gate, danger=True)

    # ── Gates ──────────────────────────────────────────────────────────

    def _gate_box(self, title: str, body_text: str) -> None:
        box = ctk.CTkFrame(self._body, corner_radius=6, fg_color=gui_theme.ERROR_CONTAINER)
        box.pack(fill="x", pady=(2, 6))
        ctk.CTkLabel(
            box, text=title, anchor="w", justify="left",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=gui_theme.ON_ERROR_CONTAINER,
        ).pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            box, text=body_text, anchor="w", justify="left", wraplength=410,
            font=ctk.CTkFont(size=11), text_color=gui_theme.ON_ERROR_CONTAINER,
        ).pack(fill="x", padx=12, pady=(0, 10))

    def _gate_buttons(self, confirm_text: str, confirm_command) -> None:
        row = ctk.CTkFrame(self._body, fg_color="transparent")
        row.pack(fill="x", pady=(2, 0))
        # Cancel first and on the left, where the eye lands: the confirming
        # button is the one that cannot be undone, so it is neither the default
        # nor where the pointer already is.
        ctk.CTkButton(
            row, text="Cancel", width=90, height=32,
            fg_color="transparent", border_width=1,
            text_color=gui_theme.ON_SURFACE,
            command=self._cancel_gate,
        ).pack(side="left")
        ctk.CTkButton(
            row, text=confirm_text, height=32,
            fg_color=gui_theme.ERROR, hover_color=gui_theme.ERROR_HOVER,
            command=confirm_command,
        ).pack(side="left", padx=(8, 0))

    def _cancel_gate(self) -> None:
        self._gate = 0
        self._render()

    def _render_gate(self) -> None:
        if self._gate == "delete":
            # Android's words for the same question (ChatDetailScreen's delete
            # dialog), because it is the same question with the same
            # consequence -- including the second paragraph, which is the part
            # that actually distinguishes this from Reset.
            self._gate_box(
                "Delete this chat?",
                f"This removes '{self._display_name}' from your list entirely — "
                "unlike Reset, it won't be kept for re-syncing. Mail already in "
                "your mailbox is not deleted.\n\n"
                "It also forgets which messages were already archived, so if you "
                "ever import this export again you will get a second copy of all "
                "of them unless you delete the old mail first.",
            )
            self._gate_buttons("Delete", self._confirm_delete)
            return

        archived, folder = self._archived, self._folder
        noun = "message" if archived == 1 else "messages"

        if archived == 0:
            self._gate_box(
                "Reset this chat?",
                f"Reset sync history for '{self._display_name}'?\n\n"
                "Nothing has been archived for this chat yet, so no duplicate "
                "mail can result.\n\n"
                "A new mail thread is created the next time this chat is synced.",
            )
            self._gate_buttons("Reset", self._confirm_reset)
            return

        if self._gate == 1:
            steps = mailbox_clear_steps(folder, is_gmail_mailbox(self._app._settings))
            numbered = "\n".join(f"  {i}. {s}" for i, s in enumerate(steps, 1))
            self._gate_box(
                "Delete the old mail first",
                f"'{self._display_name}' already has {archived} {noun} archived "
                f"in your mailbox, in:\n\n    {folder}\n\n"
                "Resetting makes the app forget it sent them, so the next sync "
                "files a second copy. This app can never delete mail - only "
                "you can.\n\n"
                f"{numbered}\n\n"
                "Have you already deleted that mail?",
            )
            self._gate_buttons("Yes, I deleted it", self._advance_reset_gate)
            return

        # Gate 2 -- the commitment. It does NOT claim an immediate re-archive:
        # reset only clears local state and moves the export back to the inbox.
        self._gate_box(
            "Confirm reset",
            f"You've said this folder is now empty:\n\n    {folder}\n\n"
            "Resetting clears the app's record of this chat. No mail is sent "
            f"now - the next sync re-archives all {archived} {noun} into a "
            "fresh thread.\n\n"
            "If any of the old mail is still there, that sync gives you a "
            "second copy of it, and only you can clean it up.",
        )
        self._gate_buttons("Reset", self._confirm_reset)

    def _open_reset_gate(self) -> None:
        """Ask how much mail is already out there before asking anything else,
        so the gate can name a real count and the real folder."""
        try:
            self._archived = count_archived_messages(self._chat_id, STATE_DB_PATH)
        except Exception:
            self._archived = 0
        self._folder = mailbox_folder_for(self._display_name)
        self._gate = 1
        self._message = ""
        self._render()

    def _advance_reset_gate(self) -> None:
        self._gate = 2
        self._render()

    def _open_delete_gate(self) -> None:
        # A chat that has never synced has no record whose loss is worth
        # confirming -- the same exemption _on_delete_chat makes.
        if self._row.get("last_run_status") is None:
            self._confirm_delete()
            return
        self._gate = "delete"
        self._message = ""
        self._render()

    # ── Doing the thing ────────────────────────────────────────────────

    def _confirm_delete(self) -> None:
        self._app._apply_chat_delete(self._chat_id, self._display_name)
        # The chat this panel is about no longer exists, so there is nothing
        # left here to show -- Android's onDeleted navigates back for the same
        # reason.
        self._close()

    def _confirm_reset(self) -> None:
        ok = self._app._apply_chat_reset(
            self._chat_id, self._display_name, self._source_filename
        )
        self._gate = 0
        self._message = (
            "Reset complete. The next sync re-archives this chat from the "
            "start — the sync log has the detail."
            if ok else
            "Reset did not go through. The sync log says why."
        )
        self._refresh_row()
        self._render()

    def _on_sync_this_chat(self) -> None:
        """Run a sync narrowed to this chat, then go back to watch it.

        Back rather than staying put: the progress bar and the log live on the
        sync view, and a panel sitting over them would hide the very thing the
        button just started -- the same reason Android leaves the chat screen
        for the progress screen.
        """
        self._close()
        self._app._begin_sync(
            dry_run=self._app._dry_run_var.get(),
            chunk_size=self._app._chunk_var.get(),
            chat_filter=self._chat_id,
        )

    def _refresh_row(self) -> None:
        """Re-read this chat's row, so the facts above say what is true now
        rather than what was true when the panel opened."""
        try:
            for row in get_sync_summary(STATE_DB_PATH):
                if row["chat_id"] == self._chat_id:
                    self._row = dict(row)
                    return
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
