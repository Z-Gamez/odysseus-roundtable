"""Unload the auto-launched llama-server when nobody is using it.

llama-server holds the whole model resident for the life of the process and has
no idle timeout of its own. On a 16 GB machine a 9B Q6_K at ctx 12288 sits on
6.45 GB whether or not anyone is talking to it — which is most of the time.

This supervisor stops the child after `llamacpp_idle_timeout_seconds` with no
completion requests, and brings it back on the next one. The user pays a slower
first message rather than seeing an error, because `ensure_running` waits for
/v1/models to answer before the request is forwarded.

Only applies to a server THIS process started. A llama-server the user launched
themselves is left alone: `start_if_configured` is port-guarded, so we never
adopt a foreign process, and killing one we did not start would be a surprise.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Set by start_if_configured via note_started(); None means "not ours".
_child: Optional[subprocess.Popen] = None
_last_request: float = 0.0
_supervisor_task: Optional[asyncio.Task] = None
# Serialises relaunch so concurrent requests after an unload start ONE server
# rather than racing several onto the same port.
_relaunch_lock: Optional[asyncio.Lock] = None


def _setting(key, default):
    try:
        from src.settings import get_setting
        v = get_setting(key, default)
        return default if v is None else v
    except Exception:
        return default


def note_started(proc: subprocess.Popen) -> None:
    """Record the child we launched, so only that one is ever stopped."""
    global _child
    _child = proc


def note_request() -> None:
    """Mark the endpoint as in use. Cheap enough for every request."""
    global _last_request
    _last_request = time.time()


def idle_seconds() -> float:
    return 0.0 if not _last_request else time.time() - _last_request


def is_running() -> bool:
    return _child is not None and _child.poll() is None


def targets_llamacpp(endpoint_url: str) -> bool:
    """Is this URL the llama-server we manage?

    Matched on port rather than host: the same server answers on localhost, the
    LAN address and the machine's hostname, and a request through any of them
    is still activity that should hold off an unload.
    """
    try:
        if not _setting("llamacpp_enabled", False):
            return False
        port = int(_setting("llamacpp_port", 8080) or 8080)
        parsed = urlparse((endpoint_url or "").strip())
        return bool(parsed.port and int(parsed.port) == port)
    except Exception:
        return False


def _stop_child() -> bool:
    global _child
    proc = _child
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            # A model mid-generation can ignore SIGTERM long enough to matter;
            # the point of the unload is to release the RAM.
            proc.kill()
            proc.wait(timeout=10)
        logger.info("[llamacpp] idle-unloaded (pid %s) — freed the model's RAM",
                    proc.pid)
        return True
    except Exception as e:
        logger.warning("[llamacpp] idle unload failed: %s", e)
        return False
    finally:
        _child = None


async def ensure_running(timeout: float = 180.0) -> bool:
    """Bring the server back if the supervisor unloaded it. True when serving.

    Awaited before a request is forwarded, so the caller sees a slow first
    message instead of a connection error. Settings are re-read by
    start_if_configured, so llamacpp_extra_args, the preset and the bind host
    all survive the round trip unchanged.
    """
    global _relaunch_lock
    if not _setting("llamacpp_enabled", False):
        return False
    if is_running():
        return True

    if _relaunch_lock is None:
        _relaunch_lock = asyncio.Lock()
    async with _relaunch_lock:
        # Re-check inside the lock: several requests can queue on a cold start
        # and only the first should launch anything.
        if is_running():
            return True
        from src.llamacpp_launcher import start_if_configured
        proc = await asyncio.to_thread(start_if_configured)
        if proc is None:
            # Either disabled, unlaunchable, or something already holds the
            # port — start_if_configured logs which.
            return False
        note_started(proc)

        port = int(_setting("llamacpp_port", 8080) or 8080)
        deadline = time.time() + timeout
        import httpx
        while time.time() < deadline:
            if proc.poll() is not None:
                logger.warning("[llamacpp] relaunched server exited immediately")
                return False
            try:
                async with httpx.AsyncClient(timeout=5.0) as c:
                    r = await c.get(f"http://127.0.0.1:{port}/v1/models")
                if r.status_code == 200:
                    logger.info("[llamacpp] back up after idle unload")
                    note_request()
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.0)
        logger.warning("[llamacpp] relaunch did not answer within %ss", timeout)
        return False


async def _loop(interval: float = 30.0) -> None:
    while True:
        try:
            await asyncio.sleep(interval)
            timeout = float(_setting("llamacpp_idle_timeout_seconds", 900) or 0)
            if timeout <= 0 or not is_running():
                continue
            # No request yet since start: begin the clock rather than
            # unloading a server that has never been given a chance.
            if not _last_request:
                note_request()
                continue
            if idle_seconds() >= timeout:
                _stop_child()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[llamacpp] supervisor tick failed: %s", e)


def start_supervisor() -> Optional[asyncio.Task]:
    """Begin watching. No-op when the timeout is 0 (the feature is off)."""
    global _supervisor_task
    if _supervisor_task is not None and not _supervisor_task.done():
        return _supervisor_task
    if float(_setting("llamacpp_idle_timeout_seconds", 900) or 0) <= 0:
        return None
    try:
        _supervisor_task = asyncio.create_task(_loop())
        logger.info("[llamacpp] idle supervisor started (%ss)",
                    _setting("llamacpp_idle_timeout_seconds", 900))
        return _supervisor_task
    except RuntimeError:
        return None


def server_rss_bytes() -> Optional[int]:
    """Resident memory of the llama-server child, or None if unknown.

    psutil is not a dependency, so this shells out per platform. Reported in
    diagnostics so the cost of keeping a model loaded is visible rather than
    something users discover when the machine starts swapping.
    """
    if not is_running():
        return None
    pid = _child.pid
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Process -Id {pid} -ErrorAction Stop).WorkingSet64"],
                capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip().isdigit():
                return int(out.stdout.strip())
            return None
        # Linux exposes VmRSS in kB; macOS has no /proc, so fall back to ps.
        status = f"/proc/{pid}/status"
        if os.path.exists(status):
            with open(status, "r") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
        val = out.stdout.strip()
        return int(val) * 1024 if val.isdigit() else None
    except Exception as e:
        logger.debug("[llamacpp] RSS lookup failed: %s", e)
        return None


def status() -> dict:
    """Snapshot for the diagnostics route."""
    rss = server_rss_bytes()
    timeout = float(_setting("llamacpp_idle_timeout_seconds", 900) or 0)
    return {
        "running": is_running(),
        "pid": _child.pid if is_running() else None,
        "rss_bytes": rss,
        "rss_mb": round(rss / (1024 * 1024), 1) if rss else None,
        "idle_seconds": round(idle_seconds(), 1) if _last_request else None,
        "idle_timeout_seconds": timeout,
        "unloads_when_idle": timeout > 0,
    }
