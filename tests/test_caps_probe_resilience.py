"""A slow capability probe must not silently demote the agent to fenced mode.

Real failure, Round Table run rt_769b9c966817: the Developer looped five times
against QA, narrating tool calls it never made and emitting 24 raw
`<tool_call>`/`<function=` blocks as plain text.

The server was fine — llama-server had `--jinja`, reported
`chat_template_caps.supports_tools: true`, and returned proper native
`tool_calls` when tools were actually attached. The agent simply stopped
attaching them:

  * the capability probe had a 3s timeout,
  * llama-server mid-generation across four busy slots did not answer that fast,
  * a failed probe cached `None` for 60s (_OLLAMA_CAPS_TTL_UNKNOWN),
  * `local_native_mode` then fell through to the curated model-name list, and
    the model's name was a `.gguf` blob path that matches nothing,
  * so `_is_api_model` went False and no tool schemas were sent.

The model still tried to call tools — in its own trained syntax — and with no
tool grammar applied by llama.cpp that arrived as text. Nothing executed, the
gate never advanced, and the loop repeated.

The load that makes the probe slow is the agent's own generation, so this gets
worse exactly when it matters most.
"""
import asyncio

import httpx
import pytest

import src.llm_core as L


@pytest.fixture(autouse=True)
def _clear_caps_cache():
    L._OLLAMA_CAPS_CACHE.clear()
    yield
    L._OLLAMA_CAPS_CACHE.clear()


def _fake_client(payload=None, raise_exc=None):
    """Stand in for httpx.AsyncClient — the probe uses AsyncClient.get, NOT
    httpx.get. Patching the wrong one silently let these tests hit the real
    llama-server and pass for the wrong reason."""
    class _R:
        status_code = 200

        @staticmethod
        def json():
            return payload

    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            if raise_exc:
                raise raise_exc
            return _R()

    return _C


_CAPS_TRUE = {"chat_template_caps": {"supports_tools": True, "supports_tool_calls": True}}
_CAPS_FALSE = {"chat_template_caps": {"supports_tools": False}}
_BUSY = httpx.ReadTimeout("server busy generating")


def _age_out(root="http://localhost:8080", tag="\x00llamacpp"):
    """Make the cached entry look stale so the next call re-probes."""
    key = (root, tag)
    ts, val = L._OLLAMA_CAPS_CACHE[key]
    L._OLLAMA_CAPS_CACHE[key] = (ts - 10_000, val)
    return val


def test_timeout_keeps_the_last_known_answer(monkeypatch):
    """THE regression. Answer True once, then time out: must stay True."""
    monkeypatch.setattr(L.httpx, "AsyncClient", _fake_client(_CAPS_TRUE))
    assert asyncio.run(L.llamacpp_supports_tools("http://localhost:8080/v1")) is True

    _age_out()
    monkeypatch.setattr(L.httpx, "AsyncClient", _fake_client(raise_exc=_BUSY))
    assert asyncio.run(L.llamacpp_supports_tools("http://localhost:8080/v1")) is True, (
        "a transient probe timeout erased a known-good capability answer; the "
        "agent silently drops to fenced blocks and narrates tool calls instead "
        "of making them"
    )


def test_tool_mode_survives_a_probe_timeout(monkeypatch):
    """End to end: the decision the agent loop actually consumes."""
    monkeypatch.setattr(L.httpx, "AsyncClient", _fake_client(_CAPS_TRUE))

    async def _no_ollama(*a, **k):
        return None
    monkeypatch.setattr(L, "ollama_supports_tools", _no_ollama)

    url = "http://localhost:8080/v1"
    blob = r"C:\Users\x\.ollama\models\blobs\sha256-deadbeef"
    assert asyncio.run(L.local_native_mode(url, blob)) is True

    _age_out()
    monkeypatch.setattr(L.httpx, "AsyncClient", _fake_client(raise_exc=_BUSY))
    assert asyncio.run(L.local_native_mode(url, blob)) is True, (
        "tool mode collapsed to the curated name list after one slow probe; a "
        "blob-path model name matches nothing there, so it goes fenced"
    )


def test_no_prior_answer_still_reports_unknown(monkeypatch):
    """Stickiness must not invent an answer we never had."""
    monkeypatch.setattr(L.httpx, "AsyncClient", _fake_client(raise_exc=_BUSY))
    assert asyncio.run(L.llamacpp_supports_tools("http://localhost:8080/v1")) is None


def test_a_definitive_negative_is_also_sticky(monkeypatch):
    """False is an answer too — don't let a timeout flip it to unknown."""
    monkeypatch.setattr(L.httpx, "AsyncClient", _fake_client(_CAPS_FALSE))
    assert asyncio.run(L.llamacpp_supports_tools("http://localhost:8080/v1")) is False
    _age_out()
    monkeypatch.setattr(L.httpx, "AsyncClient", _fake_client(raise_exc=_BUSY))
    assert asyncio.run(L.llamacpp_supports_tools("http://localhost:8080/v1")) is False


def test_probe_timeout_is_generous_enough_for_a_busy_server():
    """3s was the value that failed in the field."""
    assert L._CAPS_PROBE_TIMEOUT >= 10, (
        "the capability probe competes with the agent's own generation for the "
        "server's attention; a tight timeout decides tool mode by luck"
    )
