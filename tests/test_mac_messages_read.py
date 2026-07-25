"""chat.db reader + AppleScript-first delivery.

Reading uses a real on-disk SQLite file shaped like Messages' chat.db, so the
queries (joins, Apple-epoch conversion, attributedBody fallback) are exercised
for real rather than mocked. Delivery order is checked separately: AppleScript
is primary, the Shortcut is only a backstop.
"""
import asyncio
import os
import sqlite3

import pytest

from src.agent_tools import mac_messages as mm
from src.agent_tools import mac_messages_read as rd


# --- fixture: a miniature chat.db ------------------------------------------

def _mk_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB,
            handle_id INTEGER, date INTEGER, is_from_me INTEGER, is_read INTEGER
        );
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
    """)
    conn.execute("INSERT INTO handle VALUES (1, '+15551234567')")
    conn.execute("INSERT INTO chat VALUES (1, '+15551234567', 'Michaela')")
    # 2026-01-01T00:00:00Z in Apple-epoch nanoseconds.
    base = (1767225600 - 978307200) * 10**9
    rows = [
        (1, "hey there", None, 1, base, 0, 1),
        (2, "on my way", None, 1, base + 10**9, 1, 1),
        (3, None, None, 1, base + 2 * 10**9, 0, 0),   # attributedBody filled below
    ]
    conn.executemany("INSERT INTO message VALUES (?,?,?,?,?,?,?)", rows)
    # A realistic-ish NSAttributedString archive carrying "sent from my phone".
    body = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01\x40\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01\x2b\x12sent from my phone\x86"
    conn.execute("UPDATE message SET attributedBody=? WHERE ROWID=3", (body,))
    conn.executemany("INSERT INTO chat_message_join VALUES (?,?)", [(1, 1), (1, 2), (1, 3)])
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "chat.db"
    _mk_db(str(p))
    monkeypatch.setattr(rd, "CHAT_DB", str(p))
    monkeypatch.setattr(rd.sys, "platform", "darwin")
    return str(p)


# --- reading ----------------------------------------------------------------

def test_list_chats(db):
    chats = rd.list_chats()
    assert len(chats) == 1
    assert chats[0]["chat"] == "Michaela"
    # Rendered in LOCAL time (what a user wants for a message timestamp), so
    # assert against the locally-converted instant rather than a fixed string.
    import datetime
    # +2s: the chat's "last" is its NEWEST message (ROWID 3), not its first.
    expect = datetime.datetime.fromtimestamp(1767225600 + 2).isoformat(" ", "seconds")
    assert chats[0]["last"] == expect


def test_read_conversation_is_chronological(db):
    msgs = rd.read_conversation("Michaela")
    assert [m["text"] for m in msgs][:2] == ["hey there", "on my way"]
    assert msgs[0]["from"] == "+15551234567"
    assert msgs[1]["from"] == "me"          # is_from_me maps to "me"


def test_search_finds_by_text(db):
    hits = rd.search_messages("on my way")
    assert len(hits) == 1 and hits[0]["from"] == "me"


def test_unread_only_incoming(db):
    un = rd.unread_messages()
    # Only ROWID 3 is unread+incoming, and its body lives in attributedBody.
    assert len(un) == 1
    assert "sent from my phone" in un[0]["text"]


def test_attributed_body_decoded(db):
    msgs = rd.read_conversation("Michaela")
    assert any("sent from my phone" in m["text"] for m in msgs)


def test_apple_epoch_conversion():
    import datetime
    ns = (1767225600 - 978307200) * 10**9
    # Apple epoch (ns since 2001) -> local ISO. Compare to the same instant.
    assert rd._apple_time_to_iso(ns) == datetime.datetime.fromtimestamp(1767225600).isoformat(" ", "seconds")
    # Seconds-based dates (very old macOS) go down the same path.
    secs = 1767225600 - 978307200
    assert rd._apple_time_to_iso(secs) == datetime.datetime.fromtimestamp(1767225600).isoformat(" ", "seconds")
    assert rd._apple_time_to_iso(0) == ""
    assert rd._apple_time_to_iso(None) == ""


def test_db_opened_read_only(db):
    conn = rd._connect()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM message")
    conn.close()


def test_missing_db_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "CHAT_DB", str(tmp_path / "nope.db"))
    monkeypatch.setattr(rd.sys, "platform", "darwin")
    out = asyncio.run(rd.ReadMessagesTool().execute('{"action":"chats"}', {}))
    assert out["exit_code"] == 1 and "No Messages database" in out["error"]


def test_tool_actions(db):
    t = rd.ReadMessagesTool()
    assert "Michaela" in asyncio.run(t.execute('{"action":"chats"}', {}))["output"]
    assert "hey there" in asyncio.run(
        t.execute('{"action":"conversation","who":"Michaela"}', {}))["output"]
    bad = asyncio.run(t.execute('{"action":"conversation"}', {}))
    assert bad["exit_code"] == 1 and "'who' is required" in bad["error"]
    unknown = asyncio.run(t.execute('{"action":"bogus"}', {}))
    assert unknown["exit_code"] == 1 and "unknown action" in unknown["error"]


def test_tool_rejects_off_mac(monkeypatch):
    monkeypatch.setattr(rd.sys, "platform", "win32")
    out = asyncio.run(rd.ReadMessagesTool().execute('{"action":"chats"}', {}))
    assert out["exit_code"] == 1 and "macOS" in out["error"]


# --- delivery order ---------------------------------------------------------

def test_applescript_is_primary(monkeypatch):
    calls = []
    monkeypatch.setattr(mm, "_send_via_applescript",
                        lambda r, b, s="": calls.append("applescript") or None)
    monkeypatch.setattr(mm, "_send_via_shortcut",
                        lambda r, b, s="": calls.append("shortcut") or None)
    assert mm.deliver("+1555", "hi") is None
    assert calls == ["applescript"], "Shortcut must not run when AppleScript succeeds"


def test_shortcut_is_the_backstop(monkeypatch):
    calls = []

    def as_fail(r, b, s=""):
        calls.append("applescript")
        return "Messages didn't respond (AppleEvent timed out twice)."

    monkeypatch.setattr(mm, "_send_via_applescript", as_fail)
    monkeypatch.setattr(mm, "_send_via_shortcut",
                        lambda r, b, s="": calls.append("shortcut") or None)
    assert mm.deliver("+1555", "hi") is None      # rescued
    assert calls == ["applescript", "shortcut"]


def test_bad_recipient_not_retried_via_shortcut(monkeypatch):
    calls = []
    monkeypatch.setattr(mm, "_send_via_applescript",
                        lambda r, b, s="": "Couldn't reach '+1555' on iMessage.")
    monkeypatch.setattr(mm, "_send_via_shortcut",
                        lambda r, b, s="": calls.append("shortcut") or None)
    err = mm.deliver("+1555", "hi")
    assert "Couldn't reach" in err
    assert calls == [], "a bad recipient would fail identically via the Shortcut"


def test_both_failing_reports_applescript_error(monkeypatch):
    monkeypatch.setattr(mm, "_send_via_applescript",
                        lambda r, b, s="": "AppleScript says: not authorized (-1743)")
    monkeypatch.setattr(mm, "_send_via_shortcut",
                        lambda r, b, s="": "shortcut missing")
    err = mm.deliver("+1555", "hi")
    assert "-1743" in err        # the actionable one leads


def test_applescript_retries_once_on_timeout(monkeypatch):
    attempts = {"n": 0}

    class _P:
        def __init__(self, rc, err=""):
            self.returncode, self.stderr = rc, err

    def fake_run(cmd, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return _P(1, "execution error: AppleEvent timed out. (-1712)")
        return _P(0)

    monkeypatch.setattr(mm.sys, "platform", "darwin")
    monkeypatch.setattr(mm, "_ensure_messages_running", lambda *a, **k: None)
    monkeypatch.setattr(mm.subprocess, "run", fake_run)
    monkeypatch.setattr(mm.time, "sleep", lambda s: None)
    assert mm._send_via_applescript("+1555", "hi") is None
    assert attempts["n"] == 2


def test_not_authorized_is_not_retried(monkeypatch):
    attempts = {"n": 0}

    class _P:
        returncode = 1
        stderr = "execution error: Not authorized to send Apple events. (-1743)"

    def fake_run(cmd, **kw):
        attempts["n"] += 1
        return _P()

    monkeypatch.setattr(mm.sys, "platform", "darwin")
    monkeypatch.setattr(mm, "_ensure_messages_running", lambda *a, **k: None)
    monkeypatch.setattr(mm.subprocess, "run", fake_run)
    err = mm._send_via_applescript("+1555", "hi")
    assert attempts["n"] == 1, "a missing TCC grant can't be fixed by retrying"
    assert "Automation" in err and "-1743" in err


def test_applescript_passes_argv_not_interpolated(monkeypatch):
    seen = {}

    class _P:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["script"] = kw.get("input")
        return _P()

    monkeypatch.setattr(mm.sys, "platform", "darwin")
    monkeypatch.setattr(mm, "_ensure_messages_running", lambda *a, **k: None)
    monkeypatch.setattr(mm.subprocess, "run", fake_run)
    mm._send_via_applescript('"; do shell script "evil"', "body")
    assert '"; do shell script "evil"' in seen["cmd"]
    assert "do shell script" not in seen["script"]
