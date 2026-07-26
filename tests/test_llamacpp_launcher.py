"""Optional llama.cpp server launcher.

Must stay inert unless the user opts in — an inference server nobody asked for
would silently eat GB of VRAM. Written in plain Python (not a .ps1 helper)
because the macOS window host never runs the PowerShell helpers.
"""
import os
import subprocess

import pytest

from src import llamacpp_launcher as ll


def _settings(monkeypatch, **over):
    base = {"llamacpp_enabled": True, "llamacpp_binary": "", "llamacpp_model": "",
            "llamacpp_port": 8080, "llamacpp_ctx": 8192, "llamacpp_ngl": 99,
            "llamacpp_extra_args": ""}
    base.update(over)
    import src.settings as s
    monkeypatch.setattr(s, "get_setting", lambda k, d=None: base.get(k, d))


@pytest.fixture
def no_spawn(monkeypatch):
    """Never actually launch a server; record the argv instead."""
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        seen["kwargs"] = kw

        class _P:
            pid = 4242
        return _P()

    monkeypatch.setattr(ll.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ll, "port_in_use", lambda *a, **k: False)
    return seen


def test_disabled_by_default_does_nothing(monkeypatch, no_spawn):
    _settings(monkeypatch, llamacpp_enabled=False, llamacpp_model="/x.gguf")
    assert ll.start_if_configured() is None
    assert "cmd" not in no_spawn


def test_enabled_without_model_does_nothing(monkeypatch, no_spawn):
    _settings(monkeypatch, llamacpp_model="")
    assert ll.start_if_configured() is None
    assert "cmd" not in no_spawn


def test_missing_model_file_does_nothing(monkeypatch, no_spawn):
    _settings(monkeypatch, llamacpp_model="/definitely/not/here.gguf")
    assert ll.start_if_configured() is None
    assert "cmd" not in no_spawn


def test_starts_when_configured(tmp_path, monkeypatch, no_spawn):
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x")
    binary = tmp_path / "llama-server"
    binary.write_bytes(b"x")
    _settings(monkeypatch, llamacpp_model=str(gguf), llamacpp_binary=str(binary))
    assert ll.start_if_configured() is not None
    cmd = no_spawn["cmd"]
    assert cmd[0] == str(binary)
    assert "-m" in cmd and str(gguf) in cmd
    # --jinja is mandatory: without it llama-server never emits tool_calls,
    # which would make the capability probe's answer a lie.
    assert "--jinja" in cmd


def test_does_not_double_start(tmp_path, monkeypatch, no_spawn):
    gguf = tmp_path / "m.gguf"; gguf.write_bytes(b"x")
    binary = tmp_path / "llama-server"; binary.write_bytes(b"x")
    _settings(monkeypatch, llamacpp_model=str(gguf), llamacpp_binary=str(binary))
    monkeypatch.setattr(ll, "port_in_use", lambda *a, **k: True)
    assert ll.start_if_configured() is None
    assert "cmd" not in no_spawn


def test_extra_args_are_split_not_shelled(tmp_path, monkeypatch, no_spawn):
    gguf = tmp_path / "m.gguf"; gguf.write_bytes(b"x")
    binary = tmp_path / "llama-server"; binary.write_bytes(b"x")
    _settings(monkeypatch, llamacpp_model=str(gguf), llamacpp_binary=str(binary),
              llamacpp_extra_args="--threads 8 --flash-attn")
    ll.start_if_configured()
    cmd = no_spawn["cmd"]
    assert "--threads" in cmd and "8" in cmd and "--flash-attn" in cmd
    assert no_spawn["kwargs"].get("shell") is not True   # argv, never a shell string


def test_build_command_shape():
    cmd = ll.build_command("/bin/llama-server", "/m.gguf", 8080, 4096, 33)
    assert cmd[:3] == ["/bin/llama-server", "-m", "/m.gguf"]
    for flag, val in (("--port", "8080"), ("-c", "4096"), ("-ngl", "33")):
        assert val == cmd[cmd.index(flag) + 1]


def test_find_binary_prefers_explicit(tmp_path):
    b = tmp_path / "llama-server"; b.write_bytes(b"x")
    assert ll.find_binary(str(b)) == str(b)
    # A configured-but-missing path must not silently fall back to PATH.
    assert ll.find_binary(str(tmp_path / "nope")) is None


def test_settings_default_off():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["llamacpp_enabled"] is False
    assert DEFAULT_SETTINGS["llamacpp_model"] == ""


def test_both_hosts_invoke_the_launcher():
    """Windows starts helpers in odysseus_app; macOS never runs those, so the
    Cocoa host must call the launcher itself."""
    import inspect
    import odysseus_app
    import standalone_app
    assert "start_if_configured" in inspect.getsource(odysseus_app.start_background)
    assert "start_if_configured" in inspect.getsource(standalone_app._mac_window)


def test_server_startup_invokes_the_launcher():
    """The window hosts are not the only way Odysseus runs.

    Wiring the launcher ONLY into odysseus_app/standalone_app meant that
    running the API directly — `uvicorn app:app`, Docker, a service wrapper —
    never started llama.cpp. It stayed down and every request to it returned
    503 from the dead-host cooldown, with nothing in the UI explaining why.
    The server's own startup is the one path common to every way of running it.
    """
    import inspect
    import app as app_module
    src = inspect.getsource(app_module._startup_event)
    assert "start_if_configured" in src, (
        "server startup no longer autostarts llama.cpp; anyone not launching "
        "through a window host gets 503s from an endpoint that never came up"
    )


def test_server_startup_tolerates_launcher_failure():
    """A launcher problem must never stop Odysseus itself from booting."""
    import inspect
    import app as app_module
    src = inspect.getsource(app_module._startup_event)
    idx = src.index("start_if_configured")
    window = src[max(0, idx - 300):idx + 300]
    assert "try:" in window and "except" in window


def test_agent_cannot_set_the_executable():
    """The launcher EXECUTES llamacpp_binary/extra_args at next start, so a
    settings write must not become arbitrary code execution — that would route
    around a deliberately disabled bash/python."""
    import inspect
    from src.agent_tools import admin_tools
    src = inspect.getsource(admin_tools.do_manage_settings)
    assert '"llamacpp_binary"' in src
    assert '"llamacpp_extra_args"' in src
