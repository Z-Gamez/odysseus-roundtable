"""Read the Mac's iMessage/SMS history straight from chat.db (macOS only).

The send path drives Messages.app; reading has no AppleScript surface worth
using, so this opens ~/Library/Messages/chat.db read-only — the same approach
the `imsg` CLI takes. Requires Full Disk Access for whichever process runs
Odysseus; without it the open fails and we say so.

Read-only, never immutable: Messages keeps recent rows in a WAL sidecar, and
immutable mode would silently miss them. The DB is copied nowhere and never
written; this module has no mutating paths at all.

Contract matches the rest of agent_tools: async execute(content, ctx) -> dict.
"""
import json
import os
import sqlite3
import sys
from typing import Dict, List, Optional

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# chat.db stores dates as nanoseconds since 2001-01-01 (Apple epoch).
_APPLE_EPOCH_OFFSET = 978307200

_FDA_HINT = (
    "Can't read the Messages database. Grant Full Disk Access to Odysseus "
    "under System Settings → Privacy & Security → Full Disk Access, then "
    "relaunch the app."
)


def _apple_time_to_iso(raw) -> str:
    """Apple epoch (ns, or seconds on very old macOS) -> ISO-8601 local time."""
    import datetime
    try:
        v = int(raw or 0)
    except (TypeError, ValueError):
        return ""
    if not v:
        return ""
    secs = v / 1e9 if v > 1e11 else float(v)   # ns on 10.13+, seconds before
    try:
        return datetime.datetime.fromtimestamp(secs + _APPLE_EPOCH_OFFSET).isoformat(" ", "seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _decode_attributed_body(blob) -> str:
    """Pull plain text out of an NSAttributedString archive.

    Modern macOS leaves `message.text` NULL and stores the body in
    `attributedBody` (a typedstream blob). Full unarchiving needs pyobjc, so
    extract the string payload directly — the text follows the
    NSString/NSMutableString marker, length-prefixed.
    """
    if not blob:
        return ""
    try:
        raw = bytes(blob)
    except Exception:
        return ""
    marker = raw.find(b"NSString")
    if marker == -1:
        return ""
    i = marker + len(b"NSString")
    # Skip the class-ref bookkeeping that precedes the payload.
    while i < len(raw) and raw[i] in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x2B, 0x81, 0x84, 0x94, 0x95, 0x96):
        i += 1
    if i >= len(raw):
        return ""
    length = raw[i]
    i += 1
    if length == 0x81:            # 2-byte length prefix
        if i + 1 >= len(raw):
            return ""
        length = int.from_bytes(raw[i:i + 2], "little")
        i += 2
    try:
        return raw[i:i + length].decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _connect() -> sqlite3.Connection:
    if not os.path.exists(CHAT_DB):
        raise FileNotFoundError(f"No Messages database at {CHAT_DB}")
    # mode=ro (not immutable) so WAL-backed recent messages are visible.
    conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _body(row) -> str:
    txt = (row["text"] or "").strip() if "text" in row.keys() else ""
    if txt:
        return txt
    if "attributedBody" in row.keys():
        return _decode_attributed_body(row["attributedBody"])
    return ""


def list_chats(limit: int = 20) -> List[dict]:
    """Most recently active conversations."""
    sql = """
        SELECT c.ROWID AS cid,
               COALESCE(NULLIF(c.display_name,''), c.chat_identifier) AS name,
               c.chat_identifier AS ident,
               MAX(m.date) AS last_date
        FROM chat c
        JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        JOIN message m ON m.ROWID = cmj.message_id
        GROUP BY c.ROWID
        ORDER BY last_date DESC
        LIMIT ?
    """
    with _connect() as conn:
        rows = conn.execute(sql, (max(1, min(int(limit), 100)),)).fetchall()
    return [{"chat": r["name"] or r["ident"], "handle": r["ident"],
             "last": _apple_time_to_iso(r["last_date"])} for r in rows]


def read_conversation(who: str, limit: int = 25) -> List[dict]:
    """Recent messages with a person/group, oldest-first within the window."""
    sql = """
        SELECT m.text, m.attributedBody, m.date, m.is_from_me, h.id AS handle
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE c.chat_identifier LIKE ? OR c.display_name LIKE ? OR h.id LIKE ?
        ORDER BY m.date DESC
        LIMIT ?
    """
    like = f"%{who}%"
    with _connect() as conn:
        rows = conn.execute(sql, (like, like, like,
                                  max(1, min(int(limit), 200)))).fetchall()
    out = [{"from": "me" if r["is_from_me"] else (r["handle"] or who),
            "text": _body(r), "at": _apple_time_to_iso(r["date"])} for r in rows]
    out = [m for m in out if m["text"]]
    out.reverse()               # chronological reads better in a transcript
    return out


def search_messages(query: str, limit: int = 25) -> List[dict]:
    """Full-text-ish search over message bodies.

    Only matches rows with a populated `text` column: attributedBody is a
    binary archive, so SQL can't search it. Recent messages whose body lives
    only in attributedBody won't match — read_conversation still shows them.
    """
    sql = """
        SELECT m.text, m.attributedBody, m.date, m.is_from_me, h.id AS handle
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE m.text LIKE ?
        ORDER BY m.date DESC
        LIMIT ?
    """
    with _connect() as conn:
        rows = conn.execute(sql, (f"%{query}%",
                                  max(1, min(int(limit), 100)))).fetchall()
    return [{"from": "me" if r["is_from_me"] else (r["handle"] or "?"),
             "text": _body(r), "at": _apple_time_to_iso(r["date"])} for r in rows]


def unread_messages(limit: int = 25) -> List[dict]:
    """Incoming messages still marked unread."""
    sql = """
        SELECT m.text, m.attributedBody, m.date, h.id AS handle
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE m.is_read = 0 AND m.is_from_me = 0
        ORDER BY m.date DESC
        LIMIT ?
    """
    with _connect() as conn:
        rows = conn.execute(sql, (max(1, min(int(limit), 100)),)).fetchall()
    out = [{"from": r["handle"] or "?", "text": _body(r),
            "at": _apple_time_to_iso(r["date"])} for r in rows]
    return [m for m in out if m["text"]]


def _fmt(rows: List[dict], empty: str) -> str:
    if not rows:
        return empty
    lines = []
    for r in rows:
        if "chat" in r:
            lines.append(f"- {r['chat']}  (last: {r['last'] or 'unknown'})")
        else:
            lines.append(f"[{r['at']}] {r['from']}: {r['text']}")
    return "\n".join(lines)


class ReadMessagesTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        raw = (content or "").strip()
        try:
            args = json.loads(raw) if raw.startswith("{") else {"action": raw or "chats"}
        except json.JSONDecodeError:
            return {"error": 'read_imessages: arguments must be JSON, e.g. '
                             '{"action":"conversation","who":"Michaela"}',
                    "exit_code": 1}
        if sys.platform != "darwin":
            return {"error": "read_imessages is only available on macOS.", "exit_code": 1}

        action = str(args.get("action") or "chats").strip().lower()
        limit = args.get("limit") or 25
        who = str(args.get("who") or "").strip()
        query = str(args.get("query") or "").strip()

        try:
            if action in ("chats", "list", "conversations"):
                return {"output": _fmt(list_chats(limit), "No conversations found."),
                        "exit_code": 0}
            if action in ("conversation", "read", "thread"):
                if not who:
                    return {"error": "read_imessages: 'who' is required for a conversation.",
                            "exit_code": 1}
                return {"output": _fmt(read_conversation(who, limit),
                                       f"No messages found with '{who}'."),
                        "exit_code": 0}
            if action == "search":
                if not query:
                    return {"error": "read_imessages: 'query' is required for search.",
                            "exit_code": 1}
                return {"output": _fmt(search_messages(query, limit),
                                       f"No messages matching '{query}'."),
                        "exit_code": 0}
            if action == "unread":
                return {"output": _fmt(unread_messages(limit), "No unread messages."),
                        "exit_code": 0}
            return {"error": f"read_imessages: unknown action '{action}'. "
                             "Valid: chats, conversation, search, unread.",
                    "exit_code": 1}
        except FileNotFoundError as e:
            return {"error": f"read_imessages: {e}", "exit_code": 1}
        except sqlite3.OperationalError as e:
            # "unable to open database file" = the Full Disk Access grant.
            return {"error": f"read_imessages: {_FDA_HINT}\n\nDetail: {e}", "exit_code": 1}
        except Exception as e:
            return {"error": f"read_imessages: {type(e).__name__}: {e}", "exit_code": 1}
