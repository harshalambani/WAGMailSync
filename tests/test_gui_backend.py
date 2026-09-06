"""Tests for Road B phase 2 -- mail-backend selection (gui.py / gui_worker.py).

Covers, since the v2.0.0 Google sign-in strip:
  - mail_backend defaults to imap on a fresh install, a saved value round-trips,
    and a settings file left saying "gmail_oauth" resolves to imap rather than
    to a backend nothing can build.
  - check_auth_status(), including "no credentials stored".
  - Backend selection builds the IMAP transport from the saved credentials.
  - Credentials file: written with the expected shape, and the app password
    never appears in any log record or exception string produced by a
    failed connect.

...plus the one-time "Google sign-in has been removed" notice: shown once to
someone who actually had it, flag persists, never shown to anyone else.

gui.py and gui_worker.py each resolve their own module-level _SETTINGS_FILE
as Path(__file__).parent / "data" / ".settings.json" (deliberately NOT
routed through src.config.set_root() -- see gui_worker.py's docstring), so
these tests monkeypatch that constant directly on each module rather than
using the tmp_root fixture from conftest.py.
"""

import json
import types
import queue

import pytest

import gui
import gui_worker
import src.config
from src import secret_store
from src.config import (
    IMAP_PROVIDERS,
    LEGACY_MAIL_BACKEND_GMAIL_OAUTH,
    MAIL_BACKEND_IMAP,
)
from src.progress import ProgressTracker


# ---------------------------------------------------------------------------
# gui.py settings round-trip + one-time backend notice
# ---------------------------------------------------------------------------

@pytest.fixture
def token_file(tmp_path, monkeypatch):
    """Control whether the OAuth era's leftover token.json 'exists'.

    Points src.config.LEGACY_TOKEN_FILE at a path under tmp_path that is absent
    until a test creates it -- otherwise is_legacy_oauth_user() would see the
    developer's real auth/token.json and the result would depend on whose
    machine the suite runs on.
    """
    path = tmp_path / "auth" / "token.json"
    monkeypatch.setattr(src.config, "LEGACY_TOKEN_FILE", path)
    return path


@pytest.fixture
def settings_file(tmp_path, monkeypatch, token_file):
    path = tmp_path / "data" / ".settings.json"
    monkeypatch.setattr(gui, "_SETTINGS_FILE", path)
    # Same isolation as token_file, and for the same reason on the IMAP side:
    # _render_account_summary() and _signout_state() both ask whether the saved
    # app-password file exists, so without this the answer came from the
    # developer's real auth/imap_credentials.json. Not hypothetical --
    # test_settings_shows_the_account_summary_not_the_form began failing the
    # moment a real IMAP account was connected on the machine running the
    # suite, while CI (which has no credentials at all) stayed green.
    #
    # Both modules' copies of the constant, not just gui's. _render_account_summary
    # now asks gui_worker.check_imap_auth_status() instead of answering from a
    # file check of its own (v1.9.2), so patching only gui's copy left the real
    # question -- "is there a usable credential?" -- being answered against the
    # developer's real auth\ folder while everything else in the test was
    # isolated. That is the same trap this comment already described once; it
    # moves whenever the judgement moves, so isolate the path in every module
    # that owns one.
    imap_credentials = tmp_path / "auth" / "imap_credentials.json"
    monkeypatch.setattr(gui, "IMAP_CREDENTIALS_FILE", imap_credentials)
    monkeypatch.setattr(gui_worker, "IMAP_CREDENTIALS_FILE", imap_credentials)
    return path


def test_mail_backend_defaults_to_imap_when_no_file(settings_file):
    settings = gui._load_settings()
    assert settings["mail_backend"] == MAIL_BACKEND_IMAP == "imap"
    assert settings["backend_notice_shown"] is False


def test_settings_file_without_key_takes_new_default_when_no_token(settings_file):
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps({"chunk_size": "hour"}))
    settings = gui._load_settings()
    assert settings["mail_backend"] == MAIL_BACKEND_IMAP
    assert settings["chunk_size"] == "hour"


def test_a_saved_gmail_oauth_backend_loads_as_imap(settings_file, token_file):
    """The reversal of the old upgrade guard.

    Before v2.0.0 a leftover token pinned the user back to gmail_oauth so they
    were never silently migrated. Nothing can build that transport now, so the
    loader must hand back imap -- the explanation comes from the one-time
    notice instead (see _should_show_oauth_removed_notice below).
    """
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(
        json.dumps({"mail_backend": LEGACY_MAIL_BACKEND_GMAIL_OAUTH})
    )
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("{}")

    assert gui._load_settings()["mail_backend"] == MAIL_BACKEND_IMAP


def test_gui_and_worker_agree_on_backend_for_same_file(
    settings_file, token_file, monkeypatch
):
    """The whole point of the shared resolver: no drift between the two."""
    monkeypatch.setattr(gui_worker, "_SETTINGS_FILE", settings_file)
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps({"chunk_size": "hour"}))

    for token_present in (False, True):
        if token_present:
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text("{}")
        assert (
            gui._load_settings()["mail_backend"]
            == gui_worker._load_mail_backend_settings()["mail_backend"]
        )


def test_saved_mail_backend_value_round_trips(settings_file):
    settings = gui._load_settings()
    settings["mail_backend"] = MAIL_BACKEND_IMAP
    settings["imap_email"] = "me@example.com"
    gui._save_settings(settings)

    reloaded = gui._load_settings()
    assert reloaded["mail_backend"] == MAIL_BACKEND_IMAP
    assert reloaded["imap_email"] == "me@example.com"


def test_oauth_removed_notice_shown_once_flag_persists_not_shown_again(settings_file):
    settings = gui._load_settings()

    # Someone who actually had Google sign-in -> owed one explanation.
    assert gui._should_show_oauth_removed_notice(settings, True) is True

    # Simulate the app marking it shown and persisting that.
    settings["oauth_removed_notice_shown"] = True
    gui._save_settings(settings)

    reloaded = gui._load_settings()
    assert reloaded["oauth_removed_notice_shown"] is True
    assert gui._should_show_oauth_removed_notice(reloaded, True) is False


def test_oauth_removed_notice_not_shown_to_someone_who_never_had_it(settings_file):
    """Every fresh install would otherwise be told about a feature it never
    saw, which is noise, not an explanation."""
    settings = gui._load_settings()
    assert gui._should_show_oauth_removed_notice(settings, False) is False


def test_legacy_oauth_evidence_reads_the_leftover_token(settings_file, token_file):
    assert gui._legacy_oauth_evidence() is False
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("{}", encoding="utf-8")
    assert gui._legacy_oauth_evidence() is True


# ---------------------------------------------------------------------------
# gui_worker.py: check_auth_status() per backend
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_paths(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auth"
    data_dir = tmp_path / "data"
    auth_dir.mkdir()
    data_dir.mkdir()
    imap_credentials_file = auth_dir / "imap_credentials.json"
    settings_file = data_dir / ".settings.json"

    monkeypatch.setattr(gui_worker, "IMAP_CREDENTIALS_FILE", imap_credentials_file)
    monkeypatch.setattr(gui_worker, "_SETTINGS_FILE", settings_file)

    return {
        "imap_credentials": imap_credentials_file,
        "settings": settings_file,
    }


def _write_settings(settings_file, **overrides):
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(overrides))


def test_check_auth_status_imap_no_credentials_stored(worker_paths):
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    valid, text = gui_worker.check_auth_status()
    assert valid is False
    assert text == "Not connected"


def test_check_auth_status_imap_with_saved_credentials(worker_paths):
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    worker_paths["imap_credentials"].write_text(json.dumps({
        "host": "imap.gmail.com", "port": 993,
        "email": "me@example.com", "password": "hunter2-app-pw",
    }))
    valid, text = gui_worker.check_auth_status()
    assert valid is True
    assert text == "Connected (me@example.com)"
    assert "hunter2-app-pw" not in text


def test_check_auth_status_imap_corrupt_credentials_file_no_password_leak(worker_paths):
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    # Deliberately corrupt/undecodable JSON so the except branch fires.
    worker_paths["imap_credentials"].write_text("not json {password: hunter2-app-pw")
    valid, text = gui_worker.check_auth_status()
    assert valid is False
    assert "hunter2-app-pw" not in text


# ---------------------------------------------------------------------------
# gui_worker.py: build_transport_for_active_backend()
# ---------------------------------------------------------------------------

def test_build_transport_imap_missing_credentials_raises_runtimeerror(worker_paths):
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    with pytest.raises(RuntimeError, match="No saved app password"):
        gui_worker.build_transport_for_active_backend()


def test_build_transport_imap_uses_saved_credentials(worker_paths, monkeypatch):
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    worker_paths["imap_credentials"].write_text(json.dumps({
        "host": "imap.gmail.com", "port": 993,
        "email": "me@example.com", "password": "hunter2-app-pw",
    }))

    captured = {}

    def fake_build_imap_transport(host, port, email, password):
        captured.update(host=host, port=port, email=email, password=password)
        return "FAKE_TRANSPORT"

    monkeypatch.setattr(gui_worker, "build_imap_transport", fake_build_imap_transport)
    transport = gui_worker.build_transport_for_active_backend()
    assert transport == "FAKE_TRANSPORT"
    assert captured == {
        "host": "imap.gmail.com", "port": 993,
        "email": "me@example.com", "password": "hunter2-app-pw",
    }


# ---------------------------------------------------------------------------
# gui_worker.py: connect_imap() event contract
# ---------------------------------------------------------------------------

class _FakeSucceedingImapTransport:
    def labels_list(self):
        return []


def test_connect_imap_success_persists_credentials_and_posts_transport(worker_paths, monkeypatch):
    fake_transport = _FakeSucceedingImapTransport()
    monkeypatch.setattr(gui_worker, "build_imap_transport", lambda h, p, e, pw: fake_transport)

    q: queue.Queue = queue.Queue()
    password = "hunter2-app-pw"
    gui_worker.connect_imap(q, "imap.gmail.com", 993, "me@example.com", password)

    event = q.get_nowait()
    assert event["type"] == "auth_ok"
    assert event["transport"] is fake_transport

    # Credentials file shape. This test machine is Windows, so
    # _save_imap_credentials will actually go through real DPAPI encryption
    # (see test_save_imap_credentials_file_shape below for the dedicated,
    # non-conditional assertion of that) -- but assert both branches here so
    # this test stays meaningful even if DPAPI is ever unavailable in CI.
    raw = worker_paths["imap_credentials"].read_bytes()
    saved = json.loads(raw)
    assert saved["host"] == "imap.gmail.com"
    assert saved["port"] == 993
    assert saved["email"] == "me@example.com"
    if secret_store.is_available():
        assert "password_dpapi" in saved
        assert "password" not in saved
        assert password.encode() not in raw
    else:
        assert saved.get("password") == password


class _FakeLoginFailIMAP4SSL:
    """Stands in for imaplib.IMAP4_SSL: constructs fine, but .login() raises
    imaplib.IMAP4.error with the password embedded in the server-response
    text -- the same shape a real "wrong password" reply takes. Used to
    drive ImapTransport._default_connection_factory()'s real code path (the
    one that actually calls _strip_secret), rather than a hand-rolled fake
    that would only prove the test's own assumption."""

    def __init__(self, host, port):
        pass

    def login(self, email, password):
        import imaplib as _imaplib
        raise _imaplib.IMAP4.error(
            f"[AUTHENTICATIONFAILED] Invalid credentials for {email}, "
            f"password sent was {password}"
        )


def test_connect_imap_failure_never_leaks_password_and_does_not_persist(worker_paths, monkeypatch):
    """End-to-end through the real production path: connect_imap() ->
    build_imap_transport() (not mocked) -> ImapTransport with
    connection_factory=None -> _default_connection_factory() -> a real
    imaplib.IMAP4.error whose original text contains the password. Only
    imaplib.IMAP4_SSL is faked (to avoid a real network call); the
    scrubbing itself is exercised for real, proving _strip_secret actually
    runs on this path rather than trusting it does."""
    import src.mail_client as mail_client_mod

    password = "hunter2-app-pw"
    monkeypatch.setattr(mail_client_mod.imaplib, "IMAP4_SSL", _FakeLoginFailIMAP4SSL)

    q: queue.Queue = queue.Queue()
    gui_worker.connect_imap(q, "imap.gmail.com", 993, "me@example.com", password)

    event = q.get_nowait()
    assert event["type"] == "auth_error"
    assert password not in event["msg"]
    # Nothing should have been written to the credentials file on failure.
    assert not worker_paths["imap_credentials"].exists()


def test_save_imap_credentials_file_shape(worker_paths):
    """On this Windows test machine, saving goes through real DPAPI: the
    file gets password_dpapi and NO password key, and the plaintext password
    string does not appear anywhere in the raw file bytes. Written to also
    cover the plaintext-fallback shape so the assertion stays correct on a
    hypothetical non-Windows/DPAPI-unavailable CI runner."""
    password = "hunter2-app-pw"
    gui_worker._save_imap_credentials("imap.gmail.com", 993, "me@example.com", password)
    raw = worker_paths["imap_credentials"].read_bytes()
    saved = json.loads(raw)
    assert saved["host"] == "imap.gmail.com"
    assert saved["port"] == 993
    assert saved["email"] == "me@example.com"
    if secret_store.is_available():
        assert set(saved.keys()) == {"host", "port", "email", "password_dpapi"}
        assert password.encode() not in raw
    else:
        assert set(saved.keys()) == {"host", "port", "email", "password"}
        assert saved["password"] == password


def test_save_imap_credentials_falls_back_to_plaintext_when_dpapi_unavailable(worker_paths, monkeypatch):
    """secret_store.protect() failing (DPAPI unavailable, or any runtime
    error inside it) must not refuse the save -- see _save_imap_credentials's
    docstring for why that's a deliberate call. It falls back to the same
    plaintext-protected-by-ACL storage this app always used."""
    monkeypatch.setattr(gui_worker.secret_store, "is_available", lambda: False)
    monkeypatch.setattr(gui_worker.secret_store, "protect", lambda pw: None)

    gui_worker._save_imap_credentials("imap.gmail.com", 993, "me@example.com", "hunter2-app-pw")

    saved = json.loads(worker_paths["imap_credentials"].read_text())
    assert set(saved.keys()) == {"host", "port", "email", "password"}
    assert saved["password"] == "hunter2-app-pw"


# ---------------------------------------------------------------------------
# DPAPI-encrypted credentials: read path, transparent upgrade, hard failure
# ---------------------------------------------------------------------------

def test_build_transport_imap_decrypts_dpapi_password(worker_paths, monkeypatch):
    """A file saved with password_dpapi (the current shape) must round-trip
    back to the real plaintext password on the read path too."""
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    password = "hunter2-app-pw"
    gui_worker._save_imap_credentials("imap.gmail.com", 993, "me@example.com", password)
    if not secret_store.is_available():
        pytest.skip("DPAPI not available in this environment")
    assert "password_dpapi" in json.loads(worker_paths["imap_credentials"].read_text())

    captured = {}

    def fake_build_imap_transport(host, port, email, pw):
        captured.update(host=host, port=port, email=email, password=pw)
        return "FAKE_TRANSPORT"

    monkeypatch.setattr(gui_worker, "build_imap_transport", fake_build_imap_transport)
    transport = gui_worker.build_transport_for_active_backend()
    assert transport == "FAKE_TRANSPORT"
    assert captured["password"] == password


def test_build_transport_imap_reads_and_upgrades_legacy_entropy_blob(worker_paths, monkeypatch):
    """A credentials file carried across the v1.9.0 rename must still open,
    and must be re-encrypted under the current entropy while it does.

    This is the exact failure seen on the real install on 2026-08-12: Data\\
    was copied forward as PLATFORM-PARITY.md's P3 entry instructed, but
    _APP_ENTROPY had changed, so DPAPI refused the blob and the app reported
    "Connected" and "Not connected" at the same time."""
    if not secret_store.is_available():
        pytest.skip("DPAPI not available in this environment")
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    password = "written-under-the-old-name"

    # Write the file exactly as a pre-rename build would have. This uses a
    # NESTED context rather than monkeypatch.undo(): undo() is not scoped to
    # the two setattrs above, it unwinds every patch on the fixture's
    # monkeypatch -- including worker_paths' redirection of
    # IMAP_CREDENTIALS_FILE into tmp_path, which would point the rest of this
    # test at the developer's real auth/ folder.
    with monkeypatch.context() as legacy_build:
        legacy_build.setattr(
            secret_store, "_APP_ENTROPY", secret_store._LEGACY_APP_ENTROPY[0]
        )
        legacy_build.setattr(secret_store, "_LEGACY_APP_ENTROPY", ())
        gui_worker._save_imap_credentials(
            "imap.gmail.com", 993, "me@example.com", password
        )

    legacy_blob = json.loads(worker_paths["imap_credentials"].read_text())["password_dpapi"]

    captured = {}

    def fake_build_imap_transport(host, port, email, pw):
        captured.update(password=pw)
        return "FAKE_TRANSPORT"

    monkeypatch.setattr(gui_worker, "build_imap_transport", fake_build_imap_transport)
    assert gui_worker.build_transport_for_active_backend() == "FAKE_TRANSPORT"
    assert captured["password"] == password

    # Re-saved under the current entropy, so the next read needs no fallback.
    upgraded = json.loads(worker_paths["imap_credentials"].read_text())["password_dpapi"]
    assert upgraded != legacy_blob
    assert secret_store.unprotect_ex(upgraded) == (password, False)


def test_build_transport_imap_undecryptable_dpapi_raises_runtimeerror_without_leaking(worker_paths):
    """A password_dpapi blob that fails to decrypt (auth/ copied to another
    machine or another Windows user) must raise a specific RuntimeError --
    never silently fall through to an empty/garbage password -- and that
    error must not contain the blob or any password-shaped content."""
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    fake_blob = "AQAAANCMnd8BFdERjHoAwE_this_is_not_a_real_dpapi_blob=="
    worker_paths["imap_credentials"].write_text(json.dumps({
        "host": "imap.gmail.com", "port": 993,
        "email": "me@example.com", "password_dpapi": fake_blob,
    }))

    with pytest.raises(RuntimeError) as excinfo:
        gui_worker.build_transport_for_active_backend()

    msg = str(excinfo.value)
    assert fake_blob not in msg
    assert "re-enter the app password" in msg.lower() or "re-enter" in msg.lower()


def test_build_transport_imap_raises_when_file_has_neither_password_key(worker_paths):
    """A credentials file carrying neither "password" nor "password_dpapi"
    must fail loudly. Routing both readers through resolve_imap_password()
    replaced a direct data["password"] lookup that used to raise KeyError
    here; degrading to an empty password instead would reach the provider as
    an ordinary authentication rejection and point the user at their app
    password rather than at the damaged file."""
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    worker_paths["imap_credentials"].write_text(json.dumps({
        "host": "imap.gmail.com", "port": 993, "email": "me@example.com",
    }))

    with pytest.raises(RuntimeError) as excinfo:
        gui_worker.build_transport_for_active_backend()

    assert "does not contain an app password" in str(excinfo.value)


def test_build_transport_imap_upgrades_legacy_plaintext_to_dpapi_on_read(worker_paths, monkeypatch):
    """Reading a legacy plaintext-format file on a Windows box where DPAPI is
    available must opportunistically rewrite the file encrypted (transparent
    upgrade), while still returning the correct password for this read."""
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    password = "hunter2-app-pw"
    worker_paths["imap_credentials"].write_text(json.dumps({
        "host": "imap.gmail.com", "port": 993,
        "email": "me@example.com", "password": password,
    }))
    if not secret_store.is_available():
        pytest.skip("DPAPI not available in this environment")

    monkeypatch.setattr(gui_worker, "build_imap_transport", lambda h, p, e, pw: "FAKE_TRANSPORT")
    transport = gui_worker.build_transport_for_active_backend()
    assert transport == "FAKE_TRANSPORT"

    upgraded = json.loads(worker_paths["imap_credentials"].read_text())
    assert "password_dpapi" in upgraded
    assert "password" not in upgraded
    # And the upgraded file itself decrypts back to the same password.
    assert secret_store.unprotect(upgraded["password_dpapi"]) == password


def test_build_transport_imap_upgrade_failure_does_not_break_read(worker_paths, monkeypatch):
    """If the opportunistic re-save during a transparent upgrade blows up for
    any reason, the read that triggered it must still succeed with the
    password that was already proven to work -- an upgrade hiccup must never
    turn into a sync failure."""
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    password = "hunter2-app-pw"
    worker_paths["imap_credentials"].write_text(json.dumps({
        "host": "imap.gmail.com", "port": 993,
        "email": "me@example.com", "password": password,
    }))

    def boom(*a, **kw):
        raise RuntimeError("simulated upgrade failure")

    monkeypatch.setattr(gui_worker, "_save_imap_credentials", boom)
    monkeypatch.setattr(gui_worker.secret_store, "is_available", lambda: True)

    captured = {}

    def fake_build_imap_transport(host, port, email, pw):
        captured["password"] = pw
        return "FAKE_TRANSPORT"

    monkeypatch.setattr(gui_worker, "build_imap_transport", fake_build_imap_transport)
    transport = gui_worker.build_transport_for_active_backend()
    assert transport == "FAKE_TRANSPORT"
    assert captured["password"] == password


def test_check_imap_auth_status_works_for_dpapi_shape(worker_paths):
    """A real DPAPI blob written by this machine reads back as connected."""
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    if not secret_store.is_available():
        pytest.skip("Windows DPAPI (crypt32.dll) not available in this environment")
    gui_worker._save_imap_credentials(
        "imap.gmail.com", 993, "me@example.com", "hunter2-app-pw"
    )
    assert "password_dpapi" in json.loads(worker_paths["imap_credentials"].read_text())

    valid, text = gui_worker.check_auth_status()
    assert valid is True
    assert text == "Connected (me@example.com)"
    assert "hunter2-app-pw" not in text


def test_check_imap_auth_status_rejects_undecryptable_blob(worker_paths):
    """The v1.9.1 regression test, one layer earlier than the header.

    A password_dpapi this machine cannot decrypt (a copied auth\\ folder, or a
    blob orphaned by an entropy change) must not read as connected just
    because the file parses. Before v1.9.2 it did, and the green header that
    produced was contradicted by Sync a click later.
    """
    _write_settings(worker_paths["settings"], mail_backend=MAIL_BACKEND_IMAP)
    worker_paths["imap_credentials"].write_text(json.dumps({
        "host": "imap.gmail.com", "port": 993,
        "email": "me@example.com", "password_dpapi": "bm90LWEtcmVhbC1kcGFwaS1ibG9i",
    }))
    valid, text = gui_worker.check_auth_status()
    assert valid is False
    assert text == "Not connected — reconnect"
    # The status label has room for a state, not an explanation: no exception
    # text, and so no path or credential detail, may ride along in it.
    assert "@" not in text


# ---------------------------------------------------------------------------
# At-rest protection of the credentials file (F2 sub-findings)
# ---------------------------------------------------------------------------

def test_save_imap_credentials_hardens_directory_before_file_exists(worker_paths, monkeypatch):
    """The directory ACL must be applied while the file does NOT yet exist, so
    the file inherits a restricted ACL at creation. Writing first and
    hardening afterwards left a window with a live password in a
    world-readable file."""
    monkeypatch.setattr(gui_worker.os, "name", "nt")
    seen = {}

    def fake_dir_acl(path):
        seen["file_existed_at_dir_acl"] = worker_paths["imap_credentials"].exists()
        return True

    monkeypatch.setattr(gui_worker, "_restrict_auth_dir_acl", fake_dir_acl)
    monkeypatch.setattr(gui_worker, "_restrict_file_acl", lambda p: True)

    gui_worker._save_imap_credentials("imap.gmail.com", 993, "me@example.com", "pw")

    assert seen["file_existed_at_dir_acl"] is False


def test_save_imap_credentials_raises_and_deletes_when_windows_acl_fails(worker_paths, monkeypatch):
    """A failed ACL must fail loud, not silently leave a readable password."""
    monkeypatch.setattr(gui_worker.os, "name", "nt")
    monkeypatch.setattr(gui_worker, "_restrict_auth_dir_acl", lambda p: False)
    monkeypatch.setattr(gui_worker, "_restrict_file_acl", lambda p: False)

    password = "hunter2-app-pw"
    with pytest.raises(RuntimeError) as excinfo:
        gui_worker._save_imap_credentials("imap.gmail.com", 993, "me@example.com", password)

    assert not worker_paths["imap_credentials"].exists()
    assert password not in str(excinfo.value)


def test_save_imap_credentials_raises_when_posix_chmod_fails(worker_paths, monkeypatch):
    monkeypatch.setattr(gui_worker.os, "name", "posix")

    def boom(*a, **kw):
        raise OSError("chmod refused")

    monkeypatch.setattr(gui_worker.os, "chmod", boom)

    with pytest.raises(RuntimeError):
        gui_worker._save_imap_credentials("imap.gmail.com", 993, "me@example.com", "pw")
    assert not worker_paths["imap_credentials"].exists()


def test_connect_imap_surfaces_acl_failure_as_auth_error(worker_paths, monkeypatch):
    """The loud failure must reach the user through the existing queue
    contract rather than escaping as an unhandled exception."""
    monkeypatch.setattr(
        gui_worker, "build_imap_transport",
        lambda h, p, e, pw: _FakeSucceedingImapTransport(),
    )
    monkeypatch.setattr(gui_worker.os, "name", "nt")
    monkeypatch.setattr(gui_worker, "_restrict_auth_dir_acl", lambda p: False)
    monkeypatch.setattr(gui_worker, "_restrict_file_acl", lambda p: False)

    password = "hunter2-app-pw"
    q: queue.Queue = queue.Queue()
    gui_worker.connect_imap(q, "imap.gmail.com", 993, "me@example.com", password)

    event = q.get_nowait()
    assert event["type"] == "auth_error"
    assert password not in event["msg"]
    assert not worker_paths["imap_credentials"].exists()


def test_current_username_falls_back_when_username_env_missing(monkeypatch):
    """%USERNAME% is absent in some service/scheduled-task contexts; that used
    to make ACL hardening a silent no-op."""
    import src.mail_client as mail_client_mod

    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.setattr(mail_client_mod.getpass, "getuser", lambda: "fallback-user")
    assert mail_client_mod._current_username() == "fallback-user"


def test_restrict_acl_returns_false_when_username_unresolvable(tmp_path, monkeypatch):
    import src.mail_client as mail_client_mod

    monkeypatch.setattr(mail_client_mod, "_current_username", lambda: None)
    assert mail_client_mod._restrict_acl(tmp_path, "F") is False


def test_restrict_file_acl_omits_directory_inheritance_flags(tmp_path, monkeypatch):
    """(OI)(CI) are directory-only flags; icacls rejects them on a file."""
    import src.mail_client as mail_client_mod

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        class _Done:
            returncode = 0
            stdout = ""
        return _Done()

    monkeypatch.setattr(mail_client_mod, "_current_username", lambda: "someuser")
    monkeypatch.setattr(mail_client_mod.subprocess, "run", fake_run)

    assert mail_client_mod._restrict_file_acl(tmp_path / "f.json") is True
    assert "someuser:F" in captured["cmd"]
    assert not any("(OI)(CI)" in part for part in captured["cmd"])


# ---------------------------------------------------------------------------
# IMAP_PROVIDERS sanity (used by the Settings-window provider dropdown)
# ---------------------------------------------------------------------------

def test_imap_providers_all_have_label_host_port():
    assert "gmail" in IMAP_PROVIDERS
    assert "custom" in IMAP_PROVIDERS
    for key, info in IMAP_PROVIDERS.items():
        assert "label" in info and info["label"]
        assert "port" in info and info["port"] == 993
        if key == "custom":
            assert info["host"] is None
        else:
            assert info["host"]


# ---------------------------------------------------------------------------
# Settings window: provider dropdown -> host/port autofill
#
# These need a real Tk display, so they skip where one isn't available. They
# exist because the bug they guard against shipped: _apply_host_field_state
# disables the host entry after filling it, and a disabled Tk entry silently
# drops delete/insert -- so every provider change after the first no-oped and
# left the previous provider's host in the field, which _on_save then wrote.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def tk_root():
    """One Tk root for the whole session. Creating and destroying a fresh
    CTk() per test eventually leaves Tcl in a state where the next one dies
    with `invalid command name "tcl_findLibrary"` -- which surfaced as a test
    that skipped at random, moving between runs. Only the Toplevel under test
    is per-test."""
    ctk = pytest.importorskip("customtkinter")
    try:
        root = ctk.CTk()
    except Exception as exc:  # no display / no Tk
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    yield root
    root.destroy()


def _as_host(root, settings):
    """Lend the bare Tk root the three things a panel needs from App, using
    App's own implementations rather than stand-ins -- the panel stack is what
    replaced the pop-up windows, so the tests should be running the real one."""
    root._settings = settings
    root._panels = []
    root._HEADER_HEIGHT = gui.App._HEADER_HEIGHT
    root._push_panel = types.MethodType(gui.App._push_panel, root)
    root._pop_panel = types.MethodType(gui.App._pop_panel, root)
    return root


@pytest.fixture
def settings_window(settings_file, tk_root):
    root = _as_host(tk_root, {
        "mail_backend": MAIL_BACKEND_IMAP,
        "imap_provider": "gmail",
        "imap_host": IMAP_PROVIDERS["gmail"]["host"],
        "imap_port": 993,
        "imap_email": "you@example.com",
        "chunk_size": "day",
        "auto_refresh_label": "30 s",
    })
    root._push_panel(gui._SettingsPanel)
    yield root._panels[-1]
    while root._panels:
        root._pop_panel()


@pytest.fixture
def mail_account_window(settings_file, tk_root):
    """The mail account is its own screen, as it is on Android -- Settings only
    shows a summary line and a way in. Everything about the backend, the IMAP
    form and the app-password help is asserted against this screen rather than
    the Settings one."""
    root = _as_host(tk_root, {
        "mail_backend": MAIL_BACKEND_IMAP,
        "imap_provider": "gmail",
        "imap_host": IMAP_PROVIDERS["gmail"]["host"],
        "imap_port": 993,
        "imap_email": "you@example.com",
    })
    root._push_panel(gui._MailAccountPanel)
    yield root._panels[-1]
    while root._panels:
        root._pop_panel()


def _select(win, provider_key):
    win._provider_var.set(gui._PROVIDER_LABELS[provider_key])
    win._on_provider_changed()


@pytest.mark.parametrize(
    "provider_key", [k for k in IMAP_PROVIDERS if k != "custom"]
)
def test_provider_change_updates_host_even_when_field_is_disabled(
    mail_account_window, provider_key
):
    # Start on gmail (the fixture's saved provider) so every non-gmail case
    # arrives with the host entry already disabled -- the failing path.
    _select(mail_account_window, "gmail")
    _select(mail_account_window, provider_key)
    assert mail_account_window._host_entry.get() == IMAP_PROVIDERS[provider_key]["host"]
    assert mail_account_window._port_entry.get() == str(IMAP_PROVIDERS[provider_key]["port"])


def test_custom_provider_leaves_host_editable(mail_account_window):
    _select(mail_account_window, "fastmail")
    _select(mail_account_window, "custom")
    assert str(mail_account_window._host_entry.cget("state")) == "normal"


def test_the_imap_form_and_its_help_are_always_packed_in_that_order(mail_account_window):
    """There is one backend since v2.0.0, so the form no longer appears and
    disappears with a dropdown -- it is simply always there. What still
    matters is the arrangement it is built in: form under the backend line,
    help under the form."""
    win = mail_account_window
    order = [str(c) for c in win._mail_frame.pack_slaves()]
    assert win._help_expanded is False
    assert str(win._imap_frame) in order
    assert str(win._help_container) in order
    assert order.index(str(win._imap_frame)) < order.index(str(win._help_container))


def _has_save(frame):
    return any(
        getattr(g, "cget", None) and str(g.cget("text")) == "Save"
        for g in frame.winfo_children()
    )


def test_save_is_not_inside_the_scrolling_body(mail_account_window):
    """Save and Cancel are children of the window itself rather than of the
    scroll area, so no amount of expanded help text can push them out of
    reach -- the failure that got the help block moved in the first place."""
    win = mail_account_window
    win._toggle_help()

    assert any(_has_save(c) for c in win.pack_slaves()), \
        "Save row is not packed directly in the mail account window"


def test_settings_save_is_not_inside_the_scrolling_body(settings_window):
    """Same guarantee on the Settings side, which scrolls for the same reason."""
    assert any(_has_save(c) for c in settings_window.pack_slaves()), \
        "Save row is not packed directly in the settings window"


def test_the_window_does_not_resize_itself(mail_account_window):
    """Reported live: the window changed size on every backend switch, and
    again on re-picking the option already selected. It sized itself from
    winfo_width() -- the width it had just set -- so each round trip nudged it
    further. The size is fixed now and the content scrolls instead.

    The screen is no longer a Toplevel, so it has no geometry of its own to
    creep; what it must not do is push the window it now lives inside around,
    which is what this asserts."""
    win = mail_account_window
    win.update_idletasks()
    original = win.winfo_toplevel().geometry()

    # The backend switch is gone; expanding and collapsing the app-password
    # help is now the biggest thing that changes this screen's height.
    win._toggle_help()
    win.update_idletasks()
    win._toggle_help()
    win.update_idletasks()

    assert win.winfo_toplevel().geometry() == original


def test_screens_stack_in_the_window_instead_of_opening_pop_ups(settings_window):
    """The complaint was "too many pop-ups": settings over the main window,
    mail account over settings. Both are frames inside the main window now, and
    they stack the way Android's nav stack does -- so going into the mail
    account and back leaves the settings screen exactly as it was, unsaved
    edits included, rather than rebuilding it."""
    settings = settings_window
    app = settings._app
    settings._chunk_var.set("week")          # an edit that has not been saved

    settings._open_mail_account()
    assert len(app._panels) == 2
    assert isinstance(app._panels[-1], gui._MailAccountPanel)
    assert not isinstance(app._panels[-1], gui.ctk.CTkToplevel)

    app._pop_panel()
    assert app._panels == [settings]
    assert settings.winfo_exists()
    assert settings._chunk_var.get() == "week"


def test_going_back_from_settings_leaves_nothing_over_the_sync_view(settings_window):
    settings = settings_window
    app = settings._app
    settings._close()
    assert app._panels == []
    assert not settings.winfo_exists()


def test_settings_shows_the_account_summary_not_the_form(settings_window):
    """The split is the point: Settings answers "connected as whom?" in one
    line and owns none of the mail widgets. Android's SettingsScreen does
    exactly this -- a nav row with a summary, and MailAccountScreen behind it."""
    win = settings_window
    assert not hasattr(win, "_imap_frame")
    assert not hasattr(win, "_backend_var")
    # imap_email with no saved credentials file is "not connected" on both
    # platforms -- see MainActivity's mailAccountSummary.
    assert win._account_summary.cget("text") == "Not connected"


def test_settings_save_keeps_the_mail_account_keys(settings_window, monkeypatch):
    """Each window saves its own half onto a fresh copy of the whole settings
    dict, so neither can drop the other's keys on the way past."""
    win = settings_window
    saved = {}
    monkeypatch.setattr(win._app, "_apply_settings", saved.update, raising=False)
    # Leave the real destroy in place for the fixture's teardown.
    monkeypatch.setattr(win, "destroy", lambda: None)
    win._on_save()

    assert saved["imap_email"] == "you@example.com"
    assert saved["mail_backend"] == MAIL_BACKEND_IMAP
    assert saved["chunk_size"] == "day"


def test_mail_account_save_keeps_the_settings_keys(mail_account_window, monkeypatch):
    win = mail_account_window
    win._app._settings["chunk_size"] = "week"
    saved = {}
    monkeypatch.setattr(win._app, "_apply_settings", saved.update, raising=False)
    # Leave the real destroy in place for the fixture's teardown.
    monkeypatch.setattr(win, "destroy", lambda: None)
    win._on_save()

    assert saved["chunk_size"] == "week"
    assert saved["imap_email"] == "you@example.com"


@pytest.mark.parametrize("provider_key", list(IMAP_PROVIDERS))
def test_app_password_prompt_never_contains_email_or_password(
    mail_account_window, provider_key
):
    win = mail_account_window
    win._password_entry.insert(0, "hunter2-app-password")
    _select(win, provider_key)
    win._toggle_help()

    texts = []

    def walk(parent):
        for child in parent.winfo_children():
            try:
                texts.append(str(child.cget("text")))
            except Exception:
                pass
            walk(child)

    walk(win._help_frame)
    prompt = gui._build_app_password_prompt(
        provider_key, gui._PROVIDER_LABELS[provider_key], win._host_entry.get()
    )
    blob = " ".join(texts) + " " + prompt
    assert "you@example.com" not in blob
    assert "hunter2-app-password" not in blob


# ---------------------------------------------------------------------------
# Live progress
#
# The engine emits the same events to both front-ends. Android's SyncWorker
# (eventFraction / progressText) drives its bar from the "chunk" event, which
# carries the whole-sync message counts; the desktop used to ignore "chunk"
# entirely and move only on "file_done", so a one-file inbox sat at 0% for the
# whole run and jumped to 100% at the end. These pin the desktop to Android's
# reading of the same payload. No Tk needed -- the handler only touches two
# widgets, so stubs stand in for them.
# ---------------------------------------------------------------------------

class _FakeBar:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class _FakeLabel:
    def __init__(self):
        self.text = None

    def configure(self, text):
        self.text = text


class _FakeApp:
    """Just enough of App to run _handle_sync_event: the two widgets it
    paints, and the shared tracker that now decides what they say."""

    def __init__(self):
        self._progress_bar = _FakeBar()
        self._progress_label = _FakeLabel()
        self._progress = ProgressTracker()

    def handle(self, event):
        gui.App._handle_sync_event(self, event)
        return self

    def _render_progress(self):
        gui.App._render_progress(self)


def _chunk(**over):
    event = {
        "type": "chunk", "name": "Kartik Patel",
        "chunk": 2, "total_chunks": 9,
        "msgs_done": 120, "total_msgs": 540,
        "global_done": 120, "global_total": 900,
    }
    event.update(over)
    return event


def test_chunk_moves_the_bar_by_whole_sync_message_count():
    app = _FakeApp().handle(_chunk())
    assert app._progress_bar.value == pytest.approx(120 / 900)
    assert app._progress_label.text == "Syncing: Kartik Patel — 120 / 540 messages"


def test_chunk_with_nothing_to_send_does_not_divide_by_zero():
    app = _FakeApp().handle(_chunk(global_done=0, global_total=0, msgs_done=0, total_msgs=0))
    assert app._progress_bar.value is None  # left where it was, not crashed


def test_file_count_still_drives_the_bar_when_no_chunks_arrive():
    """A run that is all dedup-skips pushes nothing, so "chunk" never fires.
    Android falls back to the file count there and so does this."""
    app = _FakeApp().handle({"type": "file_done", "done": 1, "total": 2})
    assert app._progress_bar.value == pytest.approx(0.5)
    assert app._progress_label.text == "1 / 2 files"


def test_the_bar_never_goes_backwards_at_a_file_boundary():
    """Two sources drive the bar on different scales: chunk counts messages
    across the whole sync, file_done counts files. Finishing the first of three
    files is 1/3 -- but if that file was most of the work, the message count is
    already past half, so taking file_done at face value made the bar retreat.
    Whichever is further along wins; the text still reports what just
    happened."""
    app = _FakeApp().handle(_chunk(global_done=600, global_total=900))
    assert app._progress_bar.value == pytest.approx(600 / 900)

    app.handle({"type": "file_done", "done": 1, "total": 3})
    assert app._progress_bar.value == pytest.approx(600 / 900)
    assert app._progress_label.text == "1 / 3 files"

    # ...and a genuinely further-along file count still moves it.
    app.handle({"type": "file_done", "done": 3, "total": 3})
    assert app._progress_bar.value == pytest.approx(1.0)


def test_file_total_is_reported_before_the_first_file():
    assert _FakeApp().handle({"type": "files_total", "n": 3})._progress_label.text \
        == "Found 3 file(s)…"
    assert _FakeApp().handle({"type": "files_total", "n": 0})._progress_label.text \
        == "Inbox is empty"


def test_there_is_exactly_one_backend_and_it_is_imap():
    """The predecessor of this test existed because a live bug stamped
    mail_backend "gmail_oauth" underneath a fully filled-in IMAP form and
    Connect then opened a browser. There is now only one backend to fall back
    to, on both front-ends -- Android's BACKEND_LABELS must match entry for
    entry, see PLATFORM-PARITY.md."""
    from src.config import DEFAULT_MAIL_BACKEND, MAIL_BACKEND_IMAP

    assert list(gui._BACKEND_LABELS) == [MAIL_BACKEND_IMAP]
    assert DEFAULT_MAIL_BACKEND == MAIL_BACKEND_IMAP


def test_the_poll_loop_survives_the_worker_clearing_itself_mid_drain():
    """Reported live: the bar froze mid-run and the log stopped updating.

    _handle_sync_event() sets self._worker = None on "done"/"error", and the
    drain loop re-read self._worker on every iteration -- so the next
    get_nowait() raised "AttributeError: 'NoneType' object has no attribute
    'q'". That escaped the Tk callback, killing the poll while the sync thread
    carried on working. The loop now holds its worker in a local and stops the
    moment the run it was polling is over.
    """
    class _Worker:
        def __init__(self, events):
            self.q = queue.Queue()
            for e in events:
                self.q.put(e)

    class _App:
        def __init__(self, worker):
            self._worker = worker
            self.scheduled = 0
            self.seen = []

        def after(self, _ms, _cb):
            self.scheduled += 1

        def _handle_sync_event(self, event):
            self.seen.append(event["type"])
            # Stand-in for the real "done"/"error" branches.
            if event["type"] in ("done", "error"):
                self._worker = None

    # A second event sits behind the terminal one, which is what used to blow up.
    worker = _Worker([{"type": "log", "msg": "x"},
                      {"type": "error", "msg": "boom"},
                      {"type": "log", "msg": "never read"}])
    app = _App(worker)
    gui.App._poll_sync_queue(app)          # must not raise
    assert app.seen == ["log", "error"]
    assert app.scheduled == 0              # run is over: no further timer chain


# ----------------------------------------------------------------------
# Launch-time watched-folder scan
# ----------------------------------------------------------------------


def test_launch_scan_gate_matches_the_periodic_timer():
    """The startup scan runs under exactly the timer's condition, and no wider.

    The gate is the whole feature here. A scan at launch that ignored the
    auto-watch switch would be the app doing on startup precisely what the
    user had turned off; one that ignored an unset folder would have nothing
    to scan. "Check now" stays the way to force a scan regardless.
    """
    from pathlib import Path

    folder = Path("C:/exports")

    assert gui._should_scan_at_launch(True, folder) is True
    assert gui._should_scan_at_launch(False, folder) is False
    assert gui._should_scan_at_launch(True, None) is False
    assert gui._should_scan_at_launch(False, None) is False
