"""macOS text-send tool — staging + approval state machine.

The agent stages a text (never sends); the chat's Approve/Decline card hits
the REST endpoints, and only Approve delivers. Delivery runs the macOS
Shortcuts CLI (`shortcuts run OdysseusSendMessage`) rather than Messages
AppleScript, which is unreliable on macOS 26. These tests cover the storage/
approval logic and the Shortcuts invocation with the subprocess mocked; the
live send is exercised on the Mac.
"""
import asyncio
import json
import os

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

    monkeypatch.setattr(mac_messages, "_send_via_shortcut", fake_send)
    pid = mac_messages.stage("+1555", "hello", "imessage", "o")
    res = mac_messages.approve(pid)
    assert res["success"] is True
    assert sent["args"] == ("+1555", "hello", "imessage")
    assert mac_messages.status(pid)["status"] == "sent"


def test_approve_records_failure(monkeypatch):
    monkeypatch.setattr(mac_messages, "_send_via_shortcut",
                        lambda r, b, s: "Couldn't reach that number")
    pid = mac_messages.stage("+1555", "hello", "sms", "o")
    res = mac_messages.approve(pid)
    assert res["success"] is False
    st = mac_messages.status(pid)
    assert st["status"] == "failed" and "reach" in st["error"]


def _shortcuts_result(monkeypatch, returncode=0, stderr="", stdout="", capture=None):
    """Mock the `shortcuts run` subprocess call."""
    monkeypatch.setattr(mac_messages.sys, "platform", "darwin")

    class _Proc:
        pass

    def fake_run(cmd, **kw):
        if capture is not None:
            capture["cmd"] = cmd
            # Read the JSON payload back before the finally-block unlinks it.
            try:
                idx = cmd.index("--input-path") + 1
                with open(cmd[idx], encoding="utf-8") as f:
                    capture["payload"] = f.read()
            except (ValueError, IndexError, OSError):
                capture["payload"] = None
        p = _Proc()
        p.returncode = returncode
        p.stderr = stderr
        p.stdout = stdout
        return p

    monkeypatch.setattr(mac_messages.subprocess, "run", fake_run)


def test_sends_json_payload_through_shortcuts_cli(monkeypatch):
    """Recipient + body travel as a JSON input FILE — never interpolated into a
    shell string or script, so a hostile body can't inject anything."""
    cap = {}
    _shortcuts_result(monkeypatch, capture=cap)
    err = mac_messages._send_via_shortcut('"; rm -rf /', "hi there", "imessage")
    assert err is None
    assert cap["cmd"][:3] == ["shortcuts", "run", mac_messages.SHORTCUT_NAME]
    assert "--input-path" in cap["cmd"]
    assert json.loads(cap["payload"]) == {"to": '"; rm -rf /', "body": "hi there"}


def test_temp_input_file_is_cleaned_up(monkeypatch):
    cap = {}
    _shortcuts_result(monkeypatch, capture=cap)
    mac_messages._send_via_shortcut("+1555", "hi", "imessage")
    path = cap["cmd"][cap["cmd"].index("--input-path") + 1]
    assert not os.path.exists(path), "temp JSON input left behind"


def test_missing_shortcut_gives_setup_hint(monkeypatch):
    raw = "Could not find shortcut with name OdysseusSendMessage"
    _shortcuts_result(monkeypatch, returncode=1, stderr=raw)
    err = mac_messages._send_via_shortcut("+1555", "hi")
    assert mac_messages.SHORTCUT_NAME in err
    assert "Shortcuts app" in err
    assert raw in err          # raw output preserved for diagnosis


def test_unknown_shortcut_error_returns_raw_output(monkeypatch):
    raw = "some unmapped shortcuts failure"
    _shortcuts_result(monkeypatch, returncode=1, stderr=raw)
    assert mac_messages._send_via_shortcut("+1555", "hi") == raw


def test_falls_back_to_stdout_when_stderr_empty(monkeypatch):
    _shortcuts_result(monkeypatch, returncode=1, stderr="", stdout="failed on stdout")
    assert mac_messages._send_via_shortcut("+1555", "hi") == "failed on stdout"


def test_nonzero_with_no_output_still_errors(monkeypatch):
    _shortcuts_result(monkeypatch, returncode=1, stderr="", stdout="")
    err = mac_messages._send_via_shortcut("+1555", "hi")
    assert err and "no error output" in err


def test_success_returns_none(monkeypatch):
    _shortcuts_result(monkeypatch, returncode=0)
    assert mac_messages._send_via_shortcut("+1555", "hi", "imessage") is None


def test_missing_cli_reports_clearly(monkeypatch):
    monkeypatch.setattr(mac_messages.sys, "platform", "darwin")

    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(mac_messages.subprocess, "run", boom)
    err = mac_messages._send_via_shortcut("+1555", "hi")
    assert "shortcuts CLI not found" in err


def test_timeout_reports_shortcut_hint(monkeypatch):
    monkeypatch.setattr(mac_messages.sys, "platform", "darwin")

    def boom(*a, **k):
        raise mac_messages.subprocess.TimeoutExpired(cmd="shortcuts", timeout=30)

    monkeypatch.setattr(mac_messages.subprocess, "run", boom)
    err = mac_messages._send_via_shortcut("+1555", "hi")
    assert "timed out" in err and mac_messages.SHORTCUT_NAME in err


def test_service_arg_still_accepted_but_optional(monkeypatch):
    """Delivery no longer needs service (Send Message picks the transport),
    but the arg must remain accepted so existing call sites keep working."""
    _shortcuts_result(monkeypatch, returncode=0)
    assert mac_messages._send_via_shortcut("+1555", "hi") is None
    assert mac_messages._send_via_shortcut("+1555", "hi", "sms") is None


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
    raw = "shortcuts: the OdysseusSendMessage shortcut reported a failure"
    _shortcuts_result(monkeypatch, returncode=1, stderr=raw)
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
    _shortcuts_result(monkeypatch, returncode=1, stderr="shortcut failed")
    pid = mac_messages.stage("+1555", "hi", "imessage", "o")
    mac_messages.approve(pid)
    st = mac_messages.status(pid)
    assert st["status"] == "failed" and st["error"]


def test_tool_rejects_off_mac(monkeypatch):
    monkeypatch.setattr(mac_messages.sys, "platform", "win32")
    tool = mac_messages.MacMessagesTool()
    out = asyncio.run(tool.execute('{"to":"+1555","body":"hey"}', {}))
    assert out["exit_code"] == 1 and "macOS" in out["error"]
