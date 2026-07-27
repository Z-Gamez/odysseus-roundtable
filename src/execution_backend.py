"""Where the agent's shell and python tools actually run.

Everything executed on the machine hosting Odysseus. That is the right default,
but it ties the agent's heaviest work — Gradle, Flutter, Xcode, test suites,
the Round Table build gate — to whatever laptop happens to be running the UI.

Setting `agent_execution_target` to "ssh:<host>" routes those tools to a real
machine while the UI, database and model routing stay local.

WHAT THIS IS NOT: this does not change where the MODEL runs. Model placement is
already independent — a model endpoint's base_url can point anywhere (LAN box,
Tailscale host, remapped container port), and the capability probes are
deliberately not gated on a localhost heuristic. If the goal is "run a bigger
model than this laptop fits", add a remote endpoint; this setting will not help
and routing shell commands over SSH would only add latency. This is for work
that is heavy to EXECUTE, not heavy to INFER.

Reuses core.platform_compat._ssh_exec_argv — the same argv builder the cookbook
already drives remote GPU boxes with — so host/port/known-hosts handling has
exactly one implementation. Building the argv (rather than calling the sync
run_ssh_command) keeps execution async and streaming, which the agent's
progress callbacks and timeout handling depend on.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_LOCAL = "local"


def _setting(key: str, default):
    try:
        from src.settings import get_setting
        v = get_setting(key, default)
        return default if v is None else v
    except Exception:
        return default


def resolve_target(override: Optional[str] = None) -> Tuple[str, str]:
    """Return (kind, host). kind is "local" or "ssh"; host is "" when local.

    Accepts "local", "" (local), or "ssh:user@host". Anything unrecognised
    falls back to local WITH a warning rather than raising: a typo in a setting
    must not make every shell command fail, and silently running a build on the
    laptop is recoverable in a way that a dead agent is not.
    """
    raw = str(override if override is not None
              else _setting("agent_execution_target", _LOCAL) or _LOCAL).strip()
    if not raw or raw.lower() == _LOCAL:
        return _LOCAL, ""
    if raw.lower().startswith("ssh:"):
        host = raw[4:].strip()
        if host:
            return "ssh", host
        logger.warning("agent_execution_target is 'ssh:' with no host — "
                       "running locally")
        return _LOCAL, ""
    logger.warning("agent_execution_target %r is not understood "
                   "(expected 'local' or 'ssh:<host>') — running locally", raw)
    return _LOCAL, ""


def is_remote(override: Optional[str] = None) -> bool:
    return resolve_target(override)[0] != _LOCAL


def remote_workspace() -> str:
    """Workspace path ON THE REMOTE HOST.

    The local workspace path is meaningless over there: a macOS
    /Users/x/projects has no counterpart on a Linux box, and letting it through
    means every command silently runs in the SSH login directory instead —
    tests "pass" against no source at all. Blank disables the cd entirely, which
    is the honest default when nothing has been configured.
    """
    return str(_setting("agent_ssh_workspace", "") or "").strip()


def build_remote_command(command: str, *, cwd: Optional[str] = None,
                         shell: str = "bash") -> str:
    """Wrap a command so it runs in the right directory on the remote host.

    `cd ... && ...` with the directory quoted, and a hard failure if the cd
    fails. Without the `&&` a missing workspace would run the command in the
    login directory and report success for work done nowhere near the source.
    """
    target_dir = (cwd or remote_workspace()).strip()
    inner = command
    if target_dir:
        inner = f"cd {shlex.quote(target_dir)} && {command}"
    # -lc so the remote login profile is sourced: toolchains installed by
    # nvm/sdkman/pyenv live in the profile, and without it a remote build fails
    # with "gradle: not found" on a machine that plainly has gradle.
    return f"{shell} -lc {shlex.quote(inner)}"


async def run(command: str, *, timeout: float,
              progress_cb=None, cwd: Optional[str] = None,
              target: Optional[str] = None,
              shell: str = "bash") -> Dict[str, Any]:
    """Run `command` on the configured target, streaming like a local run.

    Returns the same dict shape the local path returns, so callers do not need
    to care which backend served them.
    """
    kind, host = resolve_target(target)
    if kind == _LOCAL:
        raise RuntimeError("execution_backend.run called for a local target; "
                           "callers should use the local path directly")

    from core.platform_compat import _ssh_exec_argv
    from src.agent_tools.subprocess_tools import _run_subprocess_streaming

    remote_cmd = build_remote_command(command, cwd=cwd, shell=shell)
    try:
        argv = _ssh_exec_argv(
            host,
            str(_setting("agent_ssh_port", "") or "") or None,
            remote_cmd=remote_cmd,
            connect_timeout=int(_setting("agent_ssh_connect_timeout", 10)),
        )
    except ValueError as e:
        return {"error": f"execution target ssh:{host} is not usable: {e}",
                "exit_code": 1}

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
        proc, timeout=timeout, progress_cb=progress_cb)
    if timed_out:
        return {"error": f"remote command timed out after {timeout}s on {host}",
                "exit_code": 124, "stdout": stdout, "stderr": stderr}

    # ssh exits 255 for its OWN failures (host unreachable, auth refused). That
    # is not the command's exit code, and reporting it as one sends the agent
    # off debugging a build that never ran.
    if rc == 255 and not stdout.strip():
        return {"error": f"ssh to {host} failed: {stderr.strip()[:400] or 'no detail'}. "
                         f"Check agent_execution_target, keys, and reachability.",
                "exit_code": 255}
    return {"stdout": stdout, "stderr": stderr, "exit_code": rc or 0}
