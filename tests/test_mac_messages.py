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


def test_tool_stages_not_sends(monkeypatch):
    monkeypatch.setattr(mac_messages.sys, "platform", "darwin")
    tool = mac_messages.MacMessagesTool()
    out = asyncio.run(tool.execute('{"to":"+1555","body":"hey"}', {"owner": "o"}))
    assert out["exit_code"] == 0
    assert "NOTHING HAS BEEN SENT" in out["output"]
    assert "pending_id='" in out["output"]


def test_tool_rejects_off_mac(monkeypatch):
    monkeypatch.setattr(mac_messages.sys, "platform", "win32")
    tool = mac_messages.MacMessagesTool()
    out = asyncio.run(tool.execute('{"to":"+1555","body":"hey"}', {}))
    assert out["exit_code"] == 1 and "macOS" in out["error"]
