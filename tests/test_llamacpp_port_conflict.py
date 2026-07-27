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
