"""Send a text on macOS (iMessage / SMS), gated behind approval.

The agent NEVER sends directly. `send_imessage` STAGES the message and returns
a pending_id; the chat renders an Approve/Decline card (static/js/chat.js,
mirroring the email approval flow), and the buttons hit the REST endpoints in
routes/messages_routes.py. Only an explicit Approve triggers delivery — so
nothing leaves the machine without the user's click.

Delivery goes through the macOS **Shortcuts CLI** (`shortcuts run`) rather than
Messages AppleScript: direct AppleScript automation is unreliable on macOS 26
(AppleEvent timeouts, -1712). Requires a user-created shortcut named
SHORTCUT_NAME that accepts JSON text input {"to": ..., "body": ...} and runs
the Send Message action, which picks iMessage vs SMS on its own.

macOS only: on other platforms the tool and the approve endpoint return a clear
"macOS only" message.

Contract matches the rest of agent_tools: async execute(content, ctx) -> dict.
"""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from typing import Dict, List, Optional

from src.constants import DATA_DIR

_STORE = os.path.join(DATA_DIR, "pending_messages.json")
_LOCK = threading.Lock()

# A user-created Shortcut (Shortcuts.app) that takes JSON text input
# {"to": ..., "body": ...} and runs the Send Message action. Delivery goes
# through the Shortcuts CLI because direct Messages AppleScript automation is
# unreliable on macOS 26 (AppleEvent timeouts, -1712).
SHORTCUT_NAME = "OdysseusSendMessage"

# Passing recipient + body as argv (never string-interpolated into the script)
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


_MISSING_SHORTCUT_HINT = (
    f"Shortcut '{SHORTCUT_NAME}' not found. Create it in the Shortcuts app: "
    f"name it exactly '{SHORTCUT_NAME}', have it accept text input, pull the "
    "'to' and 'body' values out of the input JSON, and run the Send Message "
    "action."
)


def _send_via_shortcut(recipient: str, body: str, service: str = "") -> Optional[str]:
    """Deliver a text via the macOS Shortcuts CLI.

    Returns None on success, or a human-readable error string. The raw
    shortcuts stdout/stderr is preserved so an unmapped failure can still be
    diagnosed from the approval card.

    `service` is accepted for call-site compatibility but unused: the Send
    Message action picks iMessage vs SMS itself.
    """
    if sys.platform != "darwin":
        return "Sending messages is only available on macOS."
    payload = json.dumps({"to": recipient, "body": body})
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                      encoding="utf-8")
    try:
        tmp.write(payload)
        tmp.close()
        proc = subprocess.run(
            ["shortcuts", "run", SHORTCUT_NAME, "--input-path", tmp.name],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return "shortcuts CLI not found (macOS 12+ required)."
    except subprocess.TimeoutExpired:
        return (f"Shortcut timed out after 30s (is the {SHORTCUT_NAME} "
                "shortcut set up, and does it run without prompting?).")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        low = err.lower()
        if "not find" in low or "no shortcut" in low or "doesn" in low and "exist" in low:
            return f"{_MISSING_SHORTCUT_HINT}\n\nDetail: {err}" if err else _MISSING_SHORTCUT_HINT
        return err or "Shortcut failed with no error output."
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
    err = _send_via_shortcut(row["to"], row["body"], row.get("service", ""))
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
