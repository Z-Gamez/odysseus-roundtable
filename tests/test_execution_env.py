"""Execution environments, ported from Hermes Agent (MIT, Nous Research).

Two things this has to get right, and both are invisible until they break.

STATE. Every execute() spawns a fresh process, so there is no shell to hold
`cd build` or `export FOO=1`. Hermes carries them anyway: env vars through a
sourced snapshot, cwd through a marker printed into stdout and parsed back out.
Without that, the agent cd's into a directory and the very next tool call is
somewhere else — and nothing reports an error.

ONE FILESYSTEM. File tools go through the SAME environment as bash. The earlier
design here kept open() local while bash ran remote, so the agent wrote a file
in one place and looked for it in another; it had to be papered over by
refusing file tools entirely while remote.
"""
import asyncio

import pytest

from src import execution_env as ee


@pytest.fixture(autouse=True)
def _clean_cache():
    ee._ENV_CACHE.clear()
    yield
    ee._ENV_CACHE.clear()


# ── target parsing ───────────────────────────────────────────────────────────

def test_default_and_blank_are_local():
    assert ee.parse_target("local") == ("local", "")
    assert ee.parse_target("") == ("local", "")


def test_ssh_target_parses():
    assert ee.parse_target("ssh:builder@gpu-box") == ("ssh", "builder@gpu-box")


def test_malformed_target_falls_back_to_local():
    """A typo must not take out every tool the agent has."""
    for bad in ("sssh:host", "ssh:", "gpu-box"):
        assert ee.parse_target(bad) == ("local", "")


def test_environments_are_cached_per_target():
    """A fresh environment per call would reset cwd and the env snapshot every
    command, and reopen the SSH master each time."""
    a = ee.get_environment("local")
    b = ee.get_environment("local")
    assert a is b


# ── the command wrapper (Hermes base.py::_wrap_command) ──────────────────────

def _wrap(cmd="echo hi", cwd="/tmp"):
    return ee.LocalEnvironment()._wrap_command(cmd, cwd)


def test_wrapper_restores_cwd_and_env():
    w = _wrap()
    assert "builtin cd -- /tmp" in w
    assert "source " in w, "env vars from previous commands must be restored"


def test_cd_failure_aborts_instead_of_running_elsewhere():
    """`cd X || exit 126`. Without the guard a missing directory runs the
    command in whatever the previous cwd was and reports success."""
    assert "|| exit 126" in _wrap()


def test_wrapper_preserves_the_commands_exit_code():
    """The wrapper appends bookkeeping after the command; a naive wrapper
    returns the exit code of the LAST line and every failure looks like a
    success."""
    w = _wrap()
    assert "__ody_ec=$?" in w and "exit $__ody_ec" in w


def test_snapshot_write_is_atomic():
    """Concurrent commands both dump env; a non-atomic write lets one source a
    half-written file. Assemble to a per-PID temp, then mv."""
    w = _wrap()
    assert "$BASHPID" in w, "shared temp name would let writers clobber"
    assert "mv -f" in w


def test_snapshot_is_not_world_readable():
    """Snapshots carry exported env, which can include secrets."""
    assert "umask 077" in _wrap()


def test_bare_tilde_is_not_quoted_into_a_literal():
    """`cd '~'` looks for a directory literally named ~."""
    env = ee.LocalEnvironment()
    assert env._quote_cwd_for_cd("~") == "$HOME"
    assert env._quote_cwd_for_cd("~/my dir") == "$HOME/'my dir'"


def test_paths_with_spaces_survive():
    assert "'/tmp/my dir'" in _wrap(cwd="/tmp/my dir")


# ── the CWD marker round-trip ────────────────────────────────────────────────

def test_marker_updates_cwd_and_is_stripped_from_output():
    env = ee.LocalEnvironment()
    m = env._cwd_marker
    result = {"output": f"hello\n{m}/srv/project{m}\n"}
    env._extract_cwd_from_output(result)
    assert env.cwd == "/srv/project"
    assert result["output"] == "hello", "the marker leaked into the model's view"


def test_output_without_a_trailing_newline_is_not_corrupted():
    """printf 'exact' emits no newline; the wrapper injects one before the
    marker and it has to come back off again."""
    env = ee.LocalEnvironment()
    m = env._cwd_marker
    result = {"output": f"exact\n{m}/tmp{m}\n"}
    env._extract_cwd_from_output(result)
    assert result["output"] == "exact"


def test_missing_marker_leaves_output_alone():
    env = ee.LocalEnvironment()
    before = env.cwd
    result = {"output": "no marker here"}
    env._extract_cwd_from_output(result)
    assert result["output"] == "no marker here" and env.cwd == before


# ── SSH construction (Hermes ssh.py) ─────────────────────────────────────────

def _ssh():
    return ee.SSHEnvironment(host="gpu-box", user="builder")


def test_ssh_reuses_one_connection():
    """Without ControlMaster every tool call pays a fresh TCP + auth
    handshake, which dominates short commands."""
    cmd = _ssh()._build_ssh_command()
    assert "ControlMaster=auto" in cmd
    assert "ControlPersist=300" in cmd


def test_ssh_never_prompts():
    """A password prompt inside a tool call is an invisible deadlock, not an
    error the agent can see."""
    assert "BatchMode=yes" in _ssh()._build_ssh_command()


def test_control_socket_path_stays_short():
    """macOS caps Unix socket paths at 104 bytes. user@host:port under a nested
    $TMPDIR, plus the random suffix ssh appends, blows past it — so the triple
    is hashed."""
    env = _ssh()
    assert len(env.control_socket.name) <= 24
    assert "builder" not in env.control_socket.name


def test_control_socket_is_stable_for_the_same_host():
    """A changing path defeats connection reuse entirely."""
    assert _ssh().control_socket == _ssh().control_socket


def test_different_hosts_get_different_sockets():
    a = ee.SSHEnvironment(host="box-a", user="u").control_socket
    b = ee.SSHEnvironment(host="box-b", user="u").control_socket
    assert a != b


def test_ssh_snapshot_path_is_remote():
    """A Windows temp path as the snapshot would break every remote command."""
    assert _ssh()._snapshot_path.startswith("/tmp/")


# ── end to end, locally ──────────────────────────────────────────────────────

@pytest.mark.skipif(not __import__("shutil").which("bash"),
                    reason="needs bash")
def test_state_really_persists_across_calls():
    """THE point of the port: separate processes, shared state."""
    async def _go():
        env = ee.LocalEnvironment()
        # A POSIX path on purpose: this string goes inside the COMMAND, which
        # bash sees verbatim. Only paths the wrapper itself interpolates get
        # the Windows->MSYS rewrite, so a native C:\... here would just fail
        # to cd and prove nothing about state.
        await env.execute("cd /tmp && export ODY_TEST_VAR=carried")
        # A second, entirely separate process:
        r = await env.execute("echo $ODY_TEST_VAR; pwd")
        return env, r
    env, r = asyncio.run(_go())
    out = (r.get("output") or "")
    assert "carried" in out, ("the env snapshot did not survive; every export "
                              "is forgotten between tool calls")
    assert "/tmp" in out, "cwd did not survive; every cd is forgotten"
    assert env.cwd == "/tmp", "the CWD marker did not update the environment"
    assert env._cwd_marker not in out, "marker leaked to the model"
