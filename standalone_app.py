"""Odysseus standalone entry — ONE frozen exe playing every role.

PyInstaller builds this as Odysseus.exe (see OdysseusFull.spec). The same
binary dispatches on argv:

  Odysseus.exe                     -> desktop app: ensure server, open the
                                      frameless window (odysseus_app/app_window)
  Odysseus.exe --server            -> run the FastAPI server (uvicorn) in-process
  Odysseus.exe -c/-m ...           -> emulate a python interpreter call. The
                                      agent's python tool and tooling gates
                                      spawn `sys.executable -I -c ...` /
                                      `-m mod`, which IS this exe when frozen.
  Odysseus.exe <bundled script.py> -> run a bundled Python script. This is how
                                      the built-in MCP servers keep working when
                                      frozen: src/builtin_mcp.py spawns
                                      `sys.executable <mcp_servers/x.py>`, which
                                      IS this exe — so we runpy the script with
                                      the frozen module tree available.

On macOS (Odysseus.app, built by OdysseusMac.spec via the build-macos GitHub
Actions workflow) the same dispatch applies, but the window role uses a plain
Cocoa pywebview window (_mac_window) instead of the Win32 frameless host —
native title bar and traffic lights, no subclassing.

Source runs still work too (python standalone_app.py ...).
"""
import os
import sys


# Windowed (console=False) PyInstaller exes have sys.stdout/stderr = None.
# Anything that prints — fastembed download progress, uvicorn log handlers —
# dies with "'NoneType' object has no attribute 'write'". And a dummy writer
# class is NOT enough: the MCP stdio client spawns child processes with
# errlog=sys.stderr, which needs a real fileno() (observed: every built-in MCP
# server failed with "'_NullWriter' object has no attribute 'fileno'").
# Real devnull file objects satisfy both.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _run_bundled_script(path: str, extra_args) -> None:
    import runpy
    sys.argv = [path] + list(extra_args)
    # The bundled script imports (src.*, mcp, email, ...) resolve against the
    # frozen module tree, which is exactly what we want.
    runpy.run_path(path, run_name="__main__")


def _run_python_emulation(args) -> None:
    """Emulate the interpreter invocations the app makes on itself.

    When frozen, sys.executable IS this exe — internal code that spawns
    "python -c ..." or "python -m mod ..." (the agent python tool, tooling
    gates) would otherwise fall through to the window role and open a new
    GUI instance per call (observed with the Round Table Developer on the
    macOS build). Handle those shapes like a real interpreter; anything
    else dash-prefixed exits with an error instead of opening a window."""
    # Interpreter config flags that don't apply to the frozen runtime.
    while args and args[0] in ("-I", "-E", "-s", "-S", "-B", "-u"):
        args = args[1:]
    if len(args) >= 2 and args[0] == "-c":
        sys.argv = ["-c"] + list(args[2:])
        exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__"})
        return
    if len(args) >= 2 and args[0] == "-m":
        import runpy
        sys.argv = [args[1]] + list(args[2:])
        runpy.run_module(args[1], run_name="__main__", alter_sys=True)
        return
    print(f"Odysseus: unsupported interpreter arguments: {args!r}", file=sys.stderr)
    raise SystemExit(2)


def _run_server() -> None:
    from src.runtime_paths import get_app_root
    os.chdir(get_app_root())  # static/, mcp_servers/ resolve relative to root
    import uvicorn
    from app import app as fastapi_app
    uvicorn.run(
        fastapi_app,
        host="127.0.0.1",
        port=int(os.environ.get("ODYSSEUS_PORT", "7000")),
        log_level="info",
    )


class _MacWindowApi:
    """js_api for the frameless Cocoa window — the web UI's in-page window
    controls call pywebview.api.win_minimize/win_toggle_max/win_close (the
    same surface app_window.WindowApi provides on Windows). Keep the Window
    ref underscore-private: pywebview serializes public attributes over the
    JS bridge and a Window reference recurses forever."""

    def __init__(self):
        self._window = None

    def win_minimize(self):
        try:
            self._window.minimize()
        except Exception:
            pass

    def win_toggle_max(self):
        # window.native IS the NSWindow on cocoa (pywebview sets
        # pywebview_window.native = self.window), and NSWindow.zoom_ toggles
        # the zoomed state itself — exactly the maximize/restore semantic the
        # button wants. Fall back to pywebview's cross-platform calls if
        # native access ever changes.
        try:
            self._window.native.zoom_(None)
            return
        except Exception:
            pass
        try:
            self._window.maximize()
        except Exception:
            try:
                self._window.toggle_fullscreen()
            except Exception:
                pass

    def win_close(self):
        try:
            self._window.destroy()
        except Exception:
            os._exit(0)


def _probe_odysseus(base: str, timeout: float = 1.5) -> bool:
    """True only when ODYSSEUS answers at `base` — verified via the
    X-Odysseus marker on /api/health, so a port squatter (macOS AirPlay
    Receiver answers 403 to everything on 7000) never reads as "up"."""
    import urllib.request
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=timeout) as r:
            return r.headers.get("X-Odysseus") == "1"
    except Exception:
        return False


def _port_is_free(port: int) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _choose_server_port(want: int, is_ours=None, is_free=None):
    """Pick the port the app should use: (port, must_spawn).

    Prefer a live Odysseus on `want` (warm server), else the first nearby
    port that is either ours (warm from a previous fallback launch) or
    actually bindable. macOS AirPlay Receiver squats 7000 and is neither
    ours nor bindable, so a fresh boot walks past it instead of pointing
    the window at AirPlay's 403 (white screen)."""
    is_ours = is_ours or (lambda p: _probe_odysseus(f"http://127.0.0.1:{p}"))
    is_free = is_free or _port_is_free
    for p in [want] + list(range(want + 1, want + 11)):
        if is_ours(p):
            return p, False
        if is_free(p):
            return p, True
    return want, True


def _mac_window() -> None:
    """macOS window role: spawn the server child, open the frameless Cocoa
    window. Mirrors the Windows look — the web UI draws its own window
    controls (shown when pywebview announces itself) and marks the top bar
    as a drag region; no Win32-style subclassing needed. Falls back to the
    default browser if pywebview/pyobjc can't create a window."""
    import subprocess
    import time

    want = int(os.environ.get("ODYSSEUS_PORT", "7000"))
    port, must_spawn = _choose_server_port(want)
    url = f"http://127.0.0.1:{port}"

    if must_spawn:
        # Keep the server child's output — it is the only way to see a
        # traceback behind an in-app HTTP 500 when launched from Finder.
        log_path = os.path.expanduser("~/.odysseus/server.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log = open(log_path, "ab")
        env = dict(os.environ, ODYSSEUS_PORT=str(port))
        subprocess.Popen([sys.executable, "--server"],
                         stdout=log, stderr=log, env=env)
    deadline = time.time() + 120
    while time.time() < deadline and not _probe_odysseus(url):
        time.sleep(0.5)

    try:
        import webview
        # Only the element carrying .pywebview-drag-region itself starts a
        # drag — buttons inside the top bar keep working as buttons (same
        # setting the Windows host uses).
        try:
            webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
        except Exception:
            pass
        # Persistent WebKit profile so the login cookie survives relaunches.
        storage = os.path.expanduser("~/Library/Application Support/Odysseus/WebKit")
        os.makedirs(storage, exist_ok=True)
        api = _MacWindowApi()
        win = webview.create_window("Odysseus", url, js_api=api,
                                    width=1440, height=920,
                                    min_size=(900, 600),
                                    frameless=True, easy_drag=False,
                                    background_color="#282c34")  # app --bg, same as Windows host
        api._window = win

        # The in-page window controls are display:none until body gets the
        # native-app class, which index.html only adds once window.pywebview
        # announces itself — a race the frameless WKWebView lost in testing.
        # Add the class from the HOST on every navigation (login -> app);
        # native evaluateJavaScript is immune to bridge timing.
        def _mark_native():
            try:
                win.evaluate_js(
                    "document.body && document.body.classList.add('native-app')")
            except Exception:
                pass

        win.events.loaded += _mark_native
        # The OdysseusDesktop UA marker lets the server allow 'unsafe-eval'
        # for THIS window only (loopback + this UA) — pywebview's WKWebView
        # bridge needs eval in the page context, which the normal CSP blocks
        # (see core/middleware.py).
        webview.start(private_mode=False, storage_path=storage,
                      user_agent=("Mozilla/5.0 (Macintosh; Apple Silicon) "
                                  "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                                  "OdysseusDesktop/1.1"))
    except Exception:
        import webbrowser
        webbrowser.open(url)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0].endswith(".py") and os.path.exists(args[0]):
        _run_bundled_script(args[0], args[1:])
        return
    if args and args[0] == "--server":
        _run_server()
        return
    if args and args[0].startswith("-") and not args[0].startswith("--"):
        _run_python_emulation(args)
        return
    if sys.platform == "darwin":
        _mac_window()
        return
    import odysseus_app
    odysseus_app.main()


if __name__ == "__main__":
    main()
