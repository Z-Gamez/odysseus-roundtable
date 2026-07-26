"""Start a llama.cpp `llama-server` alongside Odysseus (opt-in, cross-platform).

Ollama and ChromaDB are started by PowerShell helpers, which only ever run on
the Windows window host — the macOS host never invokes them. This launcher is
plain Python so BOTH platforms can call it (odysseus_app.start_background on
Windows, standalone_app._mac_window on macOS).

Off unless `llamacpp_enabled` is true AND `llamacpp_model` points at a GGUF:
spawning an inference server nobody asked for would quietly eat several GB of
VRAM. Idempotent — if something already answers on the port, it does nothing.

`--jinja` is always passed: without it llama-server won't parse tool calls, and
Odysseus's capability probe (llm_core.llamacpp_supports_tools) would report a
tool-capable template that never actually produces tool_calls.
"""
import logging
import os
import shutil
import socket
import subprocess
import sys
from typing import List, Optional

logger = logging.getLogger(__name__)

_WIN_CANDIDATES = [
    r"C:\Odysseus\llamacpp\llama-server.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\llamacpp\llama-server.exe"),
    os.path.expandvars(r"%ProgramFiles%\llamacpp\llama-server.exe"),
]
_MAC_CANDIDATES = [
    "/opt/homebrew/bin/llama-server",     # Apple Silicon Homebrew
    "/usr/local/bin/llama-server",        # Intel Homebrew / manual install
    os.path.expanduser("~/.local/bin/llama-server"),
]


def find_binary(configured: str = "") -> Optional[str]:
    """Locate llama-server: explicit setting, then PATH, then usual install spots."""
    if configured:
        c = os.path.expanduser(configured.strip())
        return c if os.path.exists(c) else None
    found = shutil.which("llama-server")
    if found:
        return found
    for c in (_WIN_CANDIDATES if os.name == "nt" else _MAC_CANDIDATES):
        if c and os.path.exists(c):
            return c
    return None


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((host, int(port)))
        return True
    except Exception:
        return False
    finally:
        s.close()


def build_command(binary: str, model: str, port: int, ctx: int,
                  ngl: int, extra: str = "") -> List[str]:
    """Argv for llama-server. `extra` is split shell-style, never shell=True."""
    cmd = [binary, "-m", model, "--port", str(port), "--jinja",
           "-c", str(ctx), "-ngl", str(ngl)]
    if extra.strip():
        import shlex
        cmd += shlex.split(extra.strip(), posix=(os.name != "nt"))
    return cmd


def start_if_configured() -> Optional[subprocess.Popen]:
    """Launch llama-server when enabled+configured. Returns the process, or
    None when disabled, already running, or unlaunchable. Never raises —
    a failure here must not stop Odysseus from starting."""
    try:
        from src.settings import get_setting
        if not get_setting("llamacpp_enabled", False):
            return None
        model = str(get_setting("llamacpp_model", "") or "").strip()
        port = int(get_setting("llamacpp_port", 8080) or 8080)
        binary = find_binary(str(get_setting("llamacpp_binary", "") or ""))
        # llama.cpp fixes its window at launch, so it must agree with the
        # AI-defaults context cap the trimmer budgets against. The shared
        # setting wins: this used to read only llamacpp_ctx, so llama-server
        # came up at its own default while the AI default said something
        # larger, and long prompts were truncated server-side.
        #
        # llamacpp_ctx is a llama.cpp-only override, but 8192 was its former
        # DEFAULT and the save path materializes defaults — a persisted 8192
        # is almost certainly "never touched", not "deliberately chose 8192".
        # Treating it as explicit would keep overriding the shared setting for
        # every existing install, which is the bug this is fixing.
        _LEGACY_LLAMACPP_CTX_DEFAULT = 8192
        ctx = int(get_setting("ollama_num_ctx", 0) or 0)
        _override = int(get_setting("llamacpp_ctx", 0) or 0)
        if _override > 0 and _override != _LEGACY_LLAMACPP_CTX_DEFAULT:
            ctx = _override
        if ctx <= 0:
            ctx = _LEGACY_LLAMACPP_CTX_DEFAULT
        ngl = int(get_setting("llamacpp_ngl", 99) or 99)
        extra = str(get_setting("llamacpp_extra_args", "") or "")
    except Exception as e:
        logger.warning("[llamacpp] could not read settings: %s", e)
        return None

    if not model:
        logger.info("[llamacpp] enabled but llamacpp_model is unset — not starting")
        return None
    model = os.path.expanduser(model)
    if not os.path.exists(model):
        logger.warning("[llamacpp] model not found: %s", model)
        return None
    if not binary:
        logger.warning("[llamacpp] llama-server binary not found "
                       "(set llamacpp_binary, or install it on PATH)")
        return None
    if port_in_use(port):
        logger.info("[llamacpp] port %s already serving — leaving it alone", port)
        return None

    log_dir = os.path.expanduser("~/.odysseus")
    try:
        os.makedirs(log_dir, exist_ok=True)
        log = open(os.path.join(log_dir, "llamacpp.log"), "ab")
    except Exception:
        log = subprocess.DEVNULL

    cmd = build_command(binary, model, port, ctx, ngl, extra)
    kwargs = {"stdout": log, "stderr": log}
    if os.name == "nt":
        # Detach so it survives the launcher and shows no console window.
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
        logger.info("[llamacpp] started %s on port %s (pid %s)",
                    os.path.basename(binary), port, proc.pid)
        return proc
    except Exception as e:
        logger.warning("[llamacpp] failed to start: %s", e)
        return None
