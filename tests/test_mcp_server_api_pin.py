"""The built-in MCP servers depend on an API that is not stable across releases.

All four servers in mcp_servers/ are built on the low-level
@server.list_tools() / @server.call_tool() decorators. A later `mcp` release
removed them, and because requirements.txt listed a bare `mcp`, a fresh CI
install picked up that version and every built-in server crashed on startup
with AttributeError -- while dev machines holding an older wheel kept working,
so it only surfaced in a shipped build.
"""
import re


def test_mcp_is_pinned():
    """A bare `mcp` means each build gambles on whatever PyPI published today."""
    reqs = open("requirements.txt", encoding="utf-8").read()
    line = next((l.strip() for l in reqs.splitlines()
                 if re.match(r"^mcp\b", l.strip())), None)
    assert line, "mcp missing from requirements.txt"
    assert re.search(r"[=<>~]", line), (
        f"mcp must be version-constrained — the Server decorator API changes "
        f"between releases and an unpinned install crashes every built-in "
        f"server. Got: {line!r}"
    )


def test_the_installed_mcp_still_has_the_decorators():
    """Guards the pin itself: if it is ever loosened to a version without these,
    this fails here rather than in a shipped app."""
    from mcp.server import Server
    for attr in ("list_tools", "call_tool"):
        assert hasattr(Server, attr), (
            f"mcp.server.Server has no {attr} — every server in mcp_servers/ "
            f"uses it as a decorator and will fail at import"
        )


def test_the_servers_actually_register_against_it():
    """Exercise the decorators rather than trusting hasattr."""
    import asyncio
    from mcp.server import Server
    s = Server("probe")

    @s.list_tools()
    async def _lt():
        return []

    @s.call_tool()
    async def _ct(name, args):
        return []

    assert asyncio.run(_lt()) == []


def test_browser_mcp_spawn_env_carries_node_on_path():
    """npx is a shell script whose shebang runs `node`.

    Resolving npx absolutely is not enough: launched from launchd (or any
    non-login context) PATH is the bare /usr/bin:/bin:/usr/sbin:/sbin and the
    child dies with "env: node: No such file or directory" even though npx was
    found.
    """
    import inspect
    from src import builtin_mcp
    src = inspect.getsource(builtin_mcp)
    idx = src.index("PLAYWRIGHT_BROWSERS_PATH")
    window = src[max(0, idx - 1800):idx + 400]
    assert '"PATH"' in window, "spawn env must set PATH or node will not be found"
    assert "which(\"node\")" in window or "which('node')" in window, (
        "PATH must include node's own directory, not just inherit"
    )
