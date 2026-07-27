"""llama-server must not hold the model resident while nobody is using it.

It has no idle timeout of its own and keeps the whole model in RAM for the life
of the process — 6.45 GB of a 16 GB machine for a 9B Q6_K at ctx 12288, whether
or not anyone is talking to it.

The risk in unloading is that the NEXT request fails. It must not: the gate
revives the server and waits for it, so a cold start costs a slow first message
rather than an error.
"""
import asyncio
import time

import pytest

from src import llamacpp_supervisor as sup


class _Proc:
    def __init__(self, alive=True, pid=4242):
        self.pid = pid
        self._alive = alive
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        return 0


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    sup._child = None
    sup._last_request = 0.0
    sup._supervisor_task = None
    values = {"llamacpp_enabled": True, "llamacpp_port": 8080,
              "llamacpp_idle_timeout_seconds": 900}
    monkeypatch.setattr(sup, "_setting", lambda k, d: values.get(k, d))
    yield values
    sup._child = None
    sup._last_request = 0.0


# ── which requests count as activity ────────────────────────────────────────

def test_requests_to_the_managed_port_count(_reset):
    assert sup.targets_llamacpp("http://localhost:8080/v1") is True


def test_any_hostname_for_that_port_counts(_reset):
    """The same server answers on localhost, the LAN address and the machine
    name; a request through any of them is still activity."""
    for url in ("http://127.0.0.1:8080/v1", "http://gpu-box.local:8080/v1",
                "http://10.0.0.5:8080/v1"):
        assert sup.targets_llamacpp(url) is True, url


def test_other_endpoints_do_not_count(_reset):
    """Ollama traffic must not keep a llama.cpp model pinned in RAM."""
    assert sup.targets_llamacpp("http://localhost:11434/v1") is False
    assert sup.targets_llamacpp("https://api.anthropic.com") is False


def test_disabled_llamacpp_never_matches(_reset):
    _reset["llamacpp_enabled"] = False
    assert sup.targets_llamacpp("http://localhost:8080/v1") is False


# ── unloading ───────────────────────────────────────────────────────────────

def test_idle_server_is_stopped():
    sup._child = _Proc()
    sup._last_request = time.time() - 1000
    assert sup._stop_child() is True
    assert sup._child is None


def test_a_busy_server_is_left_alone(_reset):
    """idle_seconds is what the loop gates on; a recent request must keep the
    model loaded or the user pays a reload mid-conversation."""
    sup._child = _Proc()
    sup.note_request()
    assert sup.idle_seconds() < 1


def test_stop_is_forced_if_terminate_is_ignored():
    """A model mid-generation can ignore SIGTERM, and the whole point is to
    release the RAM."""
    class _Stubborn(_Proc):
        def wait(self, timeout=None):
            import subprocess
            if not self.killed:
                raise subprocess.TimeoutExpired("llama-server", timeout or 0)
            return 0
    sup._child = _Stubborn()
    sup._stop_child()
    assert sup._child is None


def test_only_a_server_we_started_is_ever_stopped():
    """A llama-server the user launched themselves must survive — killing a
    foreign process would be a nasty surprise."""
    sup._child = None
    assert sup._stop_child() is False


def test_nothing_is_stopped_before_the_first_request(_reset):
    """_last_request starts at 0; treating that as "idle forever" would unload
    a server that has never been given a chance to be used."""
    sup._child = _Proc()
    assert sup.idle_seconds() == 0.0


# ── revival ─────────────────────────────────────────────────────────────────

def test_gate_revives_an_unloaded_server(monkeypatch, _reset):
    """The user must see a slow first message, never a connection error."""
    started = []

    def fake_start():
        p = _Proc()
        started.append(p)
        sup._child = p
        return p

    import src.llamacpp_launcher as LL
    monkeypatch.setattr(LL, "start_if_configured", fake_start)

    class _R:
        status_code = 200

    class _C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _R()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _C)

    assert asyncio.run(sup.ensure_running(timeout=5)) is True
    assert len(started) == 1


def test_relaunch_waits_for_the_server_to_answer(monkeypatch, _reset):
    """Returning as soon as the process spawns would hand the caller a socket
    that is not listening yet — the error this feature exists to avoid."""
    import inspect
    src = inspect.getsource(sup.ensure_running)
    assert "/v1/models" in src, "relaunch must poll readiness, not just spawn"


def test_already_running_server_is_not_relaunched(monkeypatch, _reset):
    sup._child = _Proc()

    def boom():
        raise AssertionError("must not start a second llama-server")
    import src.llamacpp_launcher as LL
    monkeypatch.setattr(LL, "start_if_configured", boom)
    assert asyncio.run(sup.ensure_running(timeout=1)) is True


def test_relaunch_is_serialised(monkeypatch, _reset):
    """Several queued requests after an unload must start ONE server, not race
    a handful onto the same port."""
    import inspect
    assert "_relaunch_lock" in inspect.getsource(sup.ensure_running)


# ── configuration + reporting ───────────────────────────────────────────────

def test_zero_timeout_disables_the_supervisor(_reset):
    _reset["llamacpp_idle_timeout_seconds"] = 0
    assert sup.start_supervisor() is None


def test_status_reports_what_the_model_costs(_reset):
    sup._child = _Proc()
    sup.note_request()
    st = sup.status()
    assert st["running"] is True and st["pid"] == 4242
    assert st["unloads_when_idle"] is True
    assert "rss_mb" in st


def test_status_is_safe_when_nothing_is_running(_reset):
    st = sup.status()
    assert st["running"] is False and st["pid"] is None
    assert st["rss_bytes"] is None


def test_relaunch_preserves_configured_launch_args():
    """Settings are re-read by start_if_configured, so llamacpp_extra_args, the
    router preset and the bind host all survive an unload/reload cycle. If the
    supervisor ever built its own argv this would silently drop them."""
    import inspect
    src = inspect.getsource(sup.ensure_running)
    assert "start_if_configured" in src
    assert "--" not in src.split("start_if_configured")[1][:400], (
        "the supervisor must delegate argv construction to the launcher"
    )
