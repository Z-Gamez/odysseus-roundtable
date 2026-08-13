"""On-demand start / idle stop for ComfyUI.

It is not autostarted on purpose: idle it holds ~1-2GB of RAM and, with a
checkpoint loaded, several GB of VRAM that the local LLM wants on the same
card. Measured on this machine, stopping it returned 9.2GB of RSS. So it is
launched on the first image request and released after a quiet spell.

The rule that must not break: a ComfyUI the USER started is never killed by our
idle timer — they may be working in its web UI. Ownership is tracked in a
pidfile because the image tool runs in a separate MCP process from the
supervisor, so a live process handle would not be visible to both.
"""
import asyncio
import os

import pytest

from src import comfyui_launcher as L


@pytest.fixture(autouse=True)
def isolated_pidfile(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_pidfile", lambda: str(tmp_path / "comfyui.pid"))
    L._last_request = 0.0
    yield


def test_a_user_started_comfyui_is_not_ours(tmp_path):
    """No pidfile means we did not start it, so the idle timer must leave it be."""
    assert L.we_started_it() is False


def test_a_stale_pidfile_is_not_ours(monkeypatch):
    """A pid from a previous boot may now belong to something else entirely —
    killing it would be someone else's process."""
    L._write_pid(999_999_999)
    assert L.we_started_it() is False
    # and the stale file is cleaned up rather than retried forever
    assert L._read_pid() is None


def test_our_live_process_is_recognised():
    L._write_pid(os.getpid())
    assert L.we_started_it() is True


def test_stop_without_a_pidfile_does_nothing():
    assert L.stop() is False


def test_idle_loop_never_stops_a_process_we_do_not_own(monkeypatch):
    stopped = []
    monkeypatch.setattr(L, "stop", lambda: stopped.append(1))
    monkeypatch.setattr(L, "we_started_it", lambda: False)
    monkeypatch.setattr(L, "_setting", lambda k, d: 1 if "idle" in k else d)
    L._last_request = 1.0                      # long idle
    asyncio.run(_one_tick())
    assert not stopped, "the user's own ComfyUI must never be stopped by us"


def test_idle_loop_stops_our_own_when_quiet(monkeypatch):
    stopped = []
    monkeypatch.setattr(L, "stop", lambda: stopped.append(1))
    monkeypatch.setattr(L, "we_started_it", lambda: True)
    monkeypatch.setattr(L, "_setting", lambda k, d: 1 if "idle" in k else d)
    L._last_request = 1.0                      # epoch — very idle
    asyncio.run(_one_tick())
    assert stopped


def test_a_freshly_started_server_gets_a_grace_period(monkeypatch):
    """Started but never asked for anything yet: start the clock instead of
    killing something that has had no chance to be used."""
    stopped = []
    monkeypatch.setattr(L, "stop", lambda: stopped.append(1))
    monkeypatch.setattr(L, "we_started_it", lambda: True)
    monkeypatch.setattr(L, "_setting", lambda k, d: 1 if "idle" in k else d)
    L._last_request = 0.0
    asyncio.run(_one_tick())
    assert not stopped
    assert L._last_request > 0, "the idle clock should have been started"


async def _one_tick():
    """Run the supervisor body once without waiting out its real interval."""
    task = asyncio.create_task(L._loop(interval=0))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_supervisor_is_off_when_the_timeout_is_zero(monkeypatch):
    monkeypatch.setattr(L, "_setting", lambda k, d: 0 if "idle" in k else d)
    assert L.start_supervisor() is None


def test_ensure_running_returns_false_without_an_install(monkeypatch):
    """No ComfyUI installed is a normal state, not an error — the image tool
    falls through to a configured provider."""
    monkeypatch.setattr(L, "install_dir", lambda: "")

    async def not_answering(*a, **k):
        return False

    monkeypatch.setattr(L, "is_answering", not_answering)
    assert asyncio.run(L.ensure_running()) is False


def test_ensure_running_does_not_relaunch_a_live_server(monkeypatch):
    launched = []

    async def answering(*a, **k):
        return True

    monkeypatch.setattr(L, "is_answering", answering)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: launched.append(1))
    assert asyncio.run(L.ensure_running()) is True
    assert not launched, "a second server on the same port would fight the first"


def test_launch_is_windowless_on_windows(monkeypatch):
    """A console flashing up whenever the user asks for a picture is its own
    bug report — the flags must include CREATE_NO_WINDOW."""
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows-only launch flags")
    seen = {}

    class P:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(argv, **kw):
        seen.update(kw)
        return P()

    async def answering_after(*a, **k):
        return len(seen) > 0        # not up before launch, up after

    monkeypatch.setattr(L, "install_dir", lambda: str(os.getcwd()))
    monkeypatch.setattr(L, "_launch_argv", lambda root: ["python", "-s", "main.py"])
    monkeypatch.setattr(L, "_port_in_use", lambda: False)
    monkeypatch.setattr(L, "_maybe_warm", lambda: None)
    monkeypatch.setattr(L, "is_answering", answering_after)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    asyncio.run(L.ensure_running(timeout=5))
    assert seen.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW


# ── a slow first load must not look like "no server" ───────────────────────
#
# While a 6GB checkpoint pages in, ComfyUI stops answering /system_stats. The
# health check said "not running", so ensure_running launched a SECOND server
# onto the busy port; it collided, exited, and left the pidfile pointing at a
# corpse. That is how a merely-slow first run turned into cold-every-time.


def test_a_busy_server_is_waited_for_not_relaunched(monkeypatch):
    launched = []
    answers = {"n": 0}

    async def answering(*a, **k):
        answers["n"] += 1
        return answers["n"] > 2        # silent at first, then responds

    monkeypatch.setattr(L, "is_answering", answering)
    monkeypatch.setattr(L, "_port_in_use", lambda: True)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: launched.append(1))
    monkeypatch.setattr(L, "install_dir", lambda: "/tmp/comfy")
    monkeypatch.setattr(L, "_launch_argv", lambda root: ["python", "main.py"])
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    assert asyncio.run(L.ensure_running(timeout=30)) is True
    assert not launched, "a second server on a busy port collides with the first"


def test_a_dead_port_still_launches(monkeypatch):
    """The guard must not stop a genuine cold start."""
    launched = []

    class P:
        pid = 1234
        def poll(self): return None

    calls = {"n": 0}

    async def answering(*a, **k):
        # Two checks happen before any launch — before the lock and inside it.
        calls["n"] += 1
        return calls["n"] > 2          # only answers once the launch has run

    monkeypatch.setattr(L, "is_answering", answering)
    monkeypatch.setattr(L, "_port_in_use", lambda: False)
    monkeypatch.setattr(L, "install_dir", lambda: "/tmp/comfy")
    monkeypatch.setattr(L, "_launch_argv", lambda root: ["python", "main.py"])
    monkeypatch.setattr(L, "_maybe_warm", lambda: None)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: launched.append(1) or P())
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    assert asyncio.run(L.ensure_running(timeout=30)) is True
    assert launched, "nothing was listening, so it should have started one"


async def _no_sleep(*a, **k):
    return None


def test_warmup_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(L, "_setting", lambda k, d: False if "warm" in k else d)
    started = []
    monkeypatch.setattr(asyncio, "get_running_loop",
                        lambda: started.append(1))     # would be used if it ran
    L._maybe_warm()
    assert not started


def test_warmup_failure_is_harmless(monkeypatch):
    """A failed warm-up must never fail the launch — the real request will
    load the checkpoint itself."""
    monkeypatch.setattr(L, "_setting", lambda k, d: True if "warm" in k else d)

    def boom():
        raise RuntimeError("no loop")

    monkeypatch.setattr(asyncio, "get_running_loop", boom)
    L._maybe_warm()      # must not raise
