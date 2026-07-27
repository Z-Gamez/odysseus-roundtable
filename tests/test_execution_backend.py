"""The agent's shell/python tools can run somewhere other than this machine.

Everything executed on the box hosting Odysseus, which ties the heaviest work
(Gradle, Flutter, test suites, the Round Table build gate) to whatever laptop
is running the UI.

Note what this deliberately does NOT do: it does not move the model. Endpoint
base_url already accepts any host, so "run a bigger model than this laptop
fits" is an endpoint change. Routing shell commands over SSH would only add
latency to that problem.
"""
import asyncio

import pytest

from src import execution_backend as eb


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    values = {"agent_execution_target": "local", "agent_ssh_workspace": "",
              "agent_ssh_port": "", "agent_ssh_connect_timeout": 10}
    monkeypatch.setattr(eb, "_setting", lambda k, d: values.get(k, d))
    return values


# ── target parsing ───────────────────────────────────────────────────────────

def test_default_is_local():
    assert eb.resolve_target() == ("local", "")
    assert eb.is_remote() is False


def test_ssh_target_parses():
    assert eb.resolve_target("ssh:builder@gpu-box") == ("ssh", "builder@gpu-box")
    assert eb.is_remote("ssh:builder@gpu-box") is True


def test_blank_and_local_are_local():
    assert eb.resolve_target("") == ("local", "")
    assert eb.resolve_target("LOCAL") == ("local", "")


def test_malformed_target_falls_back_to_local():
    """A typo must not make every shell command fail.

    Running locally is recoverable and visible; a hard error here takes out
    every tool the agent has.
    """
    assert eb.resolve_target("sssh:host") == ("local", "")
    assert eb.resolve_target("ssh:") == ("local", "")
    assert eb.resolve_target("gpu-box") == ("local", "")


# ── remote command construction ──────────────────────────────────────────────

def test_command_runs_in_the_remote_workspace(_settings):
    _settings["agent_ssh_workspace"] = "/srv/work"
    cmd = eb.build_remote_command("gradle test")
    # shlex.quote leaves a shell-safe path bare; the whole inner command is
    # what gets quoted for the ssh argv. See test_workspace_is_quoted for the
    # case where quoting actually matters.
    assert "cd /srv/work && gradle test" in cmd


def test_missing_cd_fails_instead_of_running_in_the_login_dir(_settings):
    """`cd X && cmd`, never `cd X; cmd`.

    With a semicolon a bad workspace runs the build in the SSH login directory
    and reports success for work done nowhere near the source.
    """
    _settings["agent_ssh_workspace"] = "/srv/work"
    assert "&&" in eb.build_remote_command("make")
    assert "; make" not in eb.build_remote_command("make")


def test_blank_workspace_skips_the_cd(_settings):
    assert "cd " not in eb.build_remote_command("echo hi")


def test_workspace_is_quoted(_settings):
    _settings["agent_ssh_workspace"] = "/srv/my projects"
    assert "'/srv/my projects'" in eb.build_remote_command("ls")


def test_login_shell_is_used(_settings):
    """Toolchains from nvm/sdkman/pyenv live in the login profile; without -l a
    remote build fails with 'gradle: not found' on a box that has gradle."""
    assert eb.build_remote_command("gradle test").startswith("bash -lc ")


def test_explicit_cwd_overrides_the_setting(_settings):
    _settings["agent_ssh_workspace"] = "/srv/work"
    assert "/tmp/other" in eb.build_remote_command("ls", cwd="/tmp/other")


# ── running ──────────────────────────────────────────────────────────────────

def _fake_exec(monkeypatch, *, stdout="", stderr="", rc=0, timed_out=False,
               captured=None):
    async def fake_create(*argv, **kw):
        if captured is not None:
            captured.extend(argv)
        return object()

    async def fake_stream(proc, timeout=None, progress_cb=None):
        return stdout, stderr, rc, timed_out

    monkeypatch.setattr(eb.asyncio, "create_subprocess_exec", fake_create)
    import src.agent_tools.subprocess_tools as st
    monkeypatch.setattr(st, "_run_subprocess_streaming", fake_stream)


def test_local_target_refuses_this_path():
    """Callers must use the local path directly; a silent local run here would
    hide a misconfigured target."""
    with pytest.raises(RuntimeError):
        asyncio.run(eb.run("ls", timeout=5, target="local"))


def test_remote_run_returns_output(monkeypatch):
    _fake_exec(monkeypatch, stdout="built ok\n", rc=0)
    r = asyncio.run(eb.run("make", timeout=5, target="ssh:box"))
    assert r["exit_code"] == 0 and "built ok" in r["stdout"]


def test_ssh_transport_failure_is_not_a_command_failure(monkeypatch):
    """ssh exits 255 for ITS OWN failures — unreachable host, refused auth.

    Reporting that as the command's exit code sends the agent off debugging a
    build that never ran.
    """
    _fake_exec(monkeypatch, stderr="ssh: connect to host box port 22: refused",
               rc=255)
    r = asyncio.run(eb.run("make", timeout=5, target="ssh:box"))
    assert r["exit_code"] == 255
    assert "ssh to box failed" in r["error"]
    assert "reachability" in r["error"]


def test_a_real_255_from_the_command_is_preserved(monkeypatch):
    """A command legitimately exiting 255 with output is not a transport
    failure and must not be relabelled."""
    _fake_exec(monkeypatch, stdout="exit code 255 from the tool\n", rc=255)
    r = asyncio.run(eb.run("make", timeout=5, target="ssh:box"))
    assert "error" not in r


def test_timeout_is_reported_with_the_host(monkeypatch):
    _fake_exec(monkeypatch, timed_out=True, rc=None)
    r = asyncio.run(eb.run("sleep 999", timeout=3, target="ssh:box"))
    assert r["exit_code"] == 124 and "box" in r["error"]


def test_bad_host_is_rejected_not_executed(monkeypatch):
    """_ssh_exec_argv rejects hosts starting with '-' so a crafted target
    cannot smuggle ssh flags."""
    r = asyncio.run(eb.run("ls", timeout=5, target="ssh:-oProxyCommand=evil"))
    assert r["exit_code"] == 1 and "not usable" in r["error"]


def test_port_is_passed_through(monkeypatch, _settings):
    _settings["agent_ssh_port"] = "2222"
    captured = []
    _fake_exec(monkeypatch, captured=captured)
    asyncio.run(eb.run("ls", timeout=5, target="ssh:box"))
    assert "-p" in captured and "2222" in captured


# ── the split-filesystem hazard ──────────────────────────────────────────────
#
# The filesystem tools call open() on the Odysseus host; bash and python run on
# the remote one. Unguarded, the agent writes app.py locally, runs `cat app.py`
# remotely and gets "no such file" -- or worse, compiles a stale checkout that
# happens to exist over there and reports a clean pass for code it never built.
# Wrong answers, not a crash, which is the expensive kind.

def test_local_fs_is_allowed_when_execution_is_local():
    assert eb.local_fs_unavailable("write_file") is None


def test_local_fs_is_refused_when_execution_is_remote(_settings):
    _settings["agent_execution_target"] = "ssh:builder@gpu-box"
    err = eb.local_fs_unavailable("write_file")
    assert err is not None and err["exit_code"] == 1


def test_refusal_tells_the_agent_what_to_do_instead(_settings):
    """The agent reads this mid-loop; a bare 'not supported' just stalls it.

    bash IS remote, so heredoc/cat is a genuinely working path — naming it
    keeps one filesystem in play instead of two.
    """
    _settings["agent_execution_target"] = "ssh:builder@gpu-box"
    msg = eb.local_fs_unavailable("write_file")["error"]
    assert "bash" in msg
    assert "agent_execution_target" in msg
    assert "gpu-box" in msg, "name the host so the mismatch is obvious"


def test_every_local_fs_tool_is_guarded():
    """A tool added later without the guard reintroduces silent divergence."""
    import inspect
    from src.agent_tools import filesystem_tools as ft
    for cls in ("EditFileTool", "ReadFileTool", "WriteFileTool", "ApplyPatchTool",
                "LsTool", "GlobTool", "GrepTool"):
        src = inspect.getsource(getattr(ft, cls))
        assert "local_fs_unavailable" in src, (
            f"{cls} touches the local filesystem but does not check the "
            f"execution target; under ssh: it would write where bash cannot see"
        )


def test_get_workspace_reports_the_remote_path(monkeypatch, _settings):
    """Informational, so report rather than refuse — but the LOCAL path is the
    one place bash will never look."""
    _settings["agent_execution_target"] = "ssh:builder@gpu-box"
    _settings["agent_ssh_workspace"] = "/srv/work"
    from src.agent_tools import filesystem_tools as ft
    out = asyncio.run(ft.GetWorkspaceTool().execute("", {}))["output"]
    assert "/srv/work" in out and "gpu-box" in out
