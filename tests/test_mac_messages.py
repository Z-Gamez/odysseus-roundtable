"""macOS Messages send tool — staging + approval state machine.

The agent stages a text (never sends); the chat's Approve/Decline card hits
the REST endpoints, and only Approve runs the AppleScript. These tests cover
the storage/approval logic and the injection-safe AppleScript invocation
without touching osascript (mocked); the live send is exercised on the Mac.
"""
import asyncio

import pytest

from src.agent_tools import mac_messages


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setattr(mac_messages, "_STORE", str(tmp_path / "pending_messages.json"))
    yield


def test_stage_and_list():
    pid = mac_messages.stage("+15551234567", "hi there", "imessage", "owner1")
    assert len(pid) == 8
    pending = mac_messages.list_pending("owner1")
    assert len(pending) == 1 and pending[0]["to"] == "+15551234567"
    assert pending[0]["status"] == "staged"
    # owner isolation
    assert mac_messages.list_pending("someone-else") == []


def test_decline_removes_from_pending():
    pid = mac_messages.stage("+1555", "yo", "imessage", "o")
    mac_messages.decline(pid)
    assert mac_messages.list_pending("o") == []
    assert mac_messages.get(pid)["status"] == "declined"


def test_approve_sends_and_records_status(monkeypatch):
    sent = {}

    def fake_send(recipient, body, service):
        sent["args"] = (recipient, body, service)
        return None  # success

    monkeypatch.setattr(mac_messages, "_send_via_applescript", fake_send)
    pid = mac_messages.stage("+1555", "hello", "imessage", "o")
    res = mac_messages.approve(pid)
    assert res["success"] is True
    assert sent["args"] == ("+1555", "hello", "imessage")
    assert mac_messages.status(pid)["status"] == "sent"


def test_approve_records_failure(monkeypatch):
    monkeypatch.setattr(mac_messages, "_send_via_applescript",
                        lambda r, b, s: "Couldn't reach that number")
    pid = mac_messages.stage("+1555", "hello", "sms", "o")
    res = mac_messages.approve(pid)
    assert res["success"] is False
    st = mac_messages.status(pid)
    assert st["status"] == "failed" and "reach" in st["error"]


def test_applescript_invocation_is_injection_safe(monkeypatch):
    """Recipient + body must be passed as argv, never interpolated into the
    script — otherwise a body containing AppleScript could run."""
    captured = {}

    class _Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, input=None, text=None, capture_output=None, timeout=None):
        captured["cmd"] = cmd
        captured["script"] = input
        return _Proc()

    monkeypatch.setattr(mac_messages.subprocess, "run", fake_run)
    monkeypatch.setattr(mac_messages.sys, "platform", "darwin")
    err = mac_messages._send_via_applescript('"; do shell script "evil"', "body", "imessage")
    assert err is None
    # The malicious recipient is an argv element, not spliced into the script.
    assert '"; do shell script "evil"' in captured["cmd"]
    assert "do shell script" not in captured["script"]
    assert captured["cmd"][0] == "osascript" and captured["cmd"][1] == "-"


def _osascript_failing(monkeypatch, stderr, returncode=1):
    """Mock osascript failing with `stderr`; neutralize the Messages pre-launch."""
    monkeypatch.setattr(mac_messages, "_ensure_messages_running", lambda *a, **k: None)
    monkeypatch.setattr(mac_messages.sys, "platform", "darwin")

    class _Proc:
        pass

    def fake_run(cmd, **kw):
        p = _Proc()
        p.returncode = returncode
        p.stderr = stderr
        return p

    monkeypatch.setattr(mac_messages.subprocess, "run", fake_run)


def test_timeout_1712_maps_to_actionable_hint(monkeypatch):
    """The reported macOS 26.5.2 failure: -1712 must not become a generic error."""
    raw = "execution error: Messages got an error: AppleEvent timed out. (-1712)"
    _osascript_failing(monkeypatch, raw)
    err = mac_messages._send_via_applescript("+1555", "hi", "imessage")
    assert "didn't respond" in err
    assert "Automation" in err
    assert raw in err          # full stderr preserved for diagnosis


def test_not_authorized_1743_maps_to_automation_hint(monkeypatch):
    raw = "execution error: Not authorized to send Apple events to Messages. (-1743)"
    _osascript_failing(monkeypatch, raw)
    err = mac_messages._send_via_applescript("+1555", "hi", "imessage")
    assert "Privacy & Security" in err and "Automation" in err
    assert raw in err


def test_unknown_error_returns_full_stderr(monkeypatch):
    raw = "execution error: something nobody mapped (-9999)"
    _osascript_failing(monkeypatch, raw)
    err = mac_messages._send_via_applescript("+1555", "hi", "imessage")
    assert err == raw          # never swallowed into a generic message


def test_success_returns_none(monkeypatch):
    _osascript_failing(monkeypatch, "", returncode=0)
    assert mac_messages._send_via_applescript("+1555", "hi", "imessage") is None


def test_applescript_uses_participant_form_with_timeout():
    # The deprecated buddy/service form must only be the fallback, and the
    # send must be bounded by an explicit AppleScript timeout.
    s = mac_messages._APPLESCRIPT
    assert "with timeout of 30 seconds" in s
    assert "1st account whose service type" in s
    assert "participant theRecipient" in s
    assert s.index("participant theRecipient") < s.index("buddy theRecipient")


def test_owner_matches_tolerates_unset_owner():
    # The empty-card bug: agent ctx owner "" vs request owner "admin".
    assert mac_messages.owner_matches({"owner": ""}, "admin") is True
    assert mac_messages.owner_matches({"owner": "admin"}, "") is True
    assert mac_messages.owner_matches({"owner": "admin"}, "admin") is True
    # Two DIFFERENT named users still don't match.
    assert mac_messages.owner_matches({"owner": "alice"}, "bob") is False


def test_pending_visible_when_staged_without_owner():
    mac_messages.stage("+1555", "hi", "imessage", "")
    assert len(mac_messages.list_pending("admin")) == 1


def test_failed_error_persisted_for_the_card(monkeypatch):
    raw = "execution error: Messages got an error: AppleEvent timed out. (-1712)"
    _osascript_failing(monkeypatch, raw)
    pid = mac_messages.stage("+1555", "hi", "imessage", "")
    res = mac_messages.approve(pid)
    assert res["success"] is False
    # Written back to pending_messages.json so the card/status can show it.
    saved = mac_messages.get(pid)
    assert saved["status"] == "failed"
    assert raw in saved["error"]
    assert raw in res["error"]


def test_tool_stages_not_sends(monkeypatch):
    monkeypatch.setattr(mac_messages.sys, "platform", "darwin")
    tool = mac_messages.MacMessagesTool()
    out = asyncio.run(tool.execute('{"to":"+1555","body":"hey"}', {"owner": "o"}))
    assert out["exit_code"] == 0
    assert "NOTHING HAS BEEN SENT" in out["output"]
    assert "pending_id='" in out["output"]


def test_pending_row_field_contract():
    """The approval card reads p.id / row.to / row.body / row.service from
    GET /api/messages/pending. Renaming any of these blanks the card."""
    mac_messages.stage("+15551234567", "hello there", "sms", "o")
    row = mac_messages.list_pending("o")[0]
    for key in ("id", "to", "body", "service"):
        assert key in row, f"card reads '{key}' — missing from pending row"
    assert row["to"] == "+15551234567"
    assert row["body"] == "hello there"
    assert row["service"] == "sms"


def test_status_exposes_error_for_failed(monkeypatch):
    _osascript_failing(monkeypatch, "execution error: boom (-1712)")
    pid = mac_messages.stage("+1555", "hi", "imessage", "o")
    mac_messages.approve(pid)
    st = mac_messages.status(pid)
    assert st["status"] == "failed" and st["error"]


def test_tool_rejects_off_mac(monkeypatch):
    monkeypatch.setattr(mac_messages.sys, "platform", "win32")
    tool = mac_messages.MacMessagesTool()
    out = asyncio.run(tool.execute('{"to":"+1555","body":"hey"}', {}))
    assert out["exit_code"] == 1 and "macOS" in out["error"]
