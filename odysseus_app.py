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
    """Kick off Ollama + ChromaDB (idempotent helpers) and the server WITHOUT
    waiting — the window opens on a splash immediately and flips to the app
    when the server answers (see app_window). Returns the uvicorn process if
    we launched it, else None (already running from a previous session)."""
    _run_helper(os.path.join("scripts", "launch-ollama.ps1"))
    _run_helper(os.path.join("scripts", "launch-chromadb.ps1"))
    proc = None
    if not _server_up() and os.path.exists(VENV_PY):
        proc = subprocess.Popen(
            [VENV_PY, "-m", "uvicorn", "app:app", "--host", HOST, "--port", str(PORT)],
            cwd=ROOT, creationflags=_NO_WINDOW)
    return proc


def open_window() -> None:
    """Host the UI in the shared frameless window (see app_window.py).

    app_window owns all the window chrome (frameless title bar, native
    resize/snap, js_api min/max/close, icon); we just point it at our URL/icon."""
    os.environ.setdefault("ODYSSEUS_URL", URL)
    if os.path.exists(ICON):
        os.environ.setdefault("ODYSSEUS_ICON", ICON)
    import app_window
    if not app_window.open_native_window(URL):
        app_window.open_app_mode(URL)


def main() -> None:
    proc = start_background()
    try:
        open_window()
    finally:
        # Keep the server WARM by default: closing the window leaves uvicorn
        # (and Ollama/ChromaDB, which were always independent) running, so the
        # next launch skips the slow boot and the splash flips instantly.
        # Set ODYSSEUS_KILL_SERVER_ON_EXIT=1 to restore the old stop-on-close.
        if (os.environ.get("ODYSSEUS_KILL_SERVER_ON_EXIT", "").strip() in ("1", "true", "yes")
                and proc is not None and proc.poll() is None):
            try:
                proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    main()
