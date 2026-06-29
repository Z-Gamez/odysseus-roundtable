"""Odysseus - standalone desktop app entry point.

Runnable directly (`python odysseus_app.py`) or packaged into Odysseus.exe with
PyInstaller (see build-app.ps1). Starts Ollama, ChromaDB, and the Odysseus server
in the background (no consoles), then hosts the UI in a native window.

Because the packaged .exe is itself the window host, pinning Odysseus.exe to the
taskbar gives a single clean button with the Odysseus icon (no shortcut AUMID hack
needed - the pinned exe and the running window share the same executable identity).
"""
import os
import sys
import time
import subprocess
import urllib.request
import urllib.error

HOST = "127.0.0.1"
PORT = 7000
URL = f"http://{HOST}:{PORT}"
TITLE = "Odysseus"
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW - keep spawned processes console-less


def app_root() -> str:
    """The Odysseus repo dir (where venv/ and app.py live). For the frozen exe this
    is the folder the exe sits in, so place Odysseus.exe in the repo root."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


ROOT = app_root()
VENV_PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")
ICON = os.path.join(ROOT, "static", "odysseus.ico")


def _server_up() -> bool:
    try:
        urllib.request.urlopen(URL, timeout=2)
        return True
    except urllib.error.HTTPError:
        return True          # any HTTP status means it's listening
    except Exception:
        return False


def _run_helper(rel_path: str) -> None:
    p = os.path.join(ROOT, rel_path)
    if os.path.exists(p):
        try:
            subprocess.Popen(["powershell", "-ExecutionPolicy", "Bypass", "-File", p],
                             creationflags=_NO_WINDOW)
        except Exception:
            pass


def start_background():
    """Start Ollama + ChromaDB (idempotent helpers) and the server. Returns the
    uvicorn process if we launched it (so we can stop it on close), else None."""
    _run_helper(os.path.join("scripts", "launch-ollama.ps1"))
    _run_helper(os.path.join("scripts", "launch-chromadb.ps1"))
    proc = None
    if not _server_up() and os.path.exists(VENV_PY):
        proc = subprocess.Popen(
            [VENV_PY, "-m", "uvicorn", "app:app", "--host", HOST, "--port", str(PORT)],
            cwd=ROOT, creationflags=_NO_WINDOW)
    deadline = time.time() + 90
    while time.time() < deadline and not _server_up():
        time.sleep(0.7)
    return proc


def _apply_icon(*_args) -> None:
    """Push the Odysseus icon onto the window (title bar + taskbar) once it exists."""
    if os.name != "nt" or not os.path.exists(ICON):
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        WM_SETICON, SMALL, BIG, IMG, LR = 0x0080, 0, 1, 1, 0x00000010
        small = u.LoadImageW(None, ICON, IMG, 16, 16, LR)
        big = u.LoadImageW(None, ICON, IMG, 32, 32, LR)
        for _ in range(50):
            hwnd = u.FindWindowW(None, TITLE)
            if hwnd:
                if small:
                    u.SendMessageW(hwnd, WM_SETICON, SMALL, small)
                if big:
                    u.SendMessageW(hwnd, WM_SETICON, BIG, big)
                break
            time.sleep(0.1)
    except Exception:
        pass


def open_window() -> None:
    import webview
    storage = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                           "Odysseus", "WebView2")
    try:
        os.makedirs(storage, exist_ok=True)
    except Exception:
        storage = None
    webview.create_window(TITLE, URL, width=1440, height=920, min_size=(900, 600))
    # private_mode=False keeps cookies/login; callback sets the icon after open.
    webview.start(_apply_icon, private_mode=False, storage_path=storage)


def main() -> None:
    proc = start_background()
    try:
        open_window()
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    main()
