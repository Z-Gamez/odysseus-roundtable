"""Odysseus desktop window.

Opens the Odysseus web UI in a FRAMELESS native window (Windows WebView2 via
pywebview) with the app's own in-page title bar controls — the left rail runs
to the very top of the window, like a modern Electron-style app. Native window
behaviour is restored at the Win32 level:

  * edge resizing  — the form keeps a slim padding rim around the WebView2
    control and a WndProc subclass answers WM_NCHITTEST with resize handles;
  * Aero Snap      — WS_THICKFRAME/WS_MAXIMIZEBOX are re-added (invisible,
    WM_NCCALCSIZE returns a full-window client area);
  * window drag    — WebView2 non-client region support maps the page's
    `app-region: drag` areas to a native caption (snap + dblclick maximize);
    pywebview's JS drag regions remain as a fallback;
  * min/max/close  — exposed to the page as a pywebview js_api (window.pywebview
    .api.win_minimize / win_toggle_max / win_close).

Falls back to Edge/Chrome "app mode" (a normal framed browser window) if
pywebview or the WebView2 runtime isn't available.

Run by launch-app.ps1 (or imported by odysseus_app.py for the packaged exe).
ODYSSEUS_URL overrides the address; ODYSSEUS_ICON overrides the icon.
"""
import ctypes
import os
import sys
import time
import urllib.request
import urllib.error

if os.name == "nt":
    import ctypes.wintypes  # noqa: F401 — populates ctypes.wintypes

URL = os.environ.get("ODYSSEUS_URL", "http://127.0.0.1:7000")
TITLE = "Odysseus"
BG = "#282c34"      # app --bg; the form rim + load background use this
PAD = 5             # logical px rim kept around the WebView2 for resize handles

# Shown instantly while the server boots — the window no longer waits for the
# server before appearing. Minimal frameless chrome: a drag strip + close
# button (wired to the js_api once pywebview announces itself).
_SPLASH_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;background:#282c34;color:#9cdef2;
    font-family:'Cascadia Code','Consolas',monospace;overflow:hidden;
    user-select:none;cursor:default}
  .wrap{height:100%;display:flex;flex-direction:column;align-items:center;
    justify-content:center;gap:14px}
  .boat{width:56px;height:56px;color:#e06c75;animation:bob 2.2s ease-in-out infinite}
  @keyframes bob{0%,100%{transform:translateY(0) rotate(-2deg)}50%{transform:translateY(-7px) rotate(2deg)}}
  .name{font-size:26px;font-weight:600;letter-spacing:.06em;color:#e06c75}
  .status{font-size:12px;opacity:.55}
  .dots::after{content:'';animation:d 1.5s steps(4,end) infinite}
  @keyframes d{0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}}
  #drag{position:fixed;top:0;left:0;right:52px;height:34px;
    app-region:drag;-webkit-app-region:drag}
  #cls{position:fixed;top:0;right:0;width:42px;height:34px;display:none;
    align-items:center;justify-content:center;background:none;border:none;
    color:#9cdef2;opacity:.55;app-region:no-drag;-webkit-app-region:no-drag}
  #cls:hover{background:#e81123;color:#fff;opacity:1}
</style></head><body>
  <div id="drag" class="pywebview-drag-region"></div>
  <button id="cls" title="Close">&#x2715;</button>
  <div class="wrap">
    <svg class="boat" viewBox="0 0 32 32"><path d="M16 4L16 22L6 22Z" fill="currentColor"/><path d="M16 8L16 22L24 22Z" fill="currentColor" opacity="0.6"/><path d="M4 24Q10 20 16 24Q22 28 28 24" stroke="currentColor" stroke-width="2.5" fill="none" stroke-linecap="round"/></svg>
    <div class="name">Odysseus</div>
    <div class="status"><span id="st">Starting the ship</span><span class="dots"></span></div>
  </div>
  <script>
    function armClose(){var b=document.getElementById('cls');b.style.display='flex';
      b.onclick=function(){try{window.pywebview.api.win_close()}catch(e){}}}
    if (window.pywebview) armClose();
    else window.addEventListener('pywebviewready', armClose);
  </script>
</body></html>"""

# Keep ctypes callback objects alive for the life of the window (GC guard).
_KEEPALIVE = []


def wait_for_server(url: str, timeout: int = 90) -> bool:
    """Block until the server answers (any HTTP response, incl. 401/redirect, means up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except urllib.error.HTTPError:
            return True  # server responded with a status code -> it's listening
        except Exception:
            time.sleep(0.7)
    return False


def _resolve_icon() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (os.environ.get("ODYSSEUS_ICON", ""),
              os.path.join(here, "static", "odysseus.ico"),
              r"C:\Odysseus\images-removebg-preview.ico"):
        if c and os.path.exists(c):
            return c
    return ""


class WindowApi:
    """js_api exposed to the page — drives the frameless window's controls.

    NOTE: the window reference MUST be underscore-private. pywebview's JS
    bridge introspects every public attribute of the api object, and walking
    a pywebview Window -> WinForms Form graph recurses forever
    (AccessibilityObject.Bounds.Empty.Empty...)."""

    def __init__(self):
        self._window = None  # set before webview.start()

    def _hwnd(self):
        try:
            return self._window.native.Handle.ToInt32()
        except Exception:
            return None

    def win_minimize(self):
        try:
            self._window.minimize()
        except Exception:
            pass

    def win_toggle_max(self):
        try:
            h = self._hwnd()
            if h and ctypes.windll.user32.IsZoomed(h):
                self._window.restore()
            else:
                self._window.maximize()
        except Exception:
            pass

    def win_close(self):
        try:
            self._window.destroy()
        except Exception:
            pass


def _apply_icon(hwnd) -> None:
    """Push the Odysseus icon onto the window (taskbar/alt-tab) via WM_SETICON."""
    ico = _resolve_icon()
    if not ico:
        return
    try:
        u = ctypes.windll.user32
        WM_SETICON, IMAGE_ICON, LR = 0x0080, 1, 0x00000010
        small = u.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR)
        big = u.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR)
        if small:
            u.SendMessageW(hwnd, WM_SETICON, 0, small)
        if big:
            u.SendMessageW(hwnd, WM_SETICON, 1, big)
    except Exception as e:
        print(f"[odysseus-app] could not set window icon: {e}")


def _apply_dwm(hwnd) -> None:
    """Rounded corners + dark chrome hints (best-effort, Win11 DWM)."""
    try:
        dwm = ctypes.windll.dwmapi

        def _set(attr, value):
            v = ctypes.c_uint(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        _set(20, 1)           # DWMWA_USE_IMMERSIVE_DARK_MODE
        _set(33, 2)           # DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND
        # DWMWA_BORDER_COLOR = DWMWA_COLOR_NONE: no border stroke at all, so the
        # frame Windows draws for WS_THICKFRAME never shows its default (white)
        # inactive color. Must be (re)applied AFTER the thick frame exists.
        _set(34, 0xFFFFFFFE)
    except Exception:
        pass


def _subclass_frameless(hwnd) -> None:
    """Restore native resize/snap on the frameless window.

    Re-adds WS_THICKFRAME/WS_MIN/MAXIMIZEBOX (snap eligibility + minimize
    animation), makes WM_NCCALCSIZE report a full-window client area (so the
    re-added frame stays invisible), and answers WM_NCHITTEST over the padding
    rim with the resize-handle codes so all edges/corners resize natively."""
    user32 = ctypes.windll.user32
    wintypes = ctypes.wintypes

    GWL_STYLE, GWL_WNDPROC = -16, -4
    WS_THICKFRAME, WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00040000, 0x00020000, 0x00010000
    WM_NCCALCSIZE, WM_NCHITTEST = 0x0083, 0x0084

    is64 = ctypes.sizeof(ctypes.c_void_p) == 8
    LONG_PTR = ctypes.c_longlong if is64 else ctypes.c_long
    get_long = user32.GetWindowLongPtrW if is64 else user32.GetWindowLongW
    set_long = user32.SetWindowLongPtrW if is64 else user32.SetWindowLongW
    get_long.restype = LONG_PTR
    get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    set_long.restype = LONG_PTR
    set_long.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]

    call_proc = user32.CallWindowProcW
    call_proc.restype = LONG_PTR
    call_proc.argtypes = [LONG_PTR, wintypes.HWND, ctypes.c_uint,
                          wintypes.WPARAM, wintypes.LPARAM]

    style = get_long(hwnd, GWL_STYLE)
    set_long(hwnd, GWL_STYLE, style | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)

    # Physical hit-band a touch wider than the visual rim so edges are easy to grab.
    try:
        dpi = user32.GetDpiForWindow(hwnd)
    except Exception:
        dpi = 96
    band = max(6, int(round((PAD + 2) * dpi / 96.0)))

    # Capture the previous proc BEFORE swapping — messages can arrive on the UI
    # thread the instant SetWindowLongPtr returns.
    old_proc = get_long(hwnd, GWL_WNDPROC)

    WNDPROC = ctypes.WINFUNCTYPE(LONG_PTR, wintypes.HWND, ctypes.c_uint,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    class MINMAXINFO(ctypes.Structure):
        _fields_ = [("ptReserved", wintypes.POINT), ("ptMaxSize", wintypes.POINT),
                    ("ptMaxPosition", wintypes.POINT), ("ptMinTrackSize", wintypes.POINT),
                    ("ptMaxTrackSize", wintypes.POINT)]

    def proc(h, msg, wp, lp):
        if msg == 0x0086:  # WM_NCACTIVATE — on focus loss Windows repaints the
            # non-client frame with its default INACTIVE border (a white line
            # around the window). Passing lParam = -1 tells the default proc to
            # skip that non-client repaint entirely, so the border never turns
            # white when the window is unfocused. (Must still call the default
            # proc so activation state itself is processed.)
            return call_proc(old_proc, h, msg, wp, -1)
        if msg == 0x0005:  # WM_SIZE — WinForms can reassert its borderless
            # CreateParams styles (e.g. on the maximize/restore padding swap);
            # quietly re-add the bits that keep native resize + snap alive.
            st = get_long(h, GWL_STYLE)
            want = WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
            if (st & want) != want:
                set_long(h, GWL_STYLE, st | want)
        if msg == 0x0024:  # WM_GETMINMAXINFO — WinForms maximizes borderless
            # forms over the ENTIRE screen (covering the taskbar); pin the
            # maximize bounds to the current monitor's work area instead.
            res = call_proc(old_proc, h, msg, wp, lp)  # fills MinTrackSize
            try:
                mon = user32.MonitorFromWindow(h, 2)  # MONITOR_DEFAULTTONEAREST
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                    mmi = ctypes.cast(lp, ctypes.POINTER(MINMAXINFO)).contents
                    mmi.ptMaxPosition.x = mi.rcWork.left - mi.rcMonitor.left
                    mmi.ptMaxPosition.y = mi.rcWork.top - mi.rcMonitor.top
                    mmi.ptMaxSize.x = mi.rcWork.right - mi.rcWork.left
                    mmi.ptMaxSize.y = mi.rcWork.bottom - mi.rcWork.top
                    return 0
            except Exception:
                pass
            return res
        if msg == WM_NCCALCSIZE and wp:
            # Full-window client area = no visible frame. (Maximize bounds are
            # already pinned to the work area via WM_GETMINMAXINFO.)
            return 0
        if msg == WM_NCHITTEST and not user32.IsZoomed(h):
            r = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            x = ctypes.c_short(lp & 0xFFFF).value
            y = ctypes.c_short((lp >> 16) & 0xFFFF).value
            top, bottom = y < r.top + band, y >= r.bottom - band
            left, right = x < r.left + band, x >= r.right - band
            if top and left:
                return 13      # HTTOPLEFT
            if top and right:
                return 14      # HTTOPRIGHT
            if bottom and left:
                return 16      # HTBOTTOMLEFT
            if bottom and right:
                return 17      # HTBOTTOMRIGHT
            if left:
                return 10      # HTLEFT
            if right:
                return 11      # HTRIGHT
            if top:
                return 12      # HTTOP
            if bottom:
                return 15      # HTBOTTOM
        return call_proc(old_proc, h, msg, wp, lp)

    proc_ref = WNDPROC(proc)
    _KEEPALIVE.append(proc_ref)
    set_long(hwnd, GWL_WNDPROC, ctypes.cast(proc_ref, ctypes.c_void_p).value)

    SWP = 0x0001 | 0x0002 | 0x0004 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP)


def _dbg(msg: str) -> None:
    """Stage log for the window chrome — the packaged app runs windowless
    (pythonw/exe), so stdout is unreliable; mirror to a file."""
    line = f"[odysseus-app] {msg}"
    print(line, flush=True)
    try:
        p = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                         "Odysseus", "window.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + line + "\n")
    except Exception:
        pass


def _install_chrome(window) -> None:
    """webview.start() callback: dress the frameless window (icon, DWM, resize
    rim, native drag support).

    All .NET/Win32 work is queued to the UI thread with BeginInvoke — a
    blocking Invoke from this Python thread can deadlock against the GIL, and
    WinForms property changes must precede the Win32 style patch anyway (they
    reassert the borderless CreateParams styles)."""
    if os.name != "nt":
        return
    _dbg("install: enter")
    # The start() callback races window creation — wait for the native form.
    try:
        window.events.shown.wait(20)
    except Exception:
        pass
    form = None
    for _ in range(100):
        form = getattr(window, "native", None)
        if form is not None:
            break
        time.sleep(0.1)
    if form is None:
        _dbg("install: no native form")
        return
    try:
        hwnd = form.Handle.ToInt32()
    except Exception as e:
        _dbg(f"install: no window handle: {e}")
        return
    _dbg(f"install: hwnd={hwnd}")

    _apply_icon(hwnd)
    _apply_dwm(hwnd)
    _dbg("install: icon+dwm done")

    try:
        import webview.platforms.winforms as wf
        WinForms = wf.WinForms

        scale = float(getattr(form, "_scale", 1.0) or 1.0)
        pad = max(PAD, int(round(PAD * scale)))

        def _rim():
            # The WebView2 control is docked Fill, so form padding insets it,
            # exposing a slim strip of form surface for the resize hit-tests.
            if form.WindowState == WinForms.FormWindowState.Maximized:
                form.Padding = WinForms.Padding(0)
            else:
                form.Padding = WinForms.Padding(pad)

        def _on_resize(sender, args):
            try:
                _rim()
            except Exception:
                pass

        def _setup():
            try:
                # Paint the Form's own surface dark. The resize rim insets the
                # WebView2 by a few px, exposing a strip of Form background; its
                # WinForms default is light gray/white — THAT was the "white
                # outline" (a pixel probe read (255,255,255) in the rim). Match
                # it to the app bg so the rim is invisible.
                form.BackColor = wf.ColorTranslator.FromHtml("#282c34")
                _rim()
                form.Resize += _on_resize
                _dbg("setup: rim painted + installed")
                # Win32 styles + WndProc subclass LAST, on the UI thread,
                # so nothing reasserts over them.
                _subclass_frameless(hwnd)
                _dbg("setup: frameless resize/snap installed")
                # No DWM border stroke (COLOR_NONE), bound now the frame exists.
                _apply_dwm(hwnd)
                _dbg("setup: dwm re-applied post-frame")
            except Exception as e:
                _dbg(f"setup FAILED: {e}")

        form.BeginInvoke(wf.Func[wf.Type](_setup))
        _dbg("install: setup queued to UI thread")

        # Native drag for `app-region: drag` page regions (snap, dblclick-
        # maximize, system menu). CoreWebView2 initializes asynchronously —
        # only safe to touch once the page has loaded.
        try:
            window.events.loaded.wait(30)
        except Exception:
            pass

        def _ncr():
            try:
                form.webview.CoreWebView2.Settings.IsNonClientRegionSupportEnabled = True
                _dbg("ncr: native drag regions ON")
            except Exception as e:
                _dbg(f"ncr: native drag unavailable ({e}); JS drag fallback")

        form.BeginInvoke(wf.Func[wf.Type](_ncr))
        _dbg("install: ncr queued to UI thread")
    except Exception as e:
        _dbg(f"install FAILED: {e}")


def open_native_window(url: str) -> bool:
    """Frameless native window via pywebview (WebView2). Blocks until closed."""
    try:
        import webview
    except Exception as e:
        print(f"[odysseus-app] pywebview not available: {e}")
        return False
    # Give the app its own taskbar identity so it doesn't group under Python.
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Odysseus.Desktop.App")
        except Exception:
            pass
    # Only the element carrying .pywebview-drag-region itself starts a JS drag —
    # buttons inside the top bar keep working as buttons.
    try:
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    except Exception:
        pass
    # Persistent WebView2 profile so cookies / localStorage survive across launches —
    # i.e. the login session (and saved username/password) is remembered.
    storage = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Odysseus", "WebView2")
    try:
        os.makedirs(storage, exist_ok=True)
    except Exception:
        storage = None
    try:
        import threading
        api = WindowApi()
        # Open on the SPLASH instantly — don't make the user stare at nothing
        # while the server boots. A waiter thread flips to the real URL the
        # moment the server answers (sub-second when the server is already warm).
        win = webview.create_window(TITLE, html=_SPLASH_HTML, js_api=api,
                                    width=1440, height=920, min_size=(900, 600),
                                    frameless=True, easy_drag=False,
                                    background_color=BG)
        api._window = win

        def _flip_when_ready():
            ok = wait_for_server(url, timeout=120)
            try:
                if ok:
                    win.load_url(url)
                else:
                    win.evaluate_js(
                        "document.getElementById('st').textContent="
                        "'Server did not start — check launch logs';"
                        "document.querySelector('.dots').style.display='none';")
            except Exception as e:
                _dbg(f"splash flip failed: {e}")

        threading.Thread(target=_flip_when_ready, daemon=True).start()
        # private_mode=False keeps the profile on disk; the callback dresses the window.
        webview.start(_install_chrome, (win,), private_mode=False, storage_path=storage)
        return True
    except Exception as e:
        print(f"[odysseus-app] native window failed ({e}); falling back to app-mode")
        return False


def open_app_mode(url: str) -> bool:
    """Fallback: Edge/Chrome --app borderless window. Blocks until it's closed."""
    import subprocess
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ]
    exe = next((c for c in candidates if os.path.exists(c)), None)
    if not exe:
        print("[odysseus-app] No Edge/Chrome found for app-mode fallback.")
        return False
    profile = os.path.expandvars(r"%LocalAppData%\Odysseus\AppWindow")
    os.makedirs(profile, exist_ok=True)
    proc = subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run"])
    proc.wait()
    return True


if __name__ == "__main__":
    # No pre-wait: the window opens on the splash immediately and flips to the
    # app when the server answers. The app-mode fallback still needs a live
    # server, so only that path waits.
    if not open_native_window(URL):
        if wait_for_server(URL):
            open_app_mode(URL)
        else:
            print(f"[odysseus-app] Server at {URL} did not come up in time.")
            sys.exit(1)
