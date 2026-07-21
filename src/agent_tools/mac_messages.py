"""Send a text via macOS Messages.app (iMessage / SMS), gated behind approval.

The agent NEVER sends directly. `send_imessage` STAGES the message and returns
a pending_id; the chat renders an Approve/Decline card (static/js/chat.js,
mirroring the email approval flow), and the buttons hit the REST endpoints in
routes/messages_routes.py. Only an explicit Approve runs the AppleScript that
actually delivers it — so nothing leaves the machine without the user's click.

macOS only: sending shells out to `osascript`. On other platforms the tool and
the approve endpoint return a clear "macOS only" message.

Contract matches the rest of agent_tools: async execute(content, ctx) -> dict.
"""
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

from src.constants import DATA_DIR

_STORE = os.path.join(DATA_DIR, "pending_messages.json")
_LOCK = threading.Lock()

# Passing recipient + body as argv (never string-interpolated into the script)
# makes AppleScript injection impossible. `osascript - a b` reads the script
# from stdin and exposes a, b as `argv`. Service is chosen by type so the same
# script covers iMessage (blue) and SMS (green, needs Text Message Forwarding).
_APPLESCRIPT = """
on run argv
    set theRecipient to item 1 of argv
    set theBody to item 2 of argv
    set theService to item 3 of argv
    tell application "Messages"
        if theService is "sms" then
            set svc to 1st service whose service type = SMS
        else
            set svc to 1st service whose service type = iMessage
        end if
        set theBuddy to buddy theRecipient of svc
        send theBody to theBuddy
    end tell
end run
"""


def _load() -> Dict[str, dict]:
    try:
        with open(_STORE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: Dict[str, dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, _STORE)


def stage(to: str, body: str, service: str, owner: str) -> str:
    pid = secrets.token_hex(4)
    with _LOCK:
        d = _load()
        d[pid] = {
            "id": pid, "to": to, "body": body,
            "service": "sms" if str(service).lower() == "sms" else "imessage",
            "owner": owner or "", "status": "staged", "error": "",
            "created": time.time(),
        }
        _save(d)
    return pid


def list_pending(owner: str) -> List[dict]:
    d = _load()
    return [r for r in d.values()
            if r.get("status") == "staged" and (not owner or r.get("owner") == owner)]


def get(pid: str) -> Optional[dict]:
    return _load().get(pid)


def _send_via_applescript(recipient: str, body: str, service: str) -> Optional[str]:
    """Deliver now. Returns None on success, or an error string."""
    if sys.platform != "darwin":
        return "Sending via Messages is only available on macOS."
    try:
        proc = subprocess.run(
            ["osascript", "-", recipient, body, service],
            input=_APPLESCRIPT, text=True, capture_output=True, timeout=30,
        )
    except FileNotFoundError:
        return "osascript not found — is this macOS?"
    except subprocess.TimeoutExpired:
        return "Messages did not respond within 30s (is Messages.app signed in?)."
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or "AppleScript failed"
        # Most common real-world failures, made actionable.
        if "Can't get buddy" in err or "invalid" in err.lower():
            return (f"Couldn't reach '{recipient}' on "
                    f"{'SMS' if service == 'sms' else 'iMessage'}. "
                    "For a non-iMessage number, enable Text Message Forwarding "
                    "on your iPhone and send with service='sms'.")
        if "Not authorized" in err or "assistive" in err.lower():
            return ("macOS blocked automation of Messages. Grant Odysseus "
                    "control of Messages under System Settings → Privacy & "
                    "Security → Automation.")
        return err
    return None


def approve(pid: str) -> dict:
    with _LOCK:
        d = _load()
        row = d.get(pid)
        if not row:
            return {"success": False, "error": "No such staged message."}
        if row.get("status") == "sent":
            return {"success": True, "already": True}
        row["status"] = "sending"
        _save(d)
    err = _send_via_applescript(row["to"], row["body"], row["service"])
    with _LOCK:
        d = _load()
        row = d.get(pid) or row
        row["status"] = "failed" if err else "sent"
        row["error"] = err or ""
        d[pid] = row
        _save(d)
    return {"success": not err, "error": err or ""}


def decline(pid: str) -> dict:
    with _LOCK:
        d = _load()
        if pid in d:
            d[pid]["status"] = "declined"
            _save(d)
    return {"success": True}


def status(pid: str) -> dict:
    row = _load().get(pid) or {}
    return {"status": row.get("status", "unknown"), "error": row.get("error", "")}


class MacMessagesTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        raw = (content or "").strip()
        try:
            args = json.loads(raw) if raw.startswith("{") else {}
        except json.JSONDecodeError:
            return {"error": 'send_imessage: arguments must be JSON, e.g. '
                             '{"to":"+15551234567","body":"hi"}', "exit_code": 1}
        to = str(args.get("to") or "").strip()
        body = args.get("body")
        if not to or not body:
            return {"error": "send_imessage: 'to' and 'body' are required", "exit_code": 1}
        if sys.platform != "darwin":
            return {"error": "send_imessage is only available on macOS (uses Messages.app).",
                    "exit_code": 1}
        owner = (ctx or {}).get("owner") or ""
        pid = stage(to, str(body), str(args.get("service") or "imessage"), owner)
        svc = "SMS" if str(args.get("service") or "").lower() == "sms" else "iMessage"
        return {
            "output": (
                f"✋ Message staged for {to} via {svc} — NOTHING HAS BEEN SENT. "
                "The user now sees an Approve/Decline card in the chat; tell them "
                "to review it and press one (the buttons handle delivery — you do "
                "not need to do anything else). Do NOT claim the text was sent "
                f"until the card confirms it. pending_id='{pid}'"
            ),
            "exit_code": 0,
        }
