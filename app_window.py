"""Odysseus desktop window.

Opens the Odysseus web UI in a native application window (Windows WebView2 via
pywebview) so it looks and feels like a standalone app instead of a browser tab.
Falls back to Edge/Chrome "app mode" (a borderless browser window) if pywebview
or the WebView2 runtime isn't available.

Run by launch-app.ps1 after the server is up. ODYSSEUS_URL overrides the address.
"""
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


def open_native_window(url: str) -> bool:
    """True native window via pywebview (WebView2). Blocks until the window closes."""
    try:
        import webview
    except Exception as e:
        print(f"[odysseus-app] pywebview not available: {e}")
        return False
    try:
        webview.create_window(TITLE, url, width=1440, height=920, min_size=(900, 600))
        webview.start()  # blocks until the user closes the window
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
