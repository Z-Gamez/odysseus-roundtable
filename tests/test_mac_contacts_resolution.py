"""Contact-name resolution for send_imessage.

"text michaela" should reach the approval card as a real number. A name is
resolved against macOS Contacts (osascript) before staging: the Shortcut keeps
receiving a number, while the card shows "Michaela (+1...)" so the user can see
who it's going to before approving. osascript is mocked here; the live lookup
is exercised on the Mac.
"""
import asyncio

import pytest

from src.agent_tools import mac_messages


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(mac_messages, "_STORE", str(tmp_path / "pending_messages.json"))
    monkeypatch.setattr(mac_messages.sys, "platform", "darwin")
    yield


def _contacts(monkeypatch, stdout, returncode=0, stderr="", capture=None):
    class _Proc:
        pass

    def fake_run(cmd, **kw):
        if capture is not None:
            capture["cmd"] = cmd
            capture["script"] = kw.get("input")
        p = _Proc()
        p.returncode = returncode
        p.stdout = stdout
        p.stderr = stderr
        return p

    monkeypatch.setattr(mac_messages.subprocess, "run", fake_run)


# --- what counts as already-addressable -------------------------------------

@pytest.mark.parametrize("value", [
    "+14803109822", "4803109822", "480-310-9822", "(480) 310-9822",
    "someone@example.com", "+1 480 310 9822",
    "+1555",   # a leading + means "number" regardless of length
])
def test_handles_pass_through(value):
    assert mac_messages._looks_like_handle(value) is True


@pytest.mark.parametrize("value", ["Michaela", "Mike Smith", "mom", "", "  "])
def test_names_are_not_handles(value):
    assert mac_messages._looks_like_handle(value) is False


# --- E.164 normalization (conservative on purpose) --------------------------

@pytest.mark.parametrize("raw,expected", [
    ("(480) 310-9822", "+14803109822"),
    ("480-310-9822", "+14803109822"),
    ("14803109822", "+14803109822"),
    ("+1 (480) 310-9822", "+14803109822"),
    ("+44 20 7946 0958", "+442079460958"),
])
def test_e164_normalization(raw, expected):
    assert mac_messages._to_e164(raw) == expected


def test_e164_passes_through_unknown_shapes():
    # Better to send what Contacts stored than to mangle it into a wrong number.
    assert mac_messages._to_e164("12345") == "12345"


# --- resolution ---------------------------------------------------------------

def test_resolves_single_match(monkeypatch):
    cap = {}
    _contacts(monkeypatch, "Michaela Smith|(480) 310-9822", capture=cap)
    number, display, err = mac_messages.resolve_contact_name("Michaela")
    assert err is None
    assert number == "+14803109822"
    assert display == "Michaela Smith (+14803109822)"
    # Name goes through argv, never interpolated into the script.
    assert "Michaela" in cap["cmd"]
    assert cap["cmd"][0] == "osascript" and cap["cmd"][1] == "-"
    assert "Michaela" not in (cap["script"] or "")


def test_same_person_multiple_phones_is_not_ambiguous(monkeypatch):
    _contacts(monkeypatch, "Michaela Smith|(480) 310-9822\nMichaela Smith|555-0000")
    number, display, err = mac_messages.resolve_contact_name("Michaela")
    assert err is None and number == "+14803109822"


def test_multiple_people_asks_instead_of_guessing(monkeypatch):
    _contacts(monkeypatch, "Mike Smith|480-310-9822\nMike Jones|602-555-1212")
    number, display, err = mac_messages.resolve_contact_name("Mike")
    assert number is None
    assert "Mike Smith" in err and "Mike Jones" in err


def test_no_match_error(monkeypatch):
    _contacts(monkeypatch, "NOMATCH")
    _, _, err = mac_messages.resolve_contact_name("Nobody")
    assert "No contact named 'Nobody'" in err


def test_contact_without_phone(monkeypatch):
    _contacts(monkeypatch, "NOPHONE")
    _, _, err = mac_messages.resolve_contact_name("Emailonly")
    assert "no phone number" in err


def test_contacts_permission_denied(monkeypatch):
    _contacts(monkeypatch, "", returncode=1,
              stderr="execution error: Not authorized to send Apple events (-1743)")
    _, _, err = mac_messages.resolve_contact_name("Michaela")
    assert "Privacy & Security" in err and "Contacts" in err


# --- end-to-end through the tool ---------------------------------------------

def test_tool_resolves_name_then_stages_number(monkeypatch):
    _contacts(monkeypatch, "Michaela Smith|(480) 310-9822")
    out = asyncio.run(mac_messages.MacMessagesTool().execute(
        '{"to":"Michaela","body":"test"}', {"owner": "o"}))
    assert out["exit_code"] == 0
    assert "Michaela Smith (+14803109822)" in out["output"]
    row = mac_messages.list_pending("o")[0]
    assert row["to"] == "+14803109822"            # what the Shortcut receives
    assert row["to_display"] == "Michaela Smith (+14803109822)"   # what the card shows


def test_tool_errors_when_contact_missing(monkeypatch):
    _contacts(monkeypatch, "NOMATCH")
    out = asyncio.run(mac_messages.MacMessagesTool().execute(
        '{"to":"Nobody","body":"hi"}', {"owner": "o"}))
    assert out["exit_code"] == 1
    assert "No contact named 'Nobody'" in out["error"]
    assert mac_messages.list_pending("o") == []   # nothing staged on failure


def test_tool_skips_lookup_for_a_number(monkeypatch):
    called = {"n": 0}

    def should_not_run(*a, **k):
        called["n"] += 1
        raise AssertionError("Contacts lookup ran for an explicit number")

    monkeypatch.setattr(mac_messages.subprocess, "run", should_not_run)
    out = asyncio.run(mac_messages.MacMessagesTool().execute(
        '{"to":"+14803109822","body":"hi"}', {"owner": "o"}))
    assert out["exit_code"] == 0 and called["n"] == 0
    assert mac_messages.list_pending("o")[0]["to"] == "+14803109822"
