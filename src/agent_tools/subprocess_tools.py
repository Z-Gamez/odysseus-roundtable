import asyncio
import os
import re
import sys
import time
import collections
from typing import Optional, Callable, Awaitable, Tuple, Dict
from src.constants import MAX_OUTPUT_CHARS

DEFAULT_BASH_TIMEOUT = 60 * 60     # 1 hour
DEFAULT_PYTHON_TIMEOUT = 60 * 60

PROGRESS_INTERVAL_S = 2.0
PROGRESS_TAIL_LINES = 12

async def _run_subprocess_streaming(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
) -> Tuple[str, str, Optional[int], bool]:
    started = time.time()
    stdout_full: list[str] = []
    stderr_full: list[str] = []
    tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)

    async def _reader(stream, full_buf, label: str):
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            full_buf.append(decoded)
            if label == "err":
                tail.append(f"! {decoded}")
            else:
                tail.append(decoded)

    async def _progress_emitter():
        await asyncio.sleep(PROGRESS_INTERVAL_S)
        while True:
            if progress_cb:
                try:
                    await progress_cb({
                        "elapsed_s": round(time.time() - started, 1),
                        "tail": "\n".join(list(tail)),
                    })
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_INTERVAL_S)

    rd_out = asyncio.create_task(_reader(proc.stdout, stdout_full, "out"))
    rd_err = asyncio.create_task(_reader(proc.stderr, stderr_full, "err"))
    prog_task = asyncio.create_task(_progress_emitter()) if progress_cb else None

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
        for t in (rd_out, rd_err):
            t.cancel()
        if prog_task is not None:
            prog_task.cancel()
        raise
    finally:
        if prog_task is not None and not prog_task.done():
            prog_task.cancel()
            try:
                await prog_task
            except (asyncio.CancelledError, Exception):
                pass
        for t in (rd_out, rd_err):
            try:
                await asyncio.wait_for(t, timeout=1)
            except Exception:
                pass

    return (
        "\n".join(stdout_full),
        "\n".join(stderr_full),
        proc.returncode,
        timed_out,
    )

def _find_bash_on_windows() -> str:
    """Path to a real bash on Windows (Git Bash), or '' if unavailable.

    create_subprocess_shell uses cmd.exe on Windows, but the tool is NAMED
    'bash' — models rightly emit POSIX (heredocs, head, ls, redirects) and it
    exploded on cmd (observed: `python << 'EOF'` and `... | head` both exit 1
    while the model flailed for rounds). Run their commands through actual
    bash when one exists."""
    import shutil
    for cand in (
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if cand and os.path.exists(cand):
            return cand
    return ""


_WIN_BASH = _find_bash_on_windows() if os.name == "nt" else ""


# --- Windows bash command rewrites -----------------------------------------
# One colon-free path segment ("foo/"). Colons can't appear in Windows path
# segments, so excluding them keeps sed exprs and URLs out of the match.
_SEG = r"""(?:[^\s"'`<>|;&()/:]+/)"""
# A drive path buried behind a path-like prefix. Two prefix shapes:
#  - relative-ish root (./  ../  ~/  $VAR/  bare /) + 0+ segments; bare "/"
#    must not follow a word char or ":" so sed exprs (s/C:/D:/) and URLs
#    (://) keep their slashes
#  - a drive root (X:/) + 1+ segments (the joined-workspace case); requiring
#    a segment keeps s/C:/D:/ intact
_REANCHOR = re.compile(
    r"""(?:(?:\.{1,2}/|~/|\$\w+/|\$\{\w+\}/|(?<![\w.:])/)""" + _SEG + r"""*"""
    r"""|[A-Za-z]:/""" + _SEG + r"""+)"""
    r"""([A-Za-z]:/)"""
)


def rewrite_for_win_bash(content: str) -> str:
    """Make model-emitted commands safe for Git Bash (MSYS) on Windows.

    Three habits models bring from cmd.exe/Windows, each of which MSYS
    mangles into workspace junk:

    - "2>nul" redirects create a literal file named 'nul' (a reserved DOS
      device name Windows then can't delete) -> use /dev/null.
    - Backslash drive paths (C:\\foo\\bar): quoted, mkdir -p walks the
      components without recognizing the drive, creating a dir literally
      named "C:" via Cygwin's reserved-char mapping (undeletable from
      Explorer); unquoted, bash eats the backslashes ("C:ab"). Forward-slash
      drive paths resolve natively, so rewrite. The {2,} floor keeps escape
      sequences like "C:\\n" (path-less single char) untouched.
    - A drive path behind ANY prefix ("./C:/x", "$PWD/C:/x", or a
      workspace-joined "C:/ws/C:/x") is no longer drive-anchored: MSYS walks
      "C:" as a literal component and mkdir -p materializes a junk tree of
      reserved-char dirs inside the cwd (observed: the Round Table dev
      nesting <ws>/C:/Odysseus/saw-sandbox/<ws> — run rt_163dedc3ba84).
      Mirror os.path.join semantics: a drive letter mid-path resets the
      path, so drop everything before it. Loop to a fixpoint for stacked
      prefixes.
    """
    content = re.sub(r"(\d*\s*>>?)\s*nul\b", r"\1/dev/null", content, flags=re.IGNORECASE)
    content = re.sub(
        r"""([A-Za-z]):\\([^\s"'`<>|;&()]{2,})""",
        lambda m: m.group(1) + ":/" + m.group(2).replace("\\", "/"),
        content,
    )
    for _ in range(4):
        new = _REANCHOR.sub(r"\1", content)
        if new == content:
            break
        content = new
    return content


class BashTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd, _truncate
        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")
        if _WIN_BASH:
            content = rewrite_for_win_bash(content)
            proc = await asyncio.create_subprocess_exec(
                _WIN_BASH, "-c", content,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_subproc_env,
                cwd=agent_cwd(),
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                content,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_subproc_env,
                cwd=agent_cwd(),
            )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_BASH_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out:
            return {"error": f"bash: timed out after {DEFAULT_BASH_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0}

class PythonTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd, _truncate
        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")
        proc = await asyncio.create_subprocess_exec(
            (sys.executable or "python"), "-I", "-c", content,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subproc_env,
            cwd=agent_cwd(),
        )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_PYTHON_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out:
            return {"error": f"python: timed out after {DEFAULT_PYTHON_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0}
