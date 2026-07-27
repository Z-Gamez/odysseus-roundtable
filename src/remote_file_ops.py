"""File operations as shell commands, run through the execution environment.

Hermes' key structural choice (tools/file_operations.py: every op ends in
`self.env.execute(command, cwd=...)`) is that file tools are NOT a separate
code path from the shell. They build a command and hand it to the same
environment. Whatever host that environment targets is the one and only
filesystem the agent can see.

That is what makes a remote target coherent instead of dangerous. The earlier
approach here kept open() on the Odysseus host while bash ran elsewhere, so the
agent wrote app.py in one place and looked for it in another — and had to be
protected with a refusal. Routing the ops through the environment removes the
seam rather than fencing it off.

Local execution deliberately keeps the existing open() paths: same filesystem,
no behaviour change, and no reason to pay a shell round-trip per read.
"""
from __future__ import annotations

import shlex
from typing import Any, Dict

# Marker for heredoc writes. Random enough that file content cannot terminate
# the heredoc early — a body containing the delimiter would otherwise truncate
# the write and silently produce a half-written file.
_EOF = "ODY_FILE_EOF_7f3a91c4"


async def _run(command: str, *, timeout: int = 60) -> Dict[str, Any]:
    from src import execution_env
    return await execution_env.get_environment().execute(command, timeout=timeout)


async def read_file(path: str, *, timeout: int = 60) -> Dict[str, Any]:
    r = await _run(f"cat -- {shlex.quote(path)}", timeout=timeout)
    if r.get("error"):
        return r
    if r.get("exit_code"):
        return {"error": f"cannot read {path}: "
                         f"{(r.get('stderr') or 'no such file').strip()}",
                "exit_code": r.get("exit_code")}
    return {"output": r.get("output", ""), "exit_code": 0}


async def write_file(path: str, content: str, *, timeout: int = 60) -> Dict[str, Any]:
    """Write via a quoted heredoc.

    The delimiter is quoted ('EOF') so the remote shell performs NO expansion
    on the body — otherwise a file containing $VAR or backticks would be
    silently rewritten on the way to disk, which is data corruption that only
    shows up much later.
    """
    parent = shlex.quote(path.rsplit("/", 1)[0]) if "/" in path else "."
    cmd = (f"mkdir -p {parent} && cat > {shlex.quote(path)} <<'{_EOF}'\n"
           f"{content}\n{_EOF}")
    r = await _run(cmd, timeout=timeout)
    if r.get("error"):
        return r
    if r.get("exit_code"):
        return {"error": f"cannot write {path}: "
                         f"{(r.get('stderr') or 'write failed').strip()}",
                "exit_code": r.get("exit_code")}
    return {"output": f"Wrote {len(content)} chars to {path}", "exit_code": 0}


async def list_dir(path: str, *, timeout: int = 60) -> Dict[str, Any]:
    r = await _run(f"ls -la -- {shlex.quote(path)}", timeout=timeout)
    if r.get("error"):
        return r
    if r.get("exit_code"):
        return {"error": f"cannot list {path}: "
                         f"{(r.get('stderr') or 'no such directory').strip()}",
                "exit_code": r.get("exit_code")}
    return {"output": r.get("output", ""), "exit_code": 0}


async def glob(pattern: str, root: str = ".", *, timeout: int = 60) -> Dict[str, Any]:
    # -path over -name so a pattern with slashes ("src/**/*.py") behaves the way
    # the caller wrote it instead of matching only the basename.
    r = await _run(
        f"find {shlex.quote(root)} -path {shlex.quote(pattern)} 2>/dev/null | head -500",
        timeout=timeout)
    return {"output": r.get("output", ""), "exit_code": 0}


async def grep(pattern: str, root: str = ".", *, timeout: int = 60) -> Dict[str, Any]:
    # grep exits 1 on "no matches", which is not an error condition — reporting
    # it as one makes the agent think the search failed and retry it.
    r = await _run(
        f"grep -rn -- {shlex.quote(pattern)} {shlex.quote(root)} 2>/dev/null | head -200",
        timeout=timeout)
    out = r.get("output", "")
    if not out.strip():
        return {"output": f"No matches for {pattern!r} under {root}", "exit_code": 0}
    return {"output": out, "exit_code": 0}


async def edit_file(path: str, old: str, new: str, *,
                    timeout: int = 60) -> Dict[str, Any]:
    """Read-modify-write through the environment.

    Done in Python on the read content rather than with sed: sed would need the
    caller's literal text escaped into a regex, and a stray '.' or '*' in the
    old string would match the wrong span and corrupt the file.
    """
    r = await read_file(path, timeout=timeout)
    if r.get("error"):
        return r
    body = r.get("output", "")
    count = body.count(old)
    if count == 0:
        return {"error": f"old_string not found in {path}", "exit_code": 1}
    if count > 1:
        return {"error": f"old_string appears {count} times in {path} — "
                         f"make it unique so the right one is replaced",
                "exit_code": 1}
    return await write_file(path, body.replace(old, new, 1), timeout=timeout)


def _parse(content: str) -> Dict[str, Any]:
    """Args come either as JSON, or as first-line-path + body."""
    import json
    s = (content or "").strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return d
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    head, _, body = (content or "").partition("\n")
    return {"path": head.strip(), "content": body, "pattern": head.strip()}


async def dispatch(tool: str, content: str) -> Dict[str, Any]:
    """Route a file tool call to its remote equivalent.

    Paths are used AS WRITTEN, not run through the local workspace resolver:
    that resolver confines paths to the workspace on this machine, which has
    nothing to do with where the command will actually run. Forcing a local
    root onto a remote path is how you end up reading a file that only exists
    on the laptop.
    """
    a = _parse(content)
    path = str(a.get("path") or a.get("file_path") or "").strip()
    if tool in ("read_file", "ReadFile"):
        return await read_file(path)
    if tool in ("write_file", "WriteFile"):
        return await write_file(path, str(a.get("content") or ""))
    if tool in ("ls", "Ls"):
        return await list_dir(path or ".")
    if tool in ("glob", "Glob"):
        return await glob(str(a.get("pattern") or path or "*"),
                          str(a.get("path") or a.get("root") or "."))
    if tool in ("grep", "Grep"):
        return await grep(str(a.get("pattern") or path or ""),
                          str(a.get("path") or a.get("root") or "."))
    if tool in ("edit_file", "EditFile"):
        return await edit_file(path, str(a.get("old_string") or ""),
                               str(a.get("new_string") or ""))
    return {"error": f"{tool} is not available on a remote execution target yet; "
                     f"use the bash tool for this operation.",
            "exit_code": 1}
