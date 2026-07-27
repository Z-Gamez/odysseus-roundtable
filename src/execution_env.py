"""Execution environments: where the agent's shell, python and FILE tools run.

Ported from Hermes Agent's `tools/environments/` (NousResearch/hermes-agent,
MIT, Copyright (c) 2025 Nous Research). Adapted to Odysseus's async tool layer;
the mechanisms below — the session snapshot, the in-band CWD marker, and the
SSH ControlMaster setup — follow Hermes' implementation closely because each
one solves a problem that is not obvious until it bites.

WHY THIS REPLACES THE EARLIER execution_backend
-----------------------------------------------
The first attempt routed bash/python over SSH and left the file tools calling
open() on the Odysseus host. That splits the agent across two filesystems: it
writes app.py locally, runs `cat app.py` remotely, and gets "no such file" — or
compiles a stale checkout and reports a clean pass for code it never built.
It had to be papered over by refusing file tools while remote.

Hermes has no such seam because file operations are not a separate code path:
they are shell commands run through the SAME environment as everything else
(tools/file_operations.py builds a command and calls `env.execute()`). Whatever
host `execute()` targets is the one and only filesystem in play. Copying that
shape removes the split at the root instead of guarding it.

STATE ACROSS CALLS
------------------
Every execute() spawns a fresh process — no long-lived shell to lose. State
survives anyway, by two mechanisms Hermes uses:

  * env vars: each command sources a snapshot file, then re-dumps `export -p`
    into it atomically (write temp, then mv) so a concurrent reader never sees
    a half-written file.
  * cwd: the wrapper prints `pwd -P` between two copies of a per-session
    marker. The caller parses the marker out of stdout and strips it. No temp
    file, works identically on local and remote.

Without these, `cd build` in one tool call is silently forgotten by the next —
the agent walks itself into the wrong directory and never finds out.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Hermes: tools/environments/base.py::_cwd_marker
def _cwd_marker(session_id: str) -> str:
    return f"__ODY_CWD_{session_id}__"


class BaseEnvironment:
    """Common command-wrapping and state recovery. Subclasses spawn processes."""

    def __init__(self, cwd: str = "~", timeout: int = 120):
        self.cwd = cwd
        self.timeout = timeout
        self._session_id = uuid.uuid4().hex[:8]
        self._cwd_marker = _cwd_marker(self._session_id)
        self._snapshot_path = self._make_snapshot_path()
        self._snapshot_ready = True

    def _make_snapshot_path(self) -> str:
        return f"/tmp/.odysseus-env-{self._session_id}"

    def _quote_shell_path(self, path: str) -> str:
        return shlex.quote(path)

    # Hermes: base.py::_quote_cwd_for_cd — bare ``~`` must stay unquoted or the
    # shell takes it literally, while ``~/a b`` has to go through $HOME so the
    # suffix survives as ONE word.
    def _quote_cwd_for_cd(self, cwd: str) -> str:
        if not cwd or cwd == "~":
            return "$HOME"
        if cwd.startswith("~/"):
            return f"$HOME/{shlex.quote(cwd[2:])}"
        return shlex.quote(cwd)

    # Hermes: base.py::_wrap_command
    def _wrap_command(self, command: str, cwd: str) -> str:
        escaped = command.replace("'", "'\\''")
        _quoted_snap = self._quote_shell_path(self._snapshot_path)
        # $BASHPID, not $$: under `&` the subshell PID is what makes two
        # concurrent writers pick different temp names instead of clobbering
        # each other before the mv.
        _snap_tmp = self._quote_shell_path(self._snapshot_path + ".tmp.") + "$BASHPID"

        parts = []
        if self._snapshot_ready:
            # stdout to /dev/null: on macOS bash 3.2, sourcing a file of
            # `declare -x` echoes every declaration, leaking ~60 lines of env
            # into each tool response.
            parts.append(f"source {_quoted_snap} >/dev/null 2>&1 || true")

        # ``--`` so a directory named -foo is not parsed as options.
        parts.append(f"builtin cd -- {self._quote_cwd_for_cd(cwd)} || exit 126")
        parts.append(f"eval '{escaped}'")
        parts.append("__ody_ec=$?")
        # Snapshots can carry env-borne secrets; restrict them without touching
        # the umask the user's own command ran under.
        parts.append("umask 077")
        if self._snapshot_ready:
            # Chain mv on the dump succeeding so a partial dump never replaces
            # a good snapshot, and drop the temp otherwise. The redirect binds
            # to a brace group because $BASHPID inside a pipeline segment
            # expands in a different subshell than the one running mv.
            parts.append(
                f"{{ export -p > {_snap_tmp} "
                f"&& mv -f {_snap_tmp} {_quoted_snap}; }} "
                f"2>/dev/null || rm -f {_snap_tmp} 2>/dev/null || true"
            )
        # Leading \n guarantees the marker starts its own line even when the
        # command emits no trailing newline (printf 'exact'); it is stripped
        # again in _extract_cwd_from_output.
        parts.append(
            f"printf '\\n{self._cwd_marker}%s{self._cwd_marker}\\n' \"$(pwd -P)\"")
        parts.append("exit $__ody_ec")
        return "\n".join(parts)

    # Hermes: base.py::_extract_cwd_from_output
    def _extract_cwd_from_output(self, result: Dict[str, Any]) -> None:
        """Read the CWD marker out of stdout, update self.cwd, strip it."""
        output = result.get("output", "")
        marker = self._cwd_marker
        last = output.rfind(marker)
        if last == -1:
            return
        search_start = max(0, last - 4096)   # a cwd path is never >4KB
        first = output.rfind(marker, search_start, last)
        if first == -1 or first == last:
            return
        cwd_path = output[first + len(marker):last].strip()
        if cwd_path:
            self.cwd = cwd_path
        line_start = output.rfind("\n", 0, first)
        if line_start == -1:
            line_start = first
        line_end = output.find("\n", last + len(marker))
        line_end = line_end + 1 if line_end != -1 else len(output)
        result["output"] = output[:line_start] + output[line_end:]

    async def _spawn(self, wrapped: str) -> asyncio.subprocess.Process:
        raise NotImplementedError

    async def execute(self, command: str, cwd: str = "", *,
                      timeout: Optional[int] = None,
                      progress_cb=None) -> Dict[str, Any]:
        """Run `command`; return {"output", "exit_code"}. One filesystem."""
        from src.agent_tools.subprocess_tools import _run_subprocess_streaming
        effective_timeout = timeout or self.timeout
        wrapped = self._wrap_command(command, cwd or self.cwd)
        try:
            proc = await self._spawn(wrapped)
        except Exception as e:
            return {"output": "", "error": str(e), "exit_code": 1}
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc, timeout=effective_timeout, progress_cb=progress_cb)
        result = {"output": stdout or "", "exit_code": rc or 0}
        self._extract_cwd_from_output(result)
        if timed_out:
            return {"error": f"timed out after {effective_timeout}s",
                    "exit_code": 124, "output": result["output"],
                    "stderr": stderr}
        if stderr and stderr.strip():
            result["stderr"] = stderr
        return result

    async def cleanup(self) -> None:
        pass


# Hermes: local.py::_bash_safe_path
def _bash_safe_path(path: str) -> str:
    """Put a Windows path in a form Git Bash will accept.

    ``C:\\Users\\x`` and ``C:/Users/x`` both become ``/c/Users/x``. Skipping
    this is not cosmetic: bash cannot cd into or redirect to a native Windows
    path, so the snapshot write and the `cd` BOTH fail silently — env vars and
    cwd are forgotten between every call, with no error anywhere. That is
    exactly what the end-to-end test caught.
    """
    if os.name != "nt" or not path:
        return path
    if len(path) > 1 and path[1] == ":":
        path = f"/{path[0].lower()}{path[2:]}"
    if "\\" in path:
        path = path.replace("\\", "/")
    return path


class LocalEnvironment(BaseEnvironment):
    """Run on the machine hosting Odysseus."""

    def _quote_shell_path(self, path: str) -> str:
        return shlex.quote(_bash_safe_path(path))

    def _quote_cwd_for_cd(self, cwd: str) -> str:
        if not cwd or cwd == "~":
            return "$HOME"
        if cwd.startswith("~/"):
            return f"$HOME/{shlex.quote(cwd[2:])}"
        return shlex.quote(_bash_safe_path(cwd))

    def _make_snapshot_path(self) -> str:
        return os.path.join(tempfile.gettempdir(),
                            f".odysseus-env-{self._session_id}")

    async def _spawn(self, wrapped: str) -> asyncio.subprocess.Process:
        from src.agent_tools.subprocess_tools import _WIN_BASH
        shell = _WIN_BASH or "bash"
        return await asyncio.create_subprocess_exec(
            shell, "-c", wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )


def _ensure_ssh_available() -> None:
    """Hermes: ssh.py::_ensure_ssh_available — fail with a fixable message."""
    if not shutil.which("ssh"):
        raise RuntimeError("SSH is not installed or not in PATH. Install an "
                           "OpenSSH client (apt install openssh-client).")


class SSHEnvironment(BaseEnvironment):
    """Run on a remote host over SSH, with ControlMaster connection reuse.

    Hermes: tools/environments/ssh.py::SSHEnvironment. Spawn-per-call — each
    execute() is a fresh `ssh ... bash -c` — with env and cwd carried by the
    base class's snapshot and marker.
    """

    def __init__(self, host: str, user: str = "", cwd: str = "~",
                 timeout: int = 120, port: int = 22, key_path: str = ""):
        super().__init__(cwd=cwd, timeout=timeout)
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.control_dir = Path(tempfile.gettempdir()) / "odysseus-ssh"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        # Hash the triple rather than using it literally: macOS enforces a
        # 104-byte sun_path limit on Unix sockets, and "user@host:port" under a
        # deeply nested $TMPDIR (/var/folders/xx/yy/T/) plus the 16-byte random
        # suffix ssh appends in ControlMaster mode blows past it. Hashing keeps
        # the path short AND stable, so reuse still works across reconnects.
        _socket_id = hashlib.sha256(
            f"{user}@{host}:{port}".encode()).hexdigest()[:16]
        self.control_socket = self.control_dir / f"{_socket_id}.sock"
        _ensure_ssh_available()
        self._remote_home = ""

    @property
    def _target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    # Hermes: ssh.py::_build_ssh_command
    def _build_ssh_command(self, extra_args: Optional[list] = None) -> list:
        cmd = ["ssh"]
        cmd.extend(["-o", f"ControlPath={self.control_socket}"])
        cmd.extend(["-o", "ControlMaster=auto"])
        # Hold the master open between calls; without it every tool call pays a
        # fresh TCP + auth handshake, which dominates short commands.
        cmd.extend(["-o", "ControlPersist=300"])
        # Never prompt: a hung password prompt inside an agent tool call is an
        # invisible deadlock, not an error.
        cmd.extend(["-o", "BatchMode=yes"])
        cmd.extend(["-o", "StrictHostKeyChecking=accept-new"])
        cmd.extend(["-o", "ConnectTimeout=10"])
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if extra_args:
            cmd.extend(extra_args)
        cmd.append(self._target)
        return cmd

    # Hermes: ssh.py::_detect_remote_home
    async def detect_remote_home(self) -> str:
        if self._remote_home:
            return self._remote_home
        cmd = self._build_ssh_command()
        cmd.append("echo $HOME")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            home = (out or b"").decode("utf-8", "replace").strip()
            if home and proc.returncode == 0:
                self._remote_home = home
                return home
        except Exception as e:
            logger.debug("SSH: remote home detection failed: %s", e)
        self._remote_home = "/root" if self.user == "root" else f"/home/{self.user or 'user'}"
        return self._remote_home

    def _make_snapshot_path(self) -> str:
        # Remote path; /tmp exists on every POSIX host we can ssh into.
        return f"/tmp/.odysseus-env-{self._session_id}"

    # Hermes: ssh.py::_run_bash
    async def _spawn(self, wrapped: str) -> asyncio.subprocess.Process:
        cmd = self._build_ssh_command()
        cmd.extend(["bash", "-c", shlex.quote(wrapped)])
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )

    async def execute(self, command: str, cwd: str = "", *,
                      timeout: Optional[int] = None,
                      progress_cb=None) -> Dict[str, Any]:
        if not self._remote_home:
            await self.detect_remote_home()
        result = await super().execute(command, cwd, timeout=timeout,
                                       progress_cb=progress_cb)
        # ssh's own 255 (unreachable, auth refused) is not the command's exit
        # code. Reporting it as one sends the agent debugging a build that
        # never ran.
        if result.get("exit_code") == 255 and not (result.get("output") or "").strip():
            detail = (result.get("stderr") or "").strip()[:400]
            return {"error": f"ssh to {self._target} failed: {detail or 'no detail'}. "
                             f"Check the host, your keys, and reachability.",
                    "exit_code": 255}
        return result

    # Hermes: ssh.py::cleanup
    async def cleanup(self) -> None:
        if not self.control_socket.exists():
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", f"ControlPath={self.control_socket}",
                "-O", "exit", self._target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        try:
            self.control_socket.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

_ENV_CACHE: Dict[str, BaseEnvironment] = {}


def _setting(key: str, default):
    try:
        from src.settings import get_setting
        v = get_setting(key, default)
        return default if v is None else v
    except Exception:
        return default


def parse_target(raw: str) -> tuple:
    """("local", "") or ("ssh", "user@host"). Unknown values fall back local.

    A typo must not take out every tool the agent has; running locally is
    visible and recoverable in a way that a dead toolset is not.
    """
    raw = (raw or "").strip()
    if not raw or raw.lower() == "local":
        return "local", ""
    if raw.lower().startswith("ssh:"):
        host = raw[4:].strip()
        if host:
            return "ssh", host
        logger.warning("agent_execution_target is 'ssh:' with no host — local")
        return "local", ""
    logger.warning("agent_execution_target %r not understood "
                   "(expected 'local' or 'ssh:<host>') — local", raw)
    return "local", ""


def get_environment(target: Optional[str] = None) -> BaseEnvironment:
    """The environment every tool must route through — shell AND files.

    Cached per target so ControlMaster, the env snapshot and the cwd all
    persist across tool calls instead of resetting on every command.
    """
    raw = target if target is not None else str(
        _setting("agent_execution_target", "local") or "local")
    kind, host = parse_target(raw)
    key = f"{kind}:{host}"
    env = _ENV_CACHE.get(key)
    if env is not None:
        return env
    if kind == "ssh":
        user, _, hostname = host.rpartition("@")
        ws = str(_setting("agent_ssh_workspace", "") or "").strip() or "~"
        try:
            env = SSHEnvironment(
                host=hostname, user=user, cwd=ws,
                port=int(_setting("agent_ssh_port", 22) or 22),
                key_path=str(_setting("agent_ssh_key", "") or "").strip(),
                timeout=int(_setting("agent_ssh_timeout", 120) or 120))
        except Exception as e:
            logger.warning("SSH environment unavailable (%s) — using local", e)
            env = LocalEnvironment()
            key = "local:"
    else:
        env = LocalEnvironment()
    _ENV_CACHE[key] = env
    return env


def is_remote(target: Optional[str] = None) -> bool:
    raw = target if target is not None else str(
        _setting("agent_execution_target", "local") or "local")
    return parse_target(raw)[0] != "local"


async def reset_environments() -> None:
    """Drop cached environments, closing SSH masters."""
    for env in list(_ENV_CACHE.values()):
        try:
            await env.cleanup()
        except Exception:
            pass
    _ENV_CACHE.clear()
