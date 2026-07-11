"""Odysseus standalone entry — ONE frozen exe playing every role.

PyInstaller builds this as Odysseus.exe (see OdysseusFull.spec). The same
binary dispatches on argv:

  Odysseus.exe                     -> desktop app: ensure server, open the
                                      frameless window (odysseus_app/app_window)
  Odysseus.exe --server            -> run the FastAPI server (uvicorn) in-process
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
        # NSWindow.zoom_ toggles the zoomed state itself, which is exactly
        # the maximize/restore semantic the button wants. Fall back to
        # pywebview's cross-platform calls if native access ever changes.
        try:
            self._window.native.window.zoom_(None)
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


def _mac_window() -> None:
    """macOS window role: spawn the server child, open the frameless Cocoa
    window. Mirrors the Windows look — the web UI draws its own window
    controls (shown when pywebview announces itself) and marks the top bar
    as a drag region; no Win32-style subclassing needed. Falls back to the
    default browser if pywebview/pyobjc can't create a window."""
    import subprocess
    import time
    import urllib.error
    import urllib.request

    port = int(os.environ.get("ODYSSEUS_PORT", "7000"))
    url = f"http://127.0.0.1:{port}"

    def up() -> bool:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except urllib.error.HTTPError:
            return True   # any HTTP status means it's listening
        except Exception:
            return False

    if not up():
        subprocess.Popen([sys.executable, "--server"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 120
    while time.time() < deadline and not up():
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
        webview.start(private_mode=False, storage_path=storage)
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
    if sys.platform == "darwin":
        _mac_window()
        return
    import odysseus_app
    odysseus_app.main()


if __name__ == "__main__":
    main()
