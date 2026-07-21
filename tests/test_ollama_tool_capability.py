"""Native-vs-fenced tool mode for Ollama models.

Native function calling used to be opt-in (per-endpoint supports_tools plus a
hardcoded model list), so general models like qwen3.x / llama3.2 fell back to
the fenced-block format, malformed it, and never emitted a valid tool call.
Ollama already knows: /api/show reports a "tools" capability. These tests pin
the probe, its caching, and the fallback ordering.
"""
import asyncio

import httpx
import pytest

from src import llm_core


@pytest.fixture(autouse=True)
def _clear_caps_cache():
    llm_core._OLLAMA_CAPS_CACHE.clear()
    yield
    llm_core._OLLAMA_CAPS_CACHE.clear()


def _mock_show(monkeypatch, payload, status=200, counter=None):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            if counter is not None:
                counter["n"] = counter.get("n", 0) + 1
                counter["url"] = url
                counter["model"] = (json or {}).get("model")
            return httpx.Response(status, json=payload,
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_core.httpx, "AsyncClient", _Client)


def test_native_root_handles_v1_compat_base():
    # /v1 is the OpenAI-compat base; /api/show must be derived from the host.
    assert llm_core._ollama_native_root("http://localhost:11434/v1") == "http://localhost:11434/api"
    assert llm_core._ollama_native_root("http://localhost:11434/api/chat") == "http://localhost:11434/api"
    assert llm_core._ollama_native_root("http://localhost:11434") == "http://localhost:11434/api"


def test_tools_capability_true(monkeypatch):
    seen = {}
    _mock_show(monkeypatch, {"capabilities": ["completion", "tools", "thinking"]}, counter=seen)
    r = asyncio.run(llm_core.ollama_supports_tools("http://localhost:11434/v1", "qwen3.6:27b"))
    assert r is True
    assert seen["url"] == "http://localhost:11434/api/show"
    assert seen["model"] == "qwen3.6:27b"


def test_tools_capability_false_when_absent(monkeypatch):
    _mock_show(monkeypatch, {"capabilities": ["completion"]})
    assert asyncio.run(llm_core.ollama_supports_tools("http://x:11434/v1", "tiny")) is False


def test_unknown_when_no_capabilities_field(monkeypatch):
    # Old Ollama: 200 but no capabilities -> unknown, so the caller falls back.
    _mock_show(monkeypatch, {"model_info": {}})
    assert asyncio.run(llm_core.ollama_supports_tools("http://x:11434/v1", "old")) is None


def test_unknown_on_http_error(monkeypatch):
    _mock_show(monkeypatch, {"error": "model not found"}, status=404)
    assert asyncio.run(llm_core.ollama_supports_tools("http://x:11434/v1", "nope")) is None


def test_result_is_cached(monkeypatch):
    seen = {}
    _mock_show(monkeypatch, {"capabilities": ["tools"]}, counter=seen)

    async def twice():
        a = await llm_core.ollama_supports_tools("http://x:11434/v1", "m")
        b = await llm_core.ollama_supports_tools("http://x:11434/v1", "m")
        return a, b

    assert asyncio.run(twice()) == (True, True)
    assert seen["n"] == 1, "capability probe should hit the server once per model"


def test_native_mode_prefers_server_over_curated_list(monkeypatch):
    # Server says tools -> native, even though the model isn't in the list.
    _mock_show(monkeypatch, {"capabilities": ["tools"]})
    assert asyncio.run(llm_core.ollama_native_mode("http://x:11434/v1", "llama3.2:3b")) is True
    # Server says no tools -> fenced, even for a curated name.
    llm_core._OLLAMA_CAPS_CACHE.clear()
    _mock_show(monkeypatch, {"capabilities": ["completion"]})
    assert asyncio.run(llm_core.ollama_native_mode("http://x:11434/v1", "ornith:9b")) is False


def test_native_mode_falls_back_when_server_silent(monkeypatch):
    _mock_show(monkeypatch, {}, status=500)
    # Built-in curated name still wins the fallback.
    assert asyncio.run(llm_core.ollama_native_mode("http://x:11434/v1", "ornith:9b-12k")) is True
    llm_core._OLLAMA_CAPS_CACHE.clear()
    assert asyncio.run(llm_core.ollama_native_mode("http://x:11434/v1", "mystery:1b")) is False


def test_configured_list_extends_the_fallback(monkeypatch):
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: ["qwen3.6:27b", "llama3.2:3b"]
                        if k == "ollama_native_tools_model" else d)
    assert llm_core.ollama_native_tools_model("qwen3.6:27b") is True
    assert llm_core.ollama_native_tools_model("llama3.2:3b-instruct") is True
    assert llm_core.ollama_native_tools_model("something-else") is False
    # Built-in default still applies alongside the configured list.
    assert llm_core.ollama_native_tools_model("ornith:9b") is True
