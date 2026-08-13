"""Start ComfyUI only when an image is actually wanted, and stop it when it is not.

RAM is the whole reason this exists. ComfyUI idle costs ~1-2GB of system memory
and, once a checkpoint is loaded, ~7GB of VRAM — on a 12GB card that is most of
what a local LLM wants. Autostarting it at boot would hold both all day for a
feature used occasionally, so it is launched on the first image request and
unloaded again after a spell of quiet, mirroring what llamacpp_supervisor does
for llama-server.

Ownership is tracked through a pidfile rather than a Popen handle because the
image tool runs in its own MCP process: whoever starts ComfyUI records it, and
the supervisor in the main app can still find and stop it. That also survives a
restart of either side, which a live handle would not.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8188
_last_request: float = 0.0
_supervisor_task: Optional[asyncio.Task] = None
_launch_lock: Optional[asyncio.Lock] = None


def _setting(key, default):
    try:
        from src.settings import get_setting
        v = get_setting(key, default)
        return default if v is None else v
    except Exception:
        return default


def _pidfile() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "comfyui.pid")


def install_dir() -> str:
    """Where the portable ComfyUI lives. Env override for a custom install."""
    env = os.environ.get("COMFYUI_DIR", "").strip()
    if env:
        return env
    for cand in (r"C:\Odysseus\comfyui",
                 os.path.expanduser("~/comfyui"),
                 "/opt/comfyui"):
        if os.path.isdir(cand):
            return cand
    return ""


def _launch_argv(root: str) -> Optional[list]:
    """Command to start ComfyUI, preferring the portable embedded Python."""
    main = os.path.join(root, "ComfyUI", "main.py")
    if not os.path.isfile(main):
        main = os.path.join(root, "main.py")
    if not os.path.isfile(main):
        return None
    py = os.path.join(root, "python_embeded", "python.exe")
    if not os.path.isfile(py):
        py = os.path.join(root, "venv", "Scripts", "python.exe")
    if not os.path.isfile(py):
        py = sys.executable
    return [py, "-s", main, "--listen", "127.0.0.1",
            "--port", str(_port()), "--disable-auto-launch"]


def _port() -> int:
    try:
        return int(_setting("comfyui_port", DEFAULT_PORT))
    except Exception:
        return DEFAULT_PORT


def note_request() -> None:
    """Mark image generation as in use, so the idle clock restarts."""
    global _last_request
    _last_request = time.time()


def idle_seconds() -> float:
    return 0.0 if not _last_request else time.time() - _last_request


def _port_in_use() -> bool:
    """Is anything bound to the port, answering or not?

    Distinguishes "no server" from "server too busy to reply", which
    is_answering() cannot: a ComfyUI loading a 6GB checkpoint under memory
    pressure fails a 2.5s health check while being perfectly alive.
    """
    import socket
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", _port()))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


async def _wait_until_answering(timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await is_answering():
            return True
        await asyncio.sleep(2.0)
    return False


async def is_answering(timeout: float = 2.5) -> bool:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"http://127.0.0.1:{_port()}/system_stats")
            return r.status_code == 200
    except Exception:
        return False


def _read_pid() -> Optional[int]:
    try:
        with open(_pidfile(), "r", encoding="utf-8") as fh:
            return int((fh.read() or "").strip())
    except Exception:
        return None


def _write_pid(pid: int) -> None:
    try:
        with open(_pidfile(), "w", encoding="utf-8") as fh:
            fh.write(str(pid))
    except Exception as e:
        logger.warning("[comfyui] could not record pid: %s", e)


def _clear_pid() -> None:
    try:
        os.remove(_pidfile())
    except Exception:
        pass


def we_started_it() -> bool:
    """True when a ComfyUI we launched is still alive.

    A ComfyUI the user started by hand must never be killed by our idle timer —
    they may be working in its web UI.
    """
    pid = _read_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        _clear_pid()
        return False


async def ensure_running(timeout: float = 180.0) -> bool:
    """Make ComfyUI answer, launching it if needed. False when unavailable."""
    global _launch_lock
    if await is_answering():
        note_request()
        return True

    root = install_dir()
    argv = _launch_argv(root) if root else None
    if not argv:
        logger.info("[comfyui] no installation found; not launching")
        return False

    if _launch_lock is None:
        _launch_lock = asyncio.Lock()
    # Serialise: two images requested at once must not start two servers on
    # the same port.
    async with _launch_lock:
        if await is_answering():
            note_request()
            return True
        # Someone is already on the port. That is almost always a ComfyUI busy
        # loading a checkpoint — its HTTP server stops answering while a large
        # model pages in, so is_answering() says no while the process is very
        # much alive. Launching a second one here would collide, exit, and
        # leave the pidfile pointing at the corpse, which is how a slow first
        # run turned into "cold every time".
        if _port_in_use():
            logger.info("[comfyui] port %s already held — waiting for it to "
                        "answer rather than starting a second server", _port())
            if await _wait_until_answering(timeout):
                note_request()
                return True
            logger.warning("[comfyui] the process on port %s never answered", _port())
            return False
        logger.info("[comfyui] starting on demand: %s", " ".join(argv[:3]))
        try:
            kwargs = {"cwd": root, "stdout": subprocess.DEVNULL,
                      "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                # Detached and windowless: a console flashing up whenever the
                # user asks for a picture is its own bug report.
                kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW
                                           | subprocess.DETACHED_PROCESS)
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(argv, **kwargs)
        except Exception as e:
            logger.warning("[comfyui] launch failed: %s", e)
            return False
        _write_pid(proc.pid)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                logger.warning("[comfyui] exited immediately (code %s)", proc.returncode)
                _clear_pid()
                return False
            if await is_answering():
                logger.info("[comfyui] up after on-demand start")
                note_request()
                _maybe_warm()
                return True
            await asyncio.sleep(1.0)
        logger.warning("[comfyui] did not answer within %ss", timeout)
        return False


def _maybe_warm() -> None:
    """Kick off a throwaway 64x64 render so the checkpoint loads NOW.

    The checkpoint load is the expensive part — ~145s on a 16GB machine with a
    local LLM also resident, six times the uncontended cost. Paying it in the
    background right after launch means the user's actual request finds the
    model already in memory, instead of being the one that waits for it.

    Fire-and-forget on purpose: a failed warm-up must never fail the launch,
    and the real request will simply load the checkpoint itself.
    """
    if not _setting("comfyui_warm_on_launch", True):
        return

    async def _warm():
        try:
            from src import comfyui_backend as backend
            logger.info("[comfyui] warming the checkpoint in the background")
            await backend.generate("warmup", size="64x64", steps=1)
            logger.info("[comfyui] checkpoint warm — first real image will be fast")
        except Exception as e:
            logger.info("[comfyui] warm-up did not finish (harmless): %s", e)

    try:
        asyncio.get_running_loop().create_task(_warm())
    except RuntimeError:
        pass          # no loop (sync caller) — the first request warms it instead


def stop() -> bool:
    """Stop the ComfyUI we started, freeing its RAM and VRAM."""
    pid = _read_pid()
    if not pid:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.25)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
            else:
                os.kill(pid, signal.SIGKILL)
        logger.info("[comfyui] stopped after idle (pid %s)", pid)
        return True
    except Exception as e:
        logger.warning("[comfyui] stop failed: %s", e)
        return False
    finally:
        _clear_pid()


async def _loop(interval: float = 60.0) -> None:
    while True:
        try:
            await asyncio.sleep(interval)
            timeout = float(_setting("comfyui_idle_timeout_seconds", 600) or 0)
            if timeout <= 0 or not we_started_it():
                continue
            if not _last_request:
                # Started but never used yet — begin the clock rather than
                # killing something that has had no chance to be asked.
                note_request()
                continue
            if idle_seconds() >= timeout:
                stop()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[comfyui] supervisor tick failed: %s", e)


def start_supervisor() -> Optional[asyncio.Task]:
    """Watch for idleness. No-op when the timeout is 0 (feature off)."""
    global _supervisor_task
    if _supervisor_task is not None and not _supervisor_task.done():
        return _supervisor_task
    if float(_setting("comfyui_idle_timeout_seconds", 600) or 0) <= 0:
        return None
    try:
        _supervisor_task = asyncio.create_task(_loop())
        logger.info("[comfyui] idle supervisor started (%ss)",
                    _setting("comfyui_idle_timeout_seconds", 600))
        return _supervisor_task
    except RuntimeError:
        return None


def status() -> dict:
    return {
        "install_dir": install_dir(),
        "port": _port(),
        "ours": we_started_it(),
        "idle_seconds": round(idle_seconds(), 1),
        "idle_timeout": _setting("comfyui_idle_timeout_seconds", 600),
    }
