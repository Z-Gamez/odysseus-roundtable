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
#
# `account`/`participant` is the form that still works on current macOS; the
# older `service`/`buddy` form is kept as a fallback for older systems. The
# whole tell block is wrapped in an explicit timeout so a wedged Messages
# surfaces as AppleScript error -1712 (which we map to a real hint) instead of
# hanging until the subprocess is killed.
_APPLESCRIPT = """
on run argv
    set theRecipient to item 1 of argv
    set theBody to item 2 of argv
    set theService to item 3 of argv
    with timeout of 30 seconds
        tell application "Messages"
            if theService is "sms" then
                set svcType to SMS
            else
                set svcType to iMessage
            end if
            try
                set targetService to 1st account whose service type = svcType
                set targetBuddy to participant theRecipient of targetService
                send theBody to targetBuddy
            on error
                set targetService to 1st service whose service type = svcType
                set targetBuddy to buddy theRecipient of targetService
                send theBody to targetBuddy
            end try
        end tell
    end timeout
end run
"""


def _ensure_messages_running(timeout_s: float = 12.0) -> None:
    """Launch Messages.app and wait until it's actually up.

    Sending to a cold Messages is the main source of AppleEvent timeouts
    (-1712): the send AppleEvent arrives while the app is still starting and
    never gets answered. Best-effort — failures here are not fatal, the send
    itself reports the real error."""
    try:
        subprocess.run(["open", "-a", "Messages"], capture_output=True, timeout=15)
    except Exception:
        return
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = subprocess.run(["pgrep", "-x", "Messages"], capture_output=True, timeout=5)
            if r.returncode == 0:
                time.sleep(1.0)   # let it finish wiring up its services
                return
        except Exception:
            return
        time.sleep(0.4)


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


def owner_matches(row: dict, owner: str) -> bool:
    """Whether `owner` may see/act on this staged message.

    The agent's tool ctx owner can be None/"" (single-user mode, or a caller
    that didn't thread it through) while the HTTP request authenticates as a
    real username — a strict equality check then hid the row from its own
    approval card, which rendered blank. Treat an unset owner on either side
    as "same user"; two DIFFERENT named owners still don't match.
    """
    row_owner = (row or {}).get("owner") or ""
    req_owner = owner or ""
    if not row_owner or not req_owner:
        return True
    return row_owner == req_owner


def list_pending(owner: str) -> List[dict]:
    d = _load()
    return [r for r in d.values()
            if r.get("status") == "staged" and owner_matches(r, owner)]


def get(pid: str) -> Optional[dict]:
    return _load().get(pid)


_AUTOMATION_HINT = (
    "macOS blocked automation of Messages. Allow Odysseus to control Messages "
    "under System Settings → Privacy & Security → Automation (if Odysseus "
    "isn't listed, quit and relaunch it, then retry so macOS re-prompts)."
)
_TIMEOUT_HINT = (
    "Messages didn't respond (AppleEvent timed out). Open Messages.app and "
    "confirm it's signed in to iMessage, and that Odysseus is allowed to "
    "control Messages under System Settings → Privacy & Security → Automation."
)


def _send_via_applescript(recipient: str, body: str, service: str) -> Optional[str]:
    """Deliver now. Returns None on success, or a human-readable error string.

    The full osascript stderr is always preserved in the returned message so an
    unmapped AppleScript error can still be diagnosed from the approval card.
    """
    if sys.platform != "darwin":
        return "Sending via Messages is only available on macOS."
    _ensure_messages_running()
    try:
        proc = subprocess.run(
            ["osascript", "-", recipient, body, service],
            input=_APPLESCRIPT, text=True, capture_output=True,
            # Longer than the script's own 30s timeout so AppleScript reports
            # -1712 itself rather than us killing it with no diagnosis.
            timeout=45,
        )
    except FileNotFoundError:
        return "osascript not found — is this macOS?"
    except subprocess.TimeoutExpired:
        return _TIMEOUT_HINT
    if proc.returncode == 0:
        return None

    err = (proc.stderr or "").strip() or "AppleScript failed with no output"
    low = err.lower()
    # Map the known AppleScript error numbers to actionable hints, but always
    # append the raw stderr — an unmapped failure must not become a generic
    # "send failed" with the cause thrown away.
    if "-1743" in err or "not authorized" in low or "assistive" in low:
        return f"{_AUTOMATION_HINT}\n\nDetail: {err}"
    if "-1712" in err or "timed out" in low:
        return f"{_TIMEOUT_HINT}\n\nDetail: {err}"
    if "-1728" in err or "can't get buddy" in low or "can't get participant" in low:
        svc_label = "SMS" if str(service).lower() == "sms" else "iMessage"
        return (f"Couldn't reach '{recipient}' on {svc_label}. For a "
                "non-iMessage number, enable Text Message Forwarding on your "
                f"iPhone and send with service='sms'.\n\nDetail: {err}")
    return err


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
