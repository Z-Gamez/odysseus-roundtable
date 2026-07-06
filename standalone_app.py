"""Odysseus standalone entry — ONE frozen exe playing every role.

PyInstaller builds this as Odysseus.exe (see OdysseusFull.spec). The same
binary dispatches on argv:

  Odysseus.exe                     -> desktop app: ensure server, open the
                                      frameless window (odysseus_app/app_window)
  Odysseus.exe --server            -> run the FastAPI server (uvicorn) in-process
  Odysseus.exe --quick-panel       -> global-hotkey quick panel (quick_panel.py)
  Odysseus.exe <bundled script.py> -> run a bundled Python script. This is how
                                      the built-in MCP servers keep working when
                                      frozen: src/builtin_mcp.py spawns
                                      `sys.executable <mcp_servers/x.py>`, which
                                      IS this exe — so we runpy the script with
                                      the frozen module tree available.

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


def main() -> None:
    args = sys.argv[1:]
    if args and args[0].endswith(".py") and os.path.exists(args[0]):
        _run_bundled_script(args[0], args[1:])
        return
    if args and args[0] == "--server":
        _run_server()
        return
    if args and args[0] == "--quick-panel":
        import quick_panel
        quick_panel.main()
        return
    import odysseus_app
    odysseus_app.main()


if __name__ == "__main__":
    main()
