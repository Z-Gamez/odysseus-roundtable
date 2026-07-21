"""Drive the user's real Safari via Apple's safaridriver (W3C WebDriver).

Safari cannot be driven over the Chrome DevTools Protocol the Chrome backend
uses, so this speaks WebDriver (HTTP+JSON) to a `safaridriver -p <port>`
process. It exposes a `_SafariPage` that quacks like the subset of the
Playwright page API `BrowserTool._dispatch` calls — so the action logic in
browser_tools.py is shared, not duplicated.

One-time user setup on the Mac (surfaced in the error text if missing):
  1. `safaridriver --enable`   (once; may prompt for your password)
  2. Safari → Settings → Advanced → "Show Develop menu"
  3. Safari → Develop → "Allow Remote Automation"
"""
import asyncio
import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, List, Optional

import httpx

logger = logging.getLogger(__name__)

# WebDriver's "web element" key (W3C constant) and the Unicode PUA codes for
# non-printing keys sent through the value/actions endpoints.
_WEB_ELEMENT = "element-6066-11e4-a52e-4f735466cecf"
_WD_KEYS = {
    "enter": "", "return": "", "tab": "", "escape": "",
    "esc": "", "backspace": "", "delete": "", "space": " ",
    "arrowdown": "", "arrowup": "", "arrowleft": "",
    "arrowright": "", "pagedown": "", "pageup": "",
}


class SafariUnavailable(RuntimeError):
    """safaridriver missing or Remote Automation not enabled."""


class _WD:
    """Minimal async W3C WebDriver client (just what the page adapter needs)."""

    def __init__(self, base: str, session_id: str, client: httpx.AsyncClient):
        self.base = base.rstrip("/")
        self.sid = session_id
        self.c = client

    @classmethod
    async def create(cls, port: int) -> "_WD":
        base = f"http://127.0.0.1:{port}"
        client = httpx.AsyncClient(timeout=45.0)
        body = {"capabilities": {"alwaysMatch": {"browserName": "safari"}}}
        r = await client.post(f"{base}/session", json=body)
        data = r.json()
        if r.status_code != 200 or "value" not in data or "sessionId" not in data.get("value", {}):
            await client.aclose()
            msg = ((data.get("value") or {}).get("message")
                   if isinstance(data, dict) else None) or r.text
            raise SafariUnavailable(
                "safaridriver refused a session. Enable it once: run "
                "`safaridriver --enable` in Terminal, then Safari → Develop → "
                f"'Allow Remote Automation'. Detail: {msg[:200]}")
        return cls(base, data["value"]["sessionId"], client)

    async def _post(self, path: str, body: Optional[dict] = None) -> Any:
        r = await self.c.post(f"{self.base}/session/{self.sid}{path}", json=body or {})
        return self._value(r)

    async def _get(self, path: str) -> Any:
        r = await self.c.get(f"{self.base}/session/{self.sid}{path}")
        return self._value(r)

    @staticmethod
    def _value(r: httpx.Response) -> Any:
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
            return None
        if isinstance(data, dict) and isinstance(data.get("value"), dict) \
                and "error" in data["value"]:
            raise RuntimeError(data["value"].get("message") or data["value"]["error"])
        return data.get("value") if isinstance(data, dict) else None

    async def navigate(self, url: str) -> None:
        await self._post("/url", {"url": url})

    async def current_url(self) -> str:
        return await self._get("/url") or ""

    async def title(self) -> str:
        return await self._get("/title") or ""

    async def execute(self, arrow_js: str) -> Any:
        # arrow_js is a Playwright-style "() => expr". WebDriver wraps the body
        # as a function, so call the arrow with the (empty) arguments list.
        script = "return (" + arrow_js + ").apply(null, arguments);"
        return await self._post("/execute/sync", {"script": script, "args": []})

    async def find(self, css: str) -> Optional[str]:
        try:
            el = await self._post("/element", {"using": "css selector", "value": css})
        except Exception:
            return None
        return el.get(_WEB_ELEMENT) if isinstance(el, dict) else None

    async def click(self, elid: str) -> None:
        await self._post(f"/element/{elid}/click")

    async def clear(self, elid: str) -> None:
        await self._post(f"/element/{elid}/clear")

    async def send_keys(self, elid: str, text: str) -> None:
        await self._post(f"/element/{elid}/value", {"text": text})

    async def back(self) -> None:
        await self._post("/back")

    async def forward(self) -> None:
        await self._post("/forward")

    async def screenshot_b64(self) -> str:
        return await self._get("/screenshot") or ""

    async def key(self, wd_key: str) -> None:
        await self._post("/actions", {"actions": [{
            "type": "key", "id": "kb",
            "actions": [{"type": "keyDown", "value": wd_key},
                        {"type": "keyUp", "value": wd_key}],
        }]})

    async def quit(self) -> None:
        try:
            await self.c.delete(f"{self.base}/session/{self.sid}")
        finally:
            await self.c.aclose()


class _SafariLocator:
    def __init__(self, wd: _WD, css: str, page: "_SafariPage"):
        self._wd, self._css, self._page = wd, css, page

    async def inner_text(self, timeout: float = 0) -> str:
        js = "() => { const e = document.querySelector(%s); return e ? e.innerText : ''; }" % json.dumps(self._css)
        return (await self._wd.execute(js)) or ""

    async def click(self, timeout: float = 0) -> None:
        elid = await self._wd.find(self._css)
        if not elid:
            raise RuntimeError(f"element not found: {self._css}")
        await self._wd.click(elid)
        await self._page._refresh()

    async def fill(self, text: str, timeout: float = 0) -> None:
        elid = await self._wd.find(self._css)
        if not elid:
            raise RuntimeError(f"element not found: {self._css}")
        await self._wd.clear(elid)
        await self._wd.send_keys(elid, str(text))

    async def press(self, key: str) -> None:
        elid = await self._wd.find(self._css)
        if not elid:
            raise RuntimeError(f"element not found: {self._css}")
        await self._wd.send_keys(elid, _WD_KEYS.get(key.lower(), key))
        await self._page._refresh()


class _SafariKeyboard:
    def __init__(self, wd: _WD, page: "_SafariPage"):
        self._wd, self._page = wd, page

    async def press(self, key: str) -> None:
        await self._wd.key(_WD_KEYS.get(key.lower(), key))
        await self._page._refresh()


class _SafariPage:
    """Playwright-page-shaped adapter over a WebDriver session."""

    def __init__(self, wd: _WD):
        self._wd = wd
        self._url = ""
        self.keyboard = _SafariKeyboard(wd, self)

    async def _refresh(self) -> None:
        try:
            self._url = await self._wd.current_url()
        except Exception:
            pass

    @property
    def url(self) -> str:
        return self._url

    async def title(self) -> str:
        try:
            return await self._wd.title()
        except Exception:
            return ""

    async def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> None:
        await self._wd.navigate(url)
        await self._refresh()

    async def bring_to_front(self) -> None:
        return None  # Safari drives a single automation window

    async def evaluate(self, arrow_js: str, *args) -> Any:
        return await self._wd.execute(arrow_js)

    def locator(self, css: str) -> _SafariLocator:
        return _SafariLocator(self._wd, css, self)

    async def wait_for_timeout(self, ms: int) -> None:
        await asyncio.sleep(max(0, int(ms)) / 1000.0)

    async def go_back(self, wait_until: str = "") -> None:
        await self._wd.back()
        await self._refresh()

    async def go_forward(self, wait_until: str = "") -> None:
        await self._wd.forward()
        await self._refresh()

    async def screenshot(self, path: str = "", full_page: bool = False) -> None:
        b64 = await self._wd.screenshot_b64()
        if path and b64:
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))

    async def wait_for_selector(self, selector: str, timeout: int = 10000) -> None:
        js = "() => !!document.querySelector(%s)" % json.dumps(selector)
        deadline = time.time() + max(1, int(timeout)) / 1000.0
        while time.time() < deadline:
            if await self._wd.execute(js):
                return
            await asyncio.sleep(0.25)
        raise RuntimeError(f"timeout waiting for selector: {selector}")


class SafariSession:
    """Owns the safaridriver process + one reused WebDriver page."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._wd: Optional[_WD] = None
        self._page: Optional[_SafariPage] = None

    def _spawn_driver(self, port: int) -> None:
        exe = shutil.which("safaridriver") or "/usr/bin/safaridriver"
        if not os.path.exists(exe):
            raise SafariUnavailable("safaridriver not found — this backend needs macOS Safari.")
        self._proc = subprocess.Popen([exe, "-p", str(port)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait for the port to accept connections.
        for _ in range(40):
            s = socket.socket()
            s.settimeout(0.3)
            try:
                s.connect(("127.0.0.1", port))
                s.close()
                return
            except Exception:
                s.close()
                time.sleep(0.25)
        raise SafariUnavailable("safaridriver did not start listening.")

    async def page(self, port: int = 0) -> _SafariPage:
        if sys.platform != "darwin":
            raise SafariUnavailable("The Safari backend is only available on macOS.")
        if self._page is not None and self._proc and self._proc.poll() is None:
            return self._page
        port = port or _free_port()
        await asyncio.to_thread(self._spawn_driver, port)
        self._wd = await _WD.create(port)
        self._page = _SafariPage(self._wd)
        await self._page._refresh()
        return self._page

    async def reset(self) -> None:
        try:
            if self._wd:
                await self._wd.quit()
        except Exception:
            pass
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass
        self._wd = self._page = self._proc = None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_SAFARI = SafariSession()
