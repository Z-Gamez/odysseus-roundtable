"""An open socket is not proof that llama-server is the one holding it.

Real failure: WSL's port proxy (wslrelay) grabbed 8080 before llama-server
could. `port_in_use(8080)` was True, so `start_if_configured` logged "already
serving — leaving it alone" and returned. Nothing was serving /v1. Odysseus
aimed its endpoint at the proxy and every model call came back 503, with no log
line anywhere connecting that to a port conflict.

The silent return is the part that cost the most time: the launcher's one
chance to notice the problem was spent asserting everything was fine.
"""
import logging

import pytest

import src.llamacpp_launcher as LL


_LLAMA_PROPS = {"default_generation_settings": {"n_ctx": 65536},
                "chat_template_caps": {"supports_tools": True}}


def _fake_urlopen(payload=None, exc=None):
    """Stand in for urllib.request.urlopen as a context manager."""
    import json

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @staticmethod
        def read():
            return json.dumps(payload).encode()

    def _open(*a, **k):
        if exc:
            raise exc
        return _R()

    return _open


def _patch(monkeypatch, *, open_socket, payload=None, exc=None):
    monkeypatch.setattr(LL, "port_in_use", lambda *a, **k: open_socket)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(payload, exc))


def test_free_port_reports_nobody(monkeypatch):
    _patch(monkeypatch, open_socket=False)
    assert LL.identify_port(8081) is None


def test_llama_server_is_recognized(monkeypatch):
    _patch(monkeypatch, open_socket=True, payload=_LLAMA_PROPS)
    assert LL.identify_port(8081) is True


def test_foreign_listener_is_not_mistaken_for_llama(monkeypatch):
    """THE regression: a proxy answers the socket but not /props."""
    _patch(monkeypatch, open_socket=True, exc=OSError("404 Not Found"))
    assert LL.identify_port(8081) is False, (
        "anything that accepts a TCP connection was treated as llama-server; "
        "that is what let wslrelay silently stand in for the model server"
    )


def test_listener_answering_with_junk_is_not_llama(monkeypatch):
    _patch(monkeypatch, open_socket=True, payload={"hello": "i am a proxy"})
    assert LL.identify_port(8081) is False


@pytest.fixture
def _configured(monkeypatch, tmp_path):
    """Minimum viable config so start_if_configured reaches the port check."""
    model = tmp_path / "m.gguf"
    model.write_bytes(b"GGUF")
    binary = tmp_path / "llama-server"
    binary.write_text("")
    values = {"llamacpp_enabled": True, "llamacpp_model": str(model),
              "llamacpp_port": 8081, "llamacpp_binary": str(binary),
              "ollama_num_ctx": 65536, "llamacpp_ctx": 0, "llamacpp_ngl": 99,
              "llamacpp_extra_args": "", "llamacpp_alias": "Qwen3.6:35B"}
    import src.settings
    monkeypatch.setattr(src.settings, "get_setting",
                        lambda k, d=None: values.get(k, d))
    spawned = []
    monkeypatch.setattr(LL.subprocess, "Popen",
                        lambda cmd, **k: spawned.append(cmd) or _DummyProc())
    return spawned


class _DummyProc:
    pid = 4242


def test_foreign_holder_blocks_start_and_says_why(monkeypatch, caplog, _configured):
    _patch(monkeypatch, open_socket=True, exc=OSError("connection reset"))
    with caplog.at_level(logging.ERROR):
        assert LL.start_if_configured() is None
    assert not _configured, "must not try to spawn onto an occupied port"
    msg = caplog.text.lower()
    assert "not llama-server" in msg and "llamacpp_port" in msg, (
        "a port conflict has to name itself in the log; the old code returned "
        f"silently and the 503s looked like a model bug. got: {caplog.text!r}"
    )


def test_real_llama_server_is_still_left_alone(monkeypatch, _configured):
    """Idempotency must survive the stricter check."""
    _patch(monkeypatch, open_socket=True, payload=_LLAMA_PROPS)
    assert LL.start_if_configured() is None
    assert not _configured, "must not start a second llama-server"


# --- router mode -----------------------------------------------------------
#
# One llama-server process pins one GGUF, so hosting a second model used to
# mean a second port and a second endpoint row -- and on 12 GB the two models
# will not be resident together anyway. Router mode serves both from one port
# and swaps them on demand.

def _router_settings(monkeypatch, tmp_path, **over):
    ini = tmp_path / "models.ini"
    ini.write_text("[A]\nmodel = a.gguf\n")
    binary = tmp_path / "llama-server"
    binary.write_text("")
    values = {"llamacpp_enabled": True, "llamacpp_preset": str(ini),
              "llamacpp_models_max": 1, "llamacpp_port": 8080,
              "llamacpp_binary": str(binary), "llamacpp_model": "",
              "ollama_num_ctx": 65536, "llamacpp_ctx": 0, "llamacpp_ngl": 99,
              "llamacpp_extra_args": "", "llamacpp_alias": ""}
    values.update(over)
    import src.settings
    monkeypatch.setattr(src.settings, "get_setting",
                        lambda k, d=None: values.get(k, d))
    spawned = []
    monkeypatch.setattr(LL.subprocess, "Popen",
                        lambda cmd, **k: spawned.append(cmd) or _DummyProc())
    _patch(monkeypatch, open_socket=False)
    return spawned, str(ini)


def test_router_command_uses_preset_not_single_model():
    cmd = LL.build_router_command("llama-server", "m.ini", 8080, 1)
    assert "--models-preset" in cmd and "m.ini" in cmd
    assert "--models-max" in cmd and "1" in cmd
    assert "--jinja" in cmd, "no --jinja means llama-server parses no tool calls"
    assert "-m" not in cmd, "router mode must not pin a single GGUF"


def test_preset_starts_router_mode(monkeypatch, tmp_path):
    spawned, ini = _router_settings(monkeypatch, tmp_path)
    assert LL.start_if_configured() is not None
    assert len(spawned) == 1
    assert "--models-preset" in spawned[0] and ini in spawned[0]


def test_preset_wins_over_a_configured_single_model(monkeypatch, tmp_path):
    """llamacpp_model names only one model; the preset names them all."""
    spawned, _ = _router_settings(monkeypatch, tmp_path,
                                  llamacpp_model=r"C:\some\other.gguf")
    LL.start_if_configured()
    assert "--models-preset" in spawned[0]
    assert "-m" not in spawned[0], (
        "a leftover llamacpp_model silently pinned one model and hid the rest"
    )


def test_missing_preset_falls_back_instead_of_dying(monkeypatch, tmp_path):
    """A bad path must not leave the user with no server at all."""
    model = tmp_path / "m.gguf"
    model.write_bytes(b"GGUF")
    spawned, _ = _router_settings(monkeypatch, tmp_path,
                                  llamacpp_preset=str(tmp_path / "nope.ini"),
                                  llamacpp_model=str(model))
    assert LL.start_if_configured() is not None
    assert "-m" in spawned[0], "should have fallen back to single-model mode"


def test_no_preset_and_no_model_starts_nothing(monkeypatch, tmp_path):
    spawned, _ = _router_settings(monkeypatch, tmp_path,
                                  llamacpp_preset="", llamacpp_model="")
    assert LL.start_if_configured() is None
    assert not spawned


# --- network binding -------------------------------------------------------
#
# llama-server has no authentication. The bind address is therefore a security
# control, not a convenience setting: widening it exposes the model, and every
# prompt sent to it, to anything that can reach the port.

def test_default_binding_stays_on_localhost():
    """Absent explicit configuration, nothing may be exposed to the network."""
    assert LL._host_args("") == []
    assert LL._host_args("127.0.0.1") == []
    assert LL._host_args("localhost") == []


def test_explicit_bind_address_is_passed_through():
    assert LL._host_args("0.0.0.0") == ["--host", "0.0.0.0"]
    assert LL._host_args("10.0.0.5") == ["--host", "10.0.0.5"]


def test_router_and_single_model_both_honour_the_bind_address():
    """A setting that only worked in one mode would silently leave the other
    unreachable, which looks like a network fault rather than a config one."""
    r = LL.build_router_command("llama-server", "m.ini", 8080, 1, "0.0.0.0")
    assert "--host" in r and "0.0.0.0" in r
    s = LL.build_command("llama-server", "m.gguf", 8080, 4096, 99,
                         bind_host="0.0.0.0")
    assert "--host" in s and "0.0.0.0" in s


def test_localhost_default_adds_no_flag_in_either_mode():
    assert "--host" not in LL.build_router_command("llama-server", "m.ini", 8080)
    assert "--host" not in LL.build_command("llama-server", "m.gguf", 8080, 4096, 99)
