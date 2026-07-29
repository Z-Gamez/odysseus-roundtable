"""Control a Vizio SmartCast TV over its local HTTP API.

Built native rather than as an MCP server on purpose. The four remaining MCP
servers exist because they carry hundreds of lines of unique IMAP/HTTP/manager
logic; this is a handful of requests to a device on the LAN. Adding a fifth
stdio server would also inherit the exact fragility that just broke all four in
a shipped build — an unpinned `mcp` release removing the decorator API they are
built on. In-process has neither problem.

WHAT THIS CAN AND CANNOT DO, because the gap surprises people:
  * Reliable: power, input switching, volume, launching an app, key presses.
  * YouTube deep links work, because the app accepts a video id.
  * Netflix will open, but "play episode 3" will not. Netflix does not expose
    per-title launch, and no amount of API here changes that.

Pairing: commands need a token the TV issues once via an on-screen PIN. Reads
like device info work without it. The token is stored in the data dir, never in
the repo.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

_PORTS = (7345, 9000)
_TIMEOUT = 8.0


def _token_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "tv_token.json")


def _load_creds() -> Dict[str, Any]:
    try:
        with open(_token_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _ctx() -> ssl.SSLContext:
    # SmartCast ships a self-signed cert baked into the firmware. Verification
    # is off deliberately: this is a fixed device on the LAN reached by IP, and
    # there is no CA that could ever vouch for it. Never reuse this context for
    # anything reachable off the local network.
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _request(ip: str, port: int, path: str, *, method: str = "GET",
             body: Optional[dict] = None, token: str = "") -> Dict[str, Any]:
    url = f"https://{ip}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("AUTH", token)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=_ctx()) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_detail": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return {"_error": str(e)}


def _resolve() -> Dict[str, Any]:
    """Stored ip/port/token, or an actionable error naming the missing step."""
    c = _load_creds()
    ip = (c.get("ip") or "").strip()
    if not ip:
        return {"error": "No TV configured. Run the pairing setup first — it "
                         "stores the TV's address and auth token."}
    return {"ip": ip, "port": int(c.get("port") or _PORTS[0]),
            "token": c.get("token") or ""}


# ── operations ──────────────────────────────────────────────────────────────

def device_info(ip: str = "", port: int = 0) -> Dict[str, Any]:
    """Model, cast name and input list. Works unpaired."""
    if not ip:
        r = _resolve()
        if r.get("error"):
            return r
        ip, port = r["ip"], r["port"]
    for p in ([port] if port else list(_PORTS)):
        out = _request(ip, p, "/state/device/deviceinfo")
        if not out.get("_error") and not out.get("_http_error"):
            v = ((out.get("ITEMS") or [{}])[0].get("VALUE") or {})
            return {"output": json.dumps({
                "model": v.get("MODEL_NAME"), "name": v.get("CAST_NAME"),
                "api": v.get("API_VERSION"), "inputs": v.get("INPUTS"),
                "ip": ip, "port": p}), "exit_code": 0}
    return {"error": f"No SmartCast API answered on {ip}. Is the TV powered on "
                     f"and on this network?", "exit_code": 1}


def power(state: str) -> Dict[str, Any]:
    r = _resolve()
    if r.get("error"):
        return {"error": r["error"], "exit_code": 1}
    key = {"on": 1, "off": 0, "toggle": 2}.get((state or "toggle").lower(), 2)
    out = _request(r["ip"], r["port"], "/key_command/", method="PUT", token=r["token"],
                   body={"KEYLIST": [{"CODESET": 11, "CODE": key, "ACTION": "KEYPRESS"}]})
    return _result(out, f"power {state}")


def volume(direction: str, steps: int = 1) -> Dict[str, Any]:
    r = _resolve()
    if r.get("error"):
        return {"error": r["error"], "exit_code": 1}
    code = {"up": 1, "down": 0, "mute": 4}.get((direction or "").lower())
    if code is None:
        return {"error": "direction must be up, down or mute", "exit_code": 1}
    keys = [{"CODESET": 5, "CODE": code, "ACTION": "KEYPRESS"}
            for _ in range(max(1, min(int(steps or 1), 20)))]
    return _result(_request(r["ip"], r["port"], "/key_command/", method="PUT",
                            token=r["token"], body={"KEYLIST": keys}),
                   f"volume {direction} x{len(keys)}")


def set_input(name: str) -> Dict[str, Any]:
    """Switch input. Names come from device_info's `inputs` list."""
    r = _resolve()
    if r.get("error"):
        return {"error": r["error"], "exit_code": 1}
    cur = _request(r["ip"], r["port"], "/menu_native/dynamic/tv_settings/devices/current_input",
                   token=r["token"])
    items = cur.get("ITEMS") or []
    if not items:
        return {"error": "could not read the current input (is the TV paired?)",
                "exit_code": 1}
    it = items[0]
    out = _request(r["ip"], r["port"],
                   "/menu_native/dynamic/tv_settings/devices/current_input",
                   method="PUT", token=r["token"],
                   body={"REQUEST": "MODIFY", "HASHVAL": it.get("HASHVAL"),
                         "VALUE": name})
    return _result(out, f"input -> {name}")


def launch_app(app: str, video_id: str = "") -> Dict[str, Any]:
    """Open an app, optionally deep-linking a YouTube video id.

    Netflix and most others accept the launch but NOT a specific title — that
    is a limit of what the apps expose, not of this tool.
    """
    r = _resolve()
    if r.get("error"):
        return {"error": r["error"], "exit_code": 1}
    known = {
        "youtube":  {"APP_ID": "1", "NAME_SPACE": 5, "MESSAGE": video_id or None},
        "netflix":  {"APP_ID": "3", "NAME_SPACE": 3, "MESSAGE": None},
        "prime":    {"APP_ID": "4", "NAME_SPACE": 3, "MESSAGE": None},
        "hulu":     {"APP_ID": "8", "NAME_SPACE": 2, "MESSAGE": None},
        "disney":   {"APP_ID": "75", "NAME_SPACE": 4, "MESSAGE": None},
    }
    cfg = known.get((app or "").strip().lower())
    if not cfg:
        return {"error": f"unknown app {app!r}. Known: {', '.join(sorted(known))}",
                "exit_code": 1}
    out = _request(r["ip"], r["port"], "/app/launch", method="PUT", token=r["token"],
                   body={"VALUE": cfg, "REQUEST": "MODIFY"})
    note = "" if (app.lower() == "youtube" and video_id) else \
        " (opened the app; per-title playback is not exposed by this app)"
    return _result(out, f"launch {app}{note}")


def _result(out: Dict[str, Any], what: str) -> Dict[str, Any]:
    if out.get("_error"):
        return {"error": f"{what} failed: {out['_error']}", "exit_code": 1}
    if out.get("_http_error"):
        code = out["_http_error"]
        if code in (401, 403):
            return {"error": f"{what} rejected: the TV needs pairing, or the "
                             f"stored token is no longer valid. Re-run pairing.",
                    "exit_code": 1}
        return {"error": f"{what} failed (HTTP {code}): {out.get('_detail','')}",
                "exit_code": 1}
    status = (out.get("STATUS") or {}).get("RESULT", "")
    if status and status.upper() != "SUCCESS":
        return {"error": f"{what} refused by the TV: {status}", "exit_code": 1}
    return {"output": f"OK — {what}", "exit_code": 0}


# ── the agent-facing tool ───────────────────────────────────────────────────

class TvControlTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        import asyncio
        try:
            args = json.loads(content) if content.strip().startswith("{") else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        action = str(args.get("action") or "").strip().lower()

        def _run():
            if action in ("info", "device_info", ""):
                return device_info()
            if action == "power":
                return power(str(args.get("state") or "toggle"))
            if action == "volume":
                return volume(str(args.get("direction") or ""),
                              int(args.get("steps") or 1))
            if action == "input":
                return set_input(str(args.get("name") or ""))
            if action in ("launch", "app"):
                return launch_app(str(args.get("app") or ""),
                                  str(args.get("video_id") or ""))
            return {"error": f"unknown action {action!r}. Use: info, power, "
                             f"volume, input, launch.", "exit_code": 1}

        # Blocking urllib on a LAN device — keep it off the event loop.
        return await asyncio.to_thread(_run)
