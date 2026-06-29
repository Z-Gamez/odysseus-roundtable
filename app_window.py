"""Odysseus desktop window.

Opens the Odysseus web UI in a native application window (Windows WebView2 via
pywebview) so it looks and feels like a standalone app instead of a browser tab.
Falls back to Edge/Chrome "app mode" (a borderless browser window) if pywebview
or the WebView2 runtime isn't available.

Run by launch-app.ps1 after the server is up. ODYSSEUS_URL overrides the address.
"""
import ctypes
import os
import sys
import time
import urllib.request
import urllib.error

URL = os.environ.get("ODYSSEUS_URL", "http://127.0.0.1:7000")
TITLE = "Odysseus"


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


def _apply_window_icon(*_args) -> None:
    """Set the title-bar + taskbar icon on the native window (Windows only).

    pywebview windows default to the python/pythonw icon; load the Odysseus .ico and
    push it onto the window with WM_SETICON once the window exists."""
    if os.name != "nt":
        return
    ico = _resolve_icon()
    if not ico:
        return
    try:
        user32 = ctypes.windll.user32
        WM_SETICON, ICON_SMALL, ICON_BIG, IMAGE_ICON, LR = 0x0080, 0, 1, 1, 0x00000010
        small = user32.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR)
        big = user32.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR)
        for _ in range(50):
            hwnd = user32.FindWindowW(None, TITLE)
            if hwnd:
                if small:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
                if big:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
                break
            time.sleep(0.1)
    except Exception as e:
        print(f"[odysseus-app] could not set window icon: {e}")


def open_native_window(url: str) -> bool:
    """True native window via pywebview (WebView2). Blocks until the window closes."""
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
    try:
        webview.create_window(TITLE, url, width=1440, height=920, min_size=(900, 600))
        webview.start(_apply_window_icon)  # callback runs after the window opens -> set icon
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
    if not wait_for_server(URL):
        print(f"[odysseus-app] Server at {URL} did not come up in time.")
        sys.exit(1)
    if not open_native_window(URL):
        open_app_mode(URL)
