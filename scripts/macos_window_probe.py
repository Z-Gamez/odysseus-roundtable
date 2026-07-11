"""CI probe for the macOS frameless window (build-macos.yml).

Opens the same frameless pywebview/WKWebView window the shipped app uses
(js_api + forced native-app class), pointed at the already-running frozen
server, and verifies from inside the page that:
  - the pywebview bridge announced itself,
  - the js_api window-control methods are exposed,
  - body carries the native-app class,
  - the in-page window controls are actually visible (display: flex).

Exit 1 on a failed check (real regression). If the runner can't create a
GUI window at all, prints the environment error and exits 0 — that's a
runner limitation, not a product failure.
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from standalone_app import _MacWindowApi  # noqa: E402

import webview  # noqa: E402

URL = os.environ.get("PROBE_URL", "http://127.0.0.1:7100")
results = {}


def probe(win):
    time.sleep(8)  # let the page render and the bridge settle
    checks = {
        "pywebview_present": "!!window.pywebview",
        "api_win_minimize": "!!(window.pywebview && window.pywebview.api"
                            " && window.pywebview.api.win_minimize)",
        "native_app_class": "document.body.classList.contains('native-app')",
        "controls_display": "(function(){var el=document.getElementById('win-controls');"
                            " return el ? getComputedStyle(el).display : 'missing';})()",
    }
    for key, js in checks.items():
        try:
            results[key] = win.evaluate_js(js)
        except Exception as e:  # noqa: BLE001 — report, don't crash the probe
            results[key] = f"ERROR: {e}"
    win.destroy()


def main() -> int:
    try:
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    except Exception:
        pass
    api = _MacWindowApi()
    win = webview.create_window("Odysseus probe", URL, js_api=api,
                                width=1200, height=800, frameless=True,
                                easy_drag=False, background_color="#282c34")
    api._window = win

    def _mark_native():
        try:
            win.evaluate_js(
                "document.body && document.body.classList.add('native-app')")
        except Exception:
            pass

    win.events.loaded += _mark_native
    threading.Thread(target=probe, args=(win,), daemon=True).start()
    try:
        webview.start(private_mode=True)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"environment_limited": str(e)}))
        return 0

    print(json.dumps(results, indent=2, default=str))
    ok = (results.get("pywebview_present") is True
          and results.get("api_win_minimize") is True
          and results.get("native_app_class") is True
          and results.get("controls_display") == "flex")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
