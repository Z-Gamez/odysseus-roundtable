"""Open a URL in the user's REAL browser (their logins, cookies, bookmarks).

Distinct from the `browser` tool: that drives an automation session (CDP for
Chrome, WebDriver for Safari) so the agent can click/read/fill. Safari's
WebDriver session is ISOLATED by Apple's design — it has none of the user's
cookies or logins — so "open Safari and go to youtube" through it lands on a
logged-out page. For "just open it in my browser", shell out to the OS opener
instead: macOS `open -a Safari <url>` uses the real Safari profile and needs no
Automation/TCC permission. Non-macOS falls back to the default browser.

Contract matches the rest of agent_tools: async execute(content, ctx) -> dict.
"""
import json
import re
import subprocess
import sys
from typing import Optional
from urllib.parse import urlparse


def _normalize_url(raw: str) -> Optional[str]:
    """Return an http(s) URL, or None if it isn't safe to open.

    A bare host gets https://. Only http/https are allowed — `open` would hand
    other schemes (file:, javascript:, tel:, custom app schemes) to whatever
    app claims them, which we don't want the agent triggering. "host:port"
    (a colon followed by a port number) is NOT a scheme and is kept.
    """
    u = (raw or "").strip()
    if not u:
        return None
    if "://" in u:
        if u.split("://", 1)[0].lower() not in ("http", "https"):
            return None
    else:
        # An opaque "scheme:rest" (javascript:, tel:, mailto:) has a non-digit
        # right after the colon; "localhost:8080" has a digit (a port) — keep it.
        lead = u.split("/", 1)[0]
        m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):(?!\d)", lead)
        if m and m.group(1).lower() not in ("http", "https"):
            return None
        u = "https://" + u
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return u


class OpenUrlTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        raw = (content or "").strip()
        try:
            args = json.loads(raw) if raw.startswith("{") else {"url": raw}
        except json.JSONDecodeError:
            return {"error": 'open_in_safari: arguments must be JSON, e.g. '
                             '{"url":"youtube.com"}', "exit_code": 1}
        url = _normalize_url(str(args.get("url") or ""))
        if not url:
            return {"error": "open_in_safari: a valid http(s) 'url' is required.",
                    "exit_code": 1}

        if sys.platform == "darwin":
            try:
                # url is passed as an argv element (never a shell string), and is
                # validated to http/https above, so nothing can be injected.
                proc = subprocess.run(["open", "-a", "Safari", url],
                                      capture_output=True, text=True, timeout=15)
            except FileNotFoundError:
                return {"error": "open_in_safari: `open` not found — is this macOS?",
                        "exit_code": 1}
            except subprocess.TimeoutExpired:
                return {"error": "open_in_safari: Safari did not respond within 15s.",
                        "exit_code": 1}
            if proc.returncode != 0:
                err = (proc.stderr or "").strip() or "open failed"
                return {"error": f"open_in_safari: {err}", "exit_code": 1}
            return {"output": f"Opened {url} in Safari (your real profile — logged-in).",
                    "exit_code": 0}

        # Non-macOS: open in the platform default browser.
        try:
            import webbrowser
            webbrowser.open(url)
            return {"output": f"Opened {url} in the default browser.", "exit_code": 0}
        except Exception as e:
            return {"error": f"open_in_safari: could not open a browser: {e}",
                    "exit_code": 1}
