"""Browser harness — let the agent drive the user's real Chrome from chat.

Attaches to a Chrome already running with `--remote-debugging-port` via Playwright
over CDP, so actions happen in the user's actual browser (their tabs, their logins).
One consolidated `browser` tool exposes discrete actions. High-stakes actions
(submitting forms, buying, sending, deleting) are gated behind an explicit
`confirm: true` so the agent must get the user's OK first.

Contract matches the rest of agent_tools: `async execute(content, ctx) -> dict`
returning {"output": str, "exit_code": 0} or {"error": str, "exit_code": 1}.
"""
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

from src.constants import MAX_OUTPUT_CHARS

logger = logging.getLogger(__name__)

_IS_MAC = sys.platform == "darwin"

# Words that signal a high-stakes, hard-to-undo action. A click whose target text
# matches these (or a real form submit) requires confirm=true.
_RISKY_WORDS = (
    "buy", "purchase", "order", "checkout", "place order", "pay", "payment",
    "subscribe", "delete", "remove", "send", "submit", "confirm", "transfer",
    "withdraw", "deactivate", "close account", "sign out", "log out", "book now",
    "reserve", "donate", "bid", "post", "publish", "apply now",
)

_SNAPSHOT_JS = """
() => {
  const sel = 'a,button,input,textarea,select,[role=button],[role=link],[role=tab],[onclick],[contenteditable=true]';
  const els = Array.from(document.querySelectorAll(sel));
  const out = [];
  let n = 0;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;          // skip hidden
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const ref = 'e' + (++n);
    el.setAttribute('data-odys-ref', ref);
    let label = (el.innerText || el.value || el.placeholder ||
                 el.getAttribute('aria-label') || el.name || el.alt || '').trim();
    label = label.replace(/\\s+/g, ' ').slice(0, 90);
    const t = el.type ? (el.tagName.toLowerCase() + '[' + el.type + ']') : el.tagName.toLowerCase();
    out.push({ ref, tag: t, text: label });
    if (n >= 200) break;
  }
  return out;
}
"""


def _port_from_url(url: str) -> int:
    try:
        from urllib.parse import urlparse
        return urlparse(url).port or 9222
    except Exception:
        return 9222


# The user's real profile, and the automation clone of it. Chrome 136+ silently
# IGNORES --remote-debugging-port when pointed at the default User Data dir, so
# "just open my Chrome with CDP" is impossible on modern Chrome. Instead the
# real profile is mirrored into a clone (bookmarks, cookies, logins, extensions
# — caches excluded) and refreshed on every launch. Same machine + same
# chrome.exe means the encrypted cookie/password stores decrypt in the copy.
if _IS_MAC:
    _REAL_USER_DATA = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    _CLONE_USER_DATA = os.path.expanduser("~/Library/Application Support/Google/OdysseusChrome")
    _AUTOMATION_USER_DATA = os.path.expanduser("~/Library/Application Support/Google/OdysseusAutomation")
    _CHROME_CANDIDATES = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
else:
    _REAL_USER_DATA = os.path.expandvars(r"%LocalAppData%\Google\Chrome\User Data")
    _CLONE_USER_DATA = os.path.expandvars(r"%LocalAppData%\Google\Chrome\OdysseusChrome")
    _AUTOMATION_USER_DATA = os.path.expandvars(r"%LocalAppData%\Google\Chrome\OdysseusAutomation")
    _CHROME_CANDIDATES = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
_CLONE_EXCLUDE_DIRS = (
    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "Media Cache", "Service Worker",
    "Crashpad", "CrashpadMetrics", "Snapshots", "component_crx_cache",
    "extensions_crx_cache", "OptimizationGuidePredictionModels",
)


def _sync_profile_clone(timeout_s: int = 300) -> bool:
    """Seed-once, persist-after profile clone.

    First launch: copy the real profile (bookmarks/history/extensions/autofill
    come through; caches excluded). Chrome's app-bound encryption refuses to
    decrypt cookies at a different profile path, so saved LOGINS can never be
    copied — the user signs into sites once IN the clone instead, and those
    logins persist because subsequent launches only refresh Bookmarks (plain
    JSON) rather than re-mirroring. Re-mirroring would wipe the clone's own
    cookie jar every launch. Returns True if the clone looks launchable."""
    import shutil
    import subprocess
    if not os.path.isdir(_REAL_USER_DATA):
        return False
    seeded = os.path.exists(os.path.join(_CLONE_USER_DATA, "Local State"))
    if not seeded:
        if _IS_MAC:
            # No robocopy on macOS — shutil.copytree with an ignore filter for
            # the cache dirs. dirs_exist_ok lets a timed-out seed resume.
            def _ignore(_dir, names):
                return [n for n in names if n in _CLONE_EXCLUDE_DIRS or n == "lockfile"]
            try:
                shutil.copytree(_REAL_USER_DATA, _CLONE_USER_DATA,
                                ignore=_ignore, dirs_exist_ok=True, symlinks=True)
            except Exception as e:
                logger.warning("[browser] profile clone seed failed: %s", e)
        else:
            cmd = ["robocopy", _REAL_USER_DATA, _CLONE_USER_DATA,
                   "/E", "/R:0", "/W:0", "/MT:8", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
                   "/XF", "lockfile", "*.tmp"]
            cmd += ["/XD"] + list(_CLONE_EXCLUDE_DIRS)
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=timeout_s)
            except subprocess.TimeoutExpired:
                logger.warning("[browser] profile clone seed timed out — will resume on next launch")
            except Exception as e:
                logger.warning("[browser] profile clone seed failed: %s", e)
    else:
        # Keep bookmarks current from the real Chrome; touch nothing else.
        for prof in ("Default",):
            src = os.path.join(_REAL_USER_DATA, prof, "Bookmarks")
            dst = os.path.join(_CLONE_USER_DATA, prof, "Bookmarks")
            try:
                if os.path.exists(src):
                    shutil.copy2(src, dst)
            except Exception:
                pass
    # Launchable = the encryption keystore + at least the default profile exist.
    return (os.path.exists(os.path.join(_CLONE_USER_DATA, "Local State"))
            and os.path.isdir(os.path.join(_CLONE_USER_DATA, "Default")))


def _try_launch_chrome(port: int) -> bool:
    """Best-effort: start Chrome with remote debugging if nothing is on the port,
    so the tool self-heals instead of failing when the debug Chrome isn't running.
    Default mode 'my-chrome': launch a freshly-synced CLONE of the user's real
    profile (their logins/bookmarks/extensions). Setting browser_profile_mode to
    'automation' keeps the old blank OdysseusAutomation profile instead.
    Returns True if the debug port is reachable afterward."""
    import socket
    import subprocess
    import time
    import shutil

    def _open() -> bool:
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port)); return True
        except Exception:
            return False
        finally:
            s.close()

    if _open():
        return True
    chrome = next((c for c in _CHROME_CANDIDATES if os.path.exists(c)), None) or shutil.which("chrome")
    if not chrome:
        return False

    try:
        from src.settings import get_setting
        mode = str(get_setting("browser_profile_mode", "my-chrome") or "my-chrome")
    except Exception:
        mode = "my-chrome"
    if mode == "my-chrome" and _sync_profile_clone():
        profile = _CLONE_USER_DATA
    else:
        if mode == "my-chrome":
            logger.warning("[browser] real-profile clone unavailable — falling back to automation profile")
        profile = _AUTOMATION_USER_DATA

    try:
        subprocess.Popen(
            [chrome, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
             "--start-maximized", "--new-window", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if not _IS_MAC else 0,
        )
    except Exception as e:
        logger.warning("[browser] could not launch Chrome: %s", e)
        return False
    for _ in range(24):
        time.sleep(0.5)
        if _open():
            return True
    return False


class _BrowserSession:
    """One shared Playwright CDP connection to the user's Chrome, reused across
    tool calls in the conversation. Never closes the browser — it's the user's."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.lock = asyncio.Lock()

    async def _ensure(self, cdp_url: str) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        from playwright.async_api import async_playwright  # lazy: optional dep
        if self._pw is None:
            self._pw = await async_playwright().start()
        # Attach to the running Chrome. If it isn't up, try to launch it (debug
        # mode, dedicated automation profile) and retry once — so the tool
        # self-heals instead of failing when the debug Chrome was closed.
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(cdp_url)
        except Exception:
            if not await asyncio.to_thread(_try_launch_chrome, _port_from_url(cdp_url)):
                raise
            self._browser = await self._pw.chromium.connect_over_cdp(cdp_url)
        ctxs = self._browser.contexts
        self._context = ctxs[0] if ctxs else await self._browser.new_context()
        pages = [p for p in self._context.pages if not p.is_closed()]
        self._page = pages[-1] if pages else await self._context.new_page()

    async def page(self, cdp_url: str):
        await self._ensure(cdp_url)
        if self._page is None or self._page.is_closed():
            pages = [p for p in self._context.pages if not p.is_closed()]
            self._page = pages[-1] if pages else await self._context.new_page()
        return self._page

    def set_page(self, page) -> None:
        self._page = page


_SESSION = _BrowserSession()

_CONNECT_HINT = (
    "Could not connect to Chrome at {url}. The tool normally launches a clone of "
    "the user's Chrome profile automatically; the first-time profile sync can take "
    "a few minutes, so simply RETRY this call. Manual start: run "
    "scripts/launch-chrome-debug.ps1. Detail: {err}"
)


def _shot_dir() -> str:
    d = os.path.join("data", "browser")
    os.makedirs(d, exist_ok=True)
    return d


def _is_risky(label: str, tag: str, force_submit: bool) -> Optional[str]:
    low = (label or "").lower()
    if force_submit:
        return "submits a form"
    if "[submit]" in (tag or "") or "button[submit]" in (tag or ""):
        return "submit button"
    for w in _RISKY_WORDS:
        if w in low:
            return f"matches high-stakes keyword '{w}'"
    return None


class BrowserTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.settings import get_setting
        cdp_url = get_setting("browser_cdp_url", "http://localhost:9222")
        backend = str(get_setting("browser_backend", "chrome") or "chrome").lower()

        raw = (content or "").strip()
        try:
            args = json.loads(raw) if raw.startswith("{") else {"action": raw}
        except json.JSONDecodeError:
            return {"error": 'browser: arguments must be JSON, e.g. {"action":"navigate","url":"example.com"}',
                    "exit_code": 1}
        action = str(args.get("action") or "").strip().lower()
        if not action:
            return {"error": "browser: missing 'action'", "exit_code": 1}

        if backend == "safari":
            from src.agent_tools.safari_backend import _SAFARI, SafariUnavailable
            async with _SAFARI.lock:
                try:
                    page = await asyncio.wait_for(_SAFARI.page(), timeout=45)
                except SafariUnavailable as e:
                    return {"error": f"browser (Safari): {e}", "exit_code": 1}
                except Exception as e:
                    return {"error": f"browser (Safari) could not start: {type(e).__name__}: {e}",
                            "exit_code": 1}
                try:
                    return await asyncio.wait_for(self._dispatch(action, args, page, is_safari=True), timeout=60)
                except asyncio.TimeoutError:
                    return {"error": f"browser: action '{action}' timed out after 60s", "exit_code": 1}
                except Exception as e:
                    await _SAFARI.reset()  # a wedged WebDriver session poisons every later call
                    return {"error": f"browser: {action} failed: {type(e).__name__}: {e}", "exit_code": 1}

        async with _SESSION.lock:
            try:
                page = await asyncio.wait_for(_SESSION.page(cdp_url), timeout=45)
            except Exception as e:
                return {"error": _CONNECT_HINT.format(url=cdp_url, err=f"{type(e).__name__}: {e}"),
                        "exit_code": 1}
            try:
                return await asyncio.wait_for(self._dispatch(action, args, page), timeout=60)
            except asyncio.TimeoutError:
                return {"error": f"browser: action '{action}' timed out after 60s", "exit_code": 1}
            except Exception as e:
                return {"error": f"browser: {action} failed: {type(e).__name__}: {e}", "exit_code": 1}

    async def _dispatch(self, action: str, args: dict, page, is_safari: bool = False) -> dict:
        if action in ("navigate", "goto", "open"):
            url = str(args.get("url") or "").strip()
            if not url:
                return {"error": "browser navigate: 'url' is required", "exit_code": 1}
            if "://" not in url:
                url = "https://" + url
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.bring_to_front()  # raise the tab so the user sees it
            except Exception:
                pass
            snap = await self._snapshot(page)
            return {"output": f"Navigated to {page.url}\nTitle: {await page.title()}\n\n{snap}",
                    "exit_code": 0}

        if action in ("snapshot", "elements", "ui"):
            return {"output": await self._snapshot(page), "exit_code": 0}

        if action in ("read", "text", "content"):
            txt = await page.evaluate("() => document.body ? document.body.innerText : ''")
            txt = (txt or "").strip()
            head = f"URL: {page.url}\nTitle: {await page.title()}\n\n"
            out = head + (txt or "(no visible text)")
            if len(out) > MAX_OUTPUT_CHARS:
                out = out[:MAX_OUTPUT_CHARS] + "\n\n[...truncated]"
            return {"output": out, "exit_code": 0}

        if action == "click":
            ref = str(args.get("ref") or "").strip()
            if not ref:
                return {"error": "browser click: 'ref' is required (from a snapshot)", "exit_code": 1}
            loc = page.locator(f'[data-odys-ref="{ref}"]')
            try:
                label = (await loc.inner_text(timeout=3000)).strip()
            except Exception:
                label = (args.get("text") or "").strip()
            reason = _is_risky(label, "", bool(args.get("submit")))
            if reason and not args.get("confirm"):
                return {"output": (f"⚠ CONFIRMATION REQUIRED — clicking '{label or ref}' {reason}. "
                                   "This is a high-stakes action. Ask the user to confirm, then re-call "
                                   'with "confirm": true to proceed.'),
                        "exit_code": 0}
            await loc.click(timeout=8000)
            await page.wait_for_timeout(500)
            return {"output": f"Clicked '{label or ref}'. Now at {page.url}. Re-snapshot to see the new page.",
                    "exit_code": 0}

        if action in ("type", "fill"):
            ref = str(args.get("ref") or "").strip()
            text = args.get("text")
            if not ref or text is None:
                return {"error": "browser type: 'ref' and 'text' are required", "exit_code": 1}
            submit = bool(args.get("submit"))
            if submit and not args.get("confirm"):
                return {"output": ("⚠ CONFIRMATION REQUIRED — typing then submitting a form. Ask the user "
                                   'to confirm, then re-call with "confirm": true.'),
                        "exit_code": 0}
            loc = page.locator(f'[data-odys-ref="{ref}"]')
            await loc.fill(str(text), timeout=8000)
            if submit:
                await loc.press("Enter")
                await page.wait_for_timeout(500)
                return {"output": f"Typed into {ref} and submitted. Now at {page.url}.", "exit_code": 0}
            return {"output": f"Typed into {ref}.", "exit_code": 0}

        if action in ("key", "press"):
            key = str(args.get("key") or "").strip()
            if not key:
                return {"error": "browser key: 'key' is required (e.g. Enter, Escape)", "exit_code": 1}
            if key.lower() in ("enter", "return") and not args.get("confirm"):
                return {"output": ("⚠ CONFIRMATION REQUIRED — pressing Enter can submit a form. Ask the user "
                                   'to confirm, then re-call with "confirm": true.'),
                        "exit_code": 0}
            await page.keyboard.press(key)
            await page.wait_for_timeout(300)
            return {"output": f"Pressed {key}. Now at {page.url}.", "exit_code": 0}

        if action == "scroll":
            direction = str(args.get("direction") or "down").lower()
            amount = int(args.get("amount") or 700)
            dy = amount if direction == "down" else -amount
            await page.evaluate(f"() => window.scrollBy(0, {dy})")
            return {"output": f"Scrolled {direction}.", "exit_code": 0}

        if action in ("back", "forward"):
            if action == "back":
                await page.go_back(wait_until="domcontentloaded")
            else:
                await page.go_forward(wait_until="domcontentloaded")
            return {"output": f"Went {action}. Now at {page.url}.", "exit_code": 0}

        if action == "screenshot":
            path = os.path.join(_shot_dir(), f"shot_{int(time.time()*1000)}.png")
            await page.screenshot(path=path, full_page=bool(args.get("full_page")))
            return {"output": f"Screenshot saved: {path}", "exit_code": 0, "image_path": path}

        if action == "tabs":
            if is_safari:
                return {"output": "Safari backend drives a single window — multi-tab management "
                                  "isn't available. Use 'navigate' to change pages.", "exit_code": 0}
            sub = str(args.get("op") or args.get("tab_action") or "list").lower()
            pages = [p for p in _SESSION._context.pages if not p.is_closed()]
            if sub == "list":
                lines = []
                for i, p in enumerate(pages):
                    mark = "*" if p is page else " "
                    lines.append(f"{mark} [{i}] {await p.title()} — {p.url}")
                return {"output": "Open tabs:\n" + "\n".join(lines), "exit_code": 0}
            if sub in ("select", "switch"):
                idx = int(args.get("index", 0))
                if 0 <= idx < len(pages):
                    _SESSION.set_page(pages[idx])
                    await pages[idx].bring_to_front()
                    return {"output": f"Switched to tab [{idx}] {pages[idx].url}", "exit_code": 0}
                return {"error": f"browser tabs: index {idx} out of range (0..{len(pages)-1})", "exit_code": 1}
            if sub == "new":
                np = await _SESSION._context.new_page()
                _SESSION.set_page(np)
                u = str(args.get("url") or "").strip()
                if u:
                    if "://" not in u:
                        u = "https://" + u
                    await np.goto(u, wait_until="domcontentloaded")
                return {"output": f"Opened new tab. Now at {np.url}", "exit_code": 0}
            return {"error": f"browser tabs: unknown op '{sub}'", "exit_code": 1}

        if action == "wait":
            if args.get("selector"):
                await page.wait_for_selector(str(args["selector"]), timeout=int(args.get("ms") or 10000))
                return {"output": f"Element appeared: {args['selector']}", "exit_code": 0}
            await page.wait_for_timeout(int(args.get("ms") or 1000))
            return {"output": "Waited.", "exit_code": 0}

        return {"error": (f"browser: unknown action '{action}'. Valid: navigate, snapshot, read, click, "
                          "type, key, scroll, back, forward, screenshot, tabs, wait"),
                "exit_code": 1}

    async def _snapshot(self, page) -> str:
        els = await page.evaluate(_SNAPSHOT_JS)
        if not els:
            return "Interactive elements: (none found — try 'read' for page text)"
        lines = [f"  {e['ref']}  {e['tag']:16s} {e['text']}" for e in els]
        out = ("Interactive elements (use the ref with click/type):\n" + "\n".join(lines))
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + "\n  [...more elements truncated; scroll or read]"
        return out
