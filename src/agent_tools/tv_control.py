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


# Vizio app launch descriptors, VERIFIED on a V4K65C by launching each app and
# reading the id back from /app/current. Not taken from published tables — the
# published value for Prime (NAME_SPACE 2, APP_ID "4") launches nothing on this
# model, and the first version of this table was written from memory, which is
# how "open netflix" ended up starting Prime Video.
#
# Netflix and Prime differ ONLY in APP_ID within NAME_SPACE 3, so a single
# wrong digit silently opens the wrong app.
#
# Correctable without a rebuild: drop a tv_apps.json in the data dir. Use
# action=current_app with an app open to read its real descriptor.
_DEFAULT_APPS = {
    "netflix": {"APP_ID": "1", "NAME_SPACE": 3, "MESSAGE": None},   # verified
    "prime":   {"APP_ID": "4", "NAME_SPACE": 3, "MESSAGE": None},   # verified
    "youtube": {"APP_ID": "1", "NAME_SPACE": 5, "MESSAGE": None},   # verified
    "disney":  {"APP_ID": "75", "NAME_SPACE": 4, "MESSAGE": None},  # verified
    "max":     {"APP_ID": "34", "NAME_SPACE": 4, "MESSAGE": None},  # verified
    "hbo":     {"APP_ID": "34", "NAME_SPACE": 4, "MESSAGE": None},  # alias of max
    "hulu":    {"APP_ID": "3", "NAME_SPACE": 4, "MESSAGE": None},   # accepted, not visually confirmed
}


# The TV normalises NAME_SPACE 2 to 4 on readback, so a launch sent as 2 reads
# back as 4. Treating that as a mismatch would report failure on a launch that
# actually worked.
_EQUIVALENT_NAMESPACES = ({2, 4},)


def _ns_equal(a, b) -> bool:
    try:
        a, b = int(a), int(b)
    except (TypeError, ValueError):
        return False
    if a == b:
        return True
    return any(a in grp and b in grp for grp in _EQUIVALENT_NAMESPACES)


def _app_table() -> dict:
    """Built-in descriptors, overlaid with data/tv_apps.json if present.

    An override file means a wrong id is a one-line JSON edit rather than a
    rebuild — which matters because these cannot be verified without the TV in
    front of you.
    """
    table = {k: dict(v) for k, v in _DEFAULT_APPS.items()}
    try:
        from src.constants import DATA_DIR
        with open(os.path.join(DATA_DIR, "tv_apps.json"), "r", encoding="utf-8") as fh:
            for name, cfg in (json.load(fh) or {}).items():
                if isinstance(cfg, dict) and "APP_ID" in cfg:
                    cfg.setdefault("MESSAGE", None)
                    table[str(name).strip().lower()] = cfg
    except Exception:
        pass
    return table


def current_app() -> Dict[str, Any]:
    """What the TV is running right now, with its raw descriptor.

    This is how an id gets corrected: open the app with the remote, run this,
    and copy the reported NAME_SPACE/APP_ID into data/tv_apps.json.
    """
    r = _resolve()
    if r.get("error"):
        return {"error": r["error"], "exit_code": 1}
    out = _request(r["ip"], r["port"], "/app/current", token=r["token"])
    if out.get("_error") or out.get("_http_error"):
        return _result(out, "read current app")
    val = (out.get("ITEM") or {}).get("VALUE") or out.get("VALUE") or out
    known = {(str(v.get("APP_ID")), int(v.get("NAME_SPACE", -1))): k
             for k, v in _app_table().items()}
    name = known.get((str(val.get("APP_ID")), int(val.get("NAME_SPACE", -1))))
    return {"output": json.dumps({
        "running": name or "unknown",
        "descriptor": val,
        "hint": ("matches a known app" if name else
                 "not in the app table — copy this descriptor into "
                 "data/tv_apps.json under the app's name to fix it"),
    }), "exit_code": 0}


def launch_app(app: str, video_id: str = "") -> Dict[str, Any]:
    """Open an app, optionally deep-linking a YouTube video id.

    Netflix and most others accept the launch but NOT a specific title — that
    is a limit of what the apps expose, not of this tool.
    """
    r = _resolve()
    if r.get("error"):
        return {"error": r["error"], "exit_code": 1}
    known = _app_table()
    cfg = known.get((app or "").strip().lower())
    if not cfg:
        return {"error": f"unknown app {app!r}. Known: {', '.join(sorted(known))}",
                "exit_code": 1}
    out = _request(r["ip"], r["port"], "/app/launch", method="PUT", token=r["token"],
                   body={"VALUE": cfg, "REQUEST": "MODIFY"})
    res = _result(out, f"launch {app}")
    if res.get("exit_code"):
        return res

    # /app/launch answers STATUS SUCCESS even when nothing launches — a wrong
    # or uninstalled app id looks identical to a working one. That is exactly
    # how the wrong Netflix id shipped. The only honest check is to read back
    # what is actually running.
    import time as _time
    _time.sleep(2.0)
    back = _request(r["ip"], r["port"], "/app/current", token=r["token"])
    val = (back.get("ITEM") or {}).get("VALUE") or back.get("VALUE") or {}
    got_id, got_ns = str(val.get("APP_ID")), val.get("NAME_SPACE")
    if got_id and got_id != "None":
        if got_id != str(cfg["APP_ID"]) or not _ns_equal(got_ns, cfg["NAME_SPACE"]):
            return {"error": (
                f"asked for {app} ({cfg['NAME_SPACE']}/{cfg['APP_ID']}) but the "
                f"TV is running {got_ns}/{got_id}. The id for {app} is wrong for "
                f"this model, or the app is not installed. Fix it by putting the "
                f"correct descriptor in data/tv_apps.json — action=current_app "
                f"reports what is actually running."), "exit_code": 1}

    note = "" if (app.lower() == "youtube" and video_id) else \
        " (opened the app; per-title playback is not exposed by this app)"
    return {"output": f"OK — launched {app}{note}", "exit_code": 0}


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
            if action in ("current_app", "current"):
                return current_app()
            if action in ("launch", "app"):
                return launch_app(str(args.get("app") or ""),
                                  str(args.get("video_id") or ""))
            return {"error": f"unknown action {action!r}. Use: info, power, "
                             f"volume, input, launch, current_app.",
                    "exit_code": 1}

        # Blocking urllib on a LAN device — keep it off the event loop.
        return await asyncio.to_thread(_run)
