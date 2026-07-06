"""Odysseus quick panel — a global-hotkey pop-up chat.

A small, persistent background process that hosts a compact Odysseus chat in a
frameless always-on-top window. Press the hotkey (default Ctrl+Alt+Space) from
anywhere to summon it; Esc or the hotkey again dismisses it. A tray icon quits.

It runs in its OWN WebView2 profile (separate from the main window), so it logs
itself in via the loopback-only /api/auth/quick-login route using a local
secret. Launch it with the venv python:
    venv\\Scripts\\python.exe quick_panel.py
"""
import ctypes
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from ctypes import wintypes

import webview

HOST = "127.0.0.1"
PORT = int(os.environ.get("ODYSSEUS_PORT", "7000"))
BASE = f"http://{HOST}:{PORT}"
WINDOW_TITLE = "OdysseusQuickPanel"
BG = "#282c34"
PANEL_W = 640  # fixed width (compact, Spotlight-like); height is content-fit via Api.resize

_FROZEN = getattr(sys, "frozen", False)
ROOT = os.path.dirname(os.path.abspath(__file__))
if _FROZEN:
    ROOT = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))


def _default_data_dir() -> str:
    # Must match the server's DATA_DIR (src.runtime_paths.get_default_data_dir)
    # or we read the quick-login secret from the wrong place.
    try:
        from src.runtime_paths import get_default_data_dir
        return get_default_data_dir()
    except Exception:
        return os.path.join(ROOT, "data")


DATA_DIR = os.environ.get("ODYSSEUS_DATA_DIR") or _default_data_dir()
SECRET_FILE = os.path.join(DATA_DIR, "quick_panel.secret")
ICON = os.path.join(ROOT, "static", "odysseus.ico")

user32 = ctypes.windll.user32
SW_HIDE, SW_SHOW = 0, 5
SWP_NOSIZE, SWP_NOZORDER, SWP_SHOWWINDOW = 0x0001, 0x0004, 0x0040
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_TOPMOST = 0x00000080, 0x00040000, 0x00000008
HWND_TOPMOST = wintypes.HWND(-1)
MONITOR_DEFAULTTONEAREST = 2

# Correct arg/return types so 64-bit HWNDs aren't truncated (the reason an
# untyped SetWindowPos silently failed to move the window).
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _server_up() -> bool:
    try:
        urllib.request.urlopen(BASE, timeout=2)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _read_secret() -> str:
    # Hitting the route (even unauthenticated) makes the server create the
    # secret file as a side effect; then we can read it.
    try:
        urllib.request.urlopen(BASE + "/api/auth/quick-login", timeout=3)
    except Exception:
        pass
    for _ in range(20):
        try:
            with open(SECRET_FILE, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if val:
                return val
        except Exception:
            pass
        time.sleep(0.3)
    return ""


class Api:
    """js_api exposed to the compact page (window.pywebview.api)."""
    def __init__(self):
        self.hwnd = None
        self.visible = False

    def hide(self):
        try:
            if _window:
                _window.hide()
        except Exception:
            if self.hwnd:
                user32.ShowWindow(self.hwnd, SW_HIDE)
        self.visible = False

    def resize(self, height):
        """Content-fit: the page reports its natural height and the window
        shrinks/grows to match (top edge anchored), so there is never empty
        window around the chat — the window IS the content."""
        try:
            h = max(58, min(int(height), 680))
            if _window:
                _window.resize(PANEL_W, h)
        except Exception:
            pass

    def quit(self):
        _quit()


_api = Api()
_window = None
_quitting = False


def _find_hwnd() -> int:
    # Qt backend: QMainWindow.winId(); WinForms backend: Form.Handle.
    # FindWindow-by-title is the last resort (flaky for frameless windows).
    try:
        if _window is not None and _window.native is not None:
            native = _window.native
            if hasattr(native, "winId"):
                return int(native.winId())
            return native.Handle.ToInt32()
    except Exception:
        pass
    return user32.FindWindowW(None, WINDOW_TITLE) or 0


def _make_toolwindow(hwnd: int) -> None:
    """Keep the panel out of the taskbar and Alt-Tab (it's a pop-up, not an
    app window), and pin it topmost."""
    try:
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TOOLWINDOW | WS_EX_TOPMOST) & ~WS_EX_APPWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
    except Exception:
        pass


def _center_and_show(hwnd: int) -> None:
    try:
        h_ = wintypes.HWND(hwnd)
        rect = wintypes.RECT()
        user32.GetWindowRect(h_, ctypes.byref(rect))
        w = rect.right - rect.left
        # Horizontally centered, anchored in the upper third of the monitor's
        # WORK AREA (Spotlight position). Top-anchored so content growth
        # expands downward from a stable spot.
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        hmon = user32.MonitorFromWindow(h_, MONITOR_DEFAULTTONEAREST)
        if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            wa = mi.rcWork
            x = wa.left + (wa.right - wa.left - w) // 2
            y = wa.top + int((wa.bottom - wa.top) * 0.22)
        else:
            x = max(0, (user32.GetSystemMetrics(0) - w) // 2)
            y = int(user32.GetSystemMetrics(1) * 0.22)
        user32.SetWindowPos(h_, HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_SHOWWINDOW)
    except Exception:
        user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOW)
    user32.SetForegroundWindow(wintypes.HWND(hwnd))


def _toggle() -> None:
    hwnd = _api.hwnd or _find_hwnd()
    print(f"[quick-panel] toggle (visible={_api.visible} hwnd={hwnd})", flush=True)
    if _api.visible:
        try:
            if _window:
                _window.hide()
        except Exception:
            if hwnd:
                user32.ShowWindow(hwnd, SW_HIDE)
        _api.visible = False
    else:
        try:
            if _window:
                _window.show()
        except Exception:
            if hwnd:
                user32.ShowWindow(hwnd, SW_SHOW)
        if hwnd:
            _center_and_show(hwnd)  # position + topmost + foreground
        _api.visible = True
        try:
            if _window:
                _window.evaluate_js("window.__quickFocus && window.__quickFocus()")
        except Exception:
            pass


def _hotkey_loop() -> None:
    """Register the global hotkey and pump WM_HOTKEY on this thread. Ctrl+Alt+
    Space by default; override with ODYSSEUS_QUICK_HOTKEY_VK (virtual-key code)."""
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
    WM_HOTKEY = 0x0312
    mods = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
    # Default Ctrl+Alt+O (O for Odysseus). Override with ODYSSEUS_QUICK_HOTKEY_VK
    # (a virtual-key code, e.g. 0x20 for Space). Fall back through a couple of
    # alternates if the primary is already taken by another app.
    primary = int(os.environ.get("ODYSSEUS_QUICK_HOTKEY_VK", "0x4F"), 0)  # 0x4F = 'O'
    candidates = [primary, 0x20, 0x4A, 0xC0]  # O, Space, J, backtick(`)
    names = {0x4F: "Ctrl+Alt+O", 0x20: "Ctrl+Alt+Space", 0x4A: "Ctrl+Alt+J", 0xC0: "Ctrl+Alt+`"}
    chosen = None
    for vk in candidates:
        if user32.RegisterHotKey(None, 1, mods, vk):
            chosen = vk
            break
    if chosen is None:
        print("[quick-panel] could not register any global hotkey (all in use).", flush=True)
        return
    print(f"[quick-panel] hotkey registered: {names.get(chosen, hex(chosen))}", flush=True)
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            _toggle()


def _start_tray() -> None:
    try:
        import pystray
        from PIL import Image
        try:
            image = Image.open(ICON)
        except Exception:
            image = Image.new("RGBA", (64, 64), (224, 108, 117, 255))
        menu = pystray.Menu(
            pystray.MenuItem("Show Odysseus  (Ctrl+Alt+Space)",
                             lambda icon, item: _toggle(), default=True),
            pystray.MenuItem("Quit", lambda icon, item: _quit()),
        )
        icon = pystray.Icon("odysseus_quick", image, "Odysseus quick panel", menu)
        globals()["_tray"] = icon
        icon.run()
    except Exception as e:
        print(f"[quick-panel] tray unavailable: {e}", flush=True)


def _quit() -> None:
    global _quitting
    if _quitting:
        return
    _quitting = True
    try:
        tray = globals().get("_tray")
        if tray:
            tray.stop()
    except Exception:
        pass
    try:
        if _window:
            _window.destroy()
    except Exception:
        pass
    os._exit(0)


def _round_corners(hwnd: int) -> None:
    """Windows 11 DWM round corners on the bare frameless window, so the
    content-fit panel reads as a floating rounded card without any CSS card.
    (True per-pixel transparency is NOT possible with windowed WebView2 —
    tested: both layered color-key and transparent=True render an opaque
    backdrop. Content-fit geometry is how PowerToys Run et al. do it.)"""
    try:
        dwm = ctypes.windll.dwmapi
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33,
                                  ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def _on_start() -> None:
    """Runs (off the GUI thread) once pywebview's loop is up: locate the native
    window, make it a tool window, and start the hotkey + tray listeners."""
    hwnd = 0
    for _ in range(40):
        hwnd = _find_hwnd()
        if hwnd:
            break
        time.sleep(0.1)
    _api.hwnd = hwnd
    if hwnd:
        _make_toolwindow(hwnd)
        _round_corners(hwnd)
        user32.ShowWindow(hwnd, SW_HIDE)  # start hidden; summon with the hotkey
        _api.visible = False
    threading.Thread(target=_hotkey_loop, daemon=True).start()
    threading.Thread(target=_start_tray, daemon=True).start()
    print(f"[quick-panel] ready (hwnd={hwnd}). Press the hotkey to summon.", flush=True)


def main() -> None:
    # Single instance via named mutex — the frozen build spawns this on every
    # app launch, and a second instance would fight over the global hotkey.
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\OdysseusQuickPanel")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("[quick-panel] already running — exiting.", flush=True)
        return
    print("[quick-panel] starting; waiting for server…", flush=True)

    # Wait for the server, then grab the loopback-login secret.
    for _ in range(120):
        if _server_up():
            break
        time.sleep(1)
    print("[quick-panel] server up; reading secret…", flush=True)
    secret = _read_secret()
    if not secret:
        print("[quick-panel] could not read quick-login secret; is the server up?", flush=True)
        return
    url = f"{BASE}/api/auth/quick-login?s={secret}"
    print("[quick-panel] secret ok; creating window + starting UI loop…", flush=True)

    storage = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                           "Odysseus", "QuickPanelWV2")
    try:
        os.makedirs(storage, exist_ok=True)
    except Exception:
        storage = None

    global _window
    _window = webview.create_window(
        WINDOW_TITLE, url, js_api=_api,
        width=PANEL_W, height=50, frameless=True, easy_drag=False,
        on_top=True, hidden=True, background_color=BG,
    )
    _api._window = _window
    # NOTE on transparency (investigated 2026-07-05, twice): windowed
    # WebView2/EdgeChromium fundamentally cannot do per-pixel transparency
    # (opaque backdrop; layered color-key also fails). pywebview's Qt backend
    # supports it in principle (WA_TranslucentBackground) but QtWebEngine
    # deadlocked at init on most launches here — not shippable. The
    # content-fit window (Api.resize) is the reliable "no background" design.
    webview.start(_on_start, private_mode=False, storage_path=storage)


if __name__ == "__main__":
    main()
