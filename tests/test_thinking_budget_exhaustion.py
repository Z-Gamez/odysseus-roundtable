"""A thinking model that never emits content must not surface as a failure.

Real failure, llama.cpp serving a reasoning model with `-c 8192`:

    finish_reason: length
    content len:            0
    reasoning_content len:  942   ("Thinking Process: 1. Analyze the Request...")

The user asked it to send a text and got back "The model returned an empty
response." Two independent defects combined:

1. Fast Mode only appended the `/no_think` text switch. That is a Qwen-on-Ollama
   prompt convention; llama.cpp gates reasoning through the chat template and
   ignores it, so Fast Mode did nothing at all on a llama-server model.

2. The empty-response guard was handed `round_reasoning`, which resets at the
   top of every round, but the guard itself runs once after the loop. Reasoning
   produced in any round but the last was discarded, so the guard saw "" and
   emitted the error instead of falling back to the reasoning it had.
"""
import asyncio
import inspect

import pytest

from src import agent_loop, llm_core


# ── fix 1: Fast Mode must reach the API, not just the prompt ─────────────────

def _payload_for(monkeypatch, *, fast_mode: bool):
    """Capture the JSON body stream_llm POSTs, with fast_mode on or off."""
    seen = {}

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class _Stream:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Client:
        def stream(self, method, url, **kw):
            return _Stream(url=url, **kw)

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _Client())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda *a, **k: False)

    real_get = llm_core_get_setting = None
    from src import settings as _settings
    monkeypatch.setattr(
        _settings, "get_setting",
        lambda key, default=None: fast_mode if key == "fast_mode" else default,
    )

    async def _run():
        async for _ in llm_core.stream_llm(
            "http://localhost:8080/v1/chat/completions", "some-gguf",
            [{"role": "user", "content": "send a text to Michaela"}],
        ):
            pass

    asyncio.run(_run())
    return seen["json"]


def test_fast_mode_sets_the_real_api_fields(monkeypatch):
    """The text switch alone is invisible to llama.cpp."""
    payload = _payload_for(monkeypatch, fast_mode=True)
    assert payload.get("reasoning_effort") == "none"
    assert payload.get("chat_template_kwargs", {}).get("enable_thinking") is False


def test_fast_mode_still_sends_the_text_switch(monkeypatch):
    """Ollama-served Qwen relies on /no_think; don't regress it."""
    payload = _payload_for(monkeypatch, fast_mode=True)
    last_user = [m for m in payload["messages"] if m["role"] == "user"][-1]
    assert last_user["content"].endswith("/no_think")


def test_fast_mode_off_leaves_reasoning_alone(monkeypatch):
    """Thinking is on by default — it genuinely helps multi-step work."""
    payload = _payload_for(monkeypatch, fast_mode=False)
    assert "reasoning_effort" not in payload
    last_user = [m for m in payload["messages"] if m["role"] == "user"][-1]
    assert "/no_think" not in last_user["content"]


# ── fix 2: the empty-response guard must see the whole turn ──────────────────

def test_guard_prefers_reasoning_over_the_error_message():
    out, chunk = agent_loop._empty_response_fallback("", "I was thinking...", [])
    assert out == "I was thinking..."
    assert chunk is None, "reasoning already streamed as thinking chunks; don't re-emit"


def test_guard_only_errors_when_there_is_truly_nothing():
    out, chunk = agent_loop._empty_response_fallback("", "", [])
    assert "empty response" in out
    assert chunk is not None


def test_guard_is_fed_turn_scoped_reasoning_not_round_scoped():
    """THE regression. round_reasoning resets each round; the guard runs once
    after the loop. Feeding it the round-scoped variable loses reasoning from
    every round but the last."""
    src = inspect.getsource(agent_loop)
    call = src[src.index("full_response, _fallback_chunk = _empty_response_fallback"):][:200]
    assert "turn_reasoning" in call, (
        "the guard is reading round-scoped reasoning again; reasoning from "
        "earlier rounds will be dropped and the user gets the error message"
    )
    assert "round_reasoning" not in call


def test_turn_reasoning_accumulates_alongside_round_reasoning():
    src = inspect.getsource(agent_loop)
    idx = src.index("round_reasoning += data[\"delta\"]")
    assert "turn_reasoning += data[\"delta\"]" in src[idx:idx + 160]


def test_turn_reasoning_is_not_reset_per_round():
    """It must be initialised once, outside the round loop."""
    src = inspect.getsource(agent_loop)
    assert src.count("turn_reasoning = \"\"") == 1, (
        "turn_reasoning is assigned more than once — if that happens inside "
        "the round loop it is round-scoped again and the bug is back"
    )
