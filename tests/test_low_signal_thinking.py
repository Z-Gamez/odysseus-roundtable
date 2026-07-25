"""The direct low-signal reply path must not be starved by a reasoning block.

Bug: the low-signal path (greetings, chit-chat) capped output at 128 tokens.
A thinking model spends that entire budget inside its reasoning block and
returns EMPTY content with finish_reason="length", so the user saw the canned
"Hey." fallback instead of a real reply. Confirmed in the field via
chat_messages.metadata: output_tokens=128, direct_low_signal=true.

Three fixes are pinned here:
  1. a real budget (512) instead of 128,
  2. reasoning actually turned off on that path,
  3. a retry when the reply came back empty *because* reasoning ate the budget.
"""
import asyncio
import inspect
import re

import pytest

from src import llm_core


# ── fix 2: the suppression payload ────────────────────────────────────────────
# Every server takes a different field and ignores the ones it doesn't know,
# so sending the wrong one fails silently with a 200 and a full reasoning
# block. Probed against Ollama /v1 with qwen3:14b ("hey", max_tokens=256):
# think:false -> 362 reasoning chars, chat_template_kwargs -> 365,
# reasoning_effort:"none" -> 0. llama.cpp is the mirror image and gates
# reasoning through the chat template.

def _capture_payload(monkeypatch, **kwargs):
    """Run stream_llm against a stubbed transport and return the POSTed body."""
    seen = {}

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class _FakeStream:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, *a):
            return False

    class _FakeClient:
        def stream(self, method, url, **kw):
            return _FakeStream(url=url, **kw)

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda *a, **k: False)

    async def _run():
        async for _ in llm_core.stream_llm(
            "http://localhost:11434/v1/chat/completions", "qwen3:14b",
            [{"role": "user", "content": "hey"}], **kwargs
        ):
            pass

    asyncio.run(_run())
    return seen["json"]


def test_suppress_thinking_sends_every_dialect(monkeypatch):
    payload = _capture_payload(monkeypatch, suppress_thinking=True)
    # The one Ollama /v1 actually honors.
    assert payload["reasoning_effort"] == "none"
    # The one llama.cpp honors.
    assert payload["chat_template_kwargs"]["enable_thinking"] is False


def test_suppression_is_opt_in(monkeypatch):
    """A normal call must keep reasoning — it's only waste for chit-chat."""
    payload = _capture_payload(monkeypatch, suppress_thinking=False)
    assert "reasoning_effort" not in payload
    assert not (payload.get("chat_template_kwargs") or {}).get("enable_thinking") is False


def test_suppression_preserves_other_template_kwargs(monkeypatch):
    payload = _capture_payload(monkeypatch, suppress_thinking=True)
    assert payload["chat_template_kwargs"]["enable_thinking"] is False


def test_stream_llm_accepts_suppress_thinking():
    params = inspect.signature(llm_core.stream_llm).parameters
    assert "suppress_thinking" in params
    assert params["suppress_thinking"].default is False


def test_fallback_wrapper_forwards_kwargs():
    """stream_llm_with_fallback must pass suppress_thinking through, or the
    low-signal path silently loses the fix the moment a fallback is used."""
    src = inspect.getsource(llm_core.stream_llm_with_fallback)
    assert "**kwargs" in src


# ── fixes 1 + 3: the low-signal path in agent_loop ────────────────────────────
# Source-pinned: this path lives deep inside the streaming agent generator and
# reproducing it needs a live model. These assertions catch the exact
# regression (a 128 cap, or rendering the canned reply over an empty
# reasoning-starved response) without booting one.

def _low_signal_source() -> str:
    from src import agent_loop
    src = inspect.getsource(agent_loop)
    start = src.index("_direct_budget")
    return src[start:start + 4000]


def test_low_signal_budget_is_not_128():
    body = _low_signal_source()
    assert "min(max_tokens or 128, 128)" not in body, (
        "the 128-token low-signal cap is back; a thinking model spends it all "
        "on reasoning and returns empty content"
    )
    assert re.search(r"_direct_budget\s*=\s*min\(max_tokens or 512, 512\)", body)


def test_low_signal_call_suppresses_thinking():
    assert "suppress_thinking=True" in _low_signal_source()


def test_empty_reasoning_starved_reply_retries_before_falling_back():
    body = _low_signal_source()
    retry = body.index("_saw_thinking")
    fallback = body.index('fallback = "Hey."')
    assert retry < fallback, "retry must run before the canned reply"
    assert "if not direct_response.strip() and _saw_thinking:" in body
    assert "max_tokens=2048" in body


def test_thinking_deltas_are_not_counted_as_the_reply():
    """Reasoning must set the retry flag, never accumulate into the reply."""
    body = _low_signal_source()
    assert "_saw_thinking = True" in body
    idx = body.index("_saw_thinking = True")
    window = body[idx:idx + 200]
    assert "else:" in window and "direct_response +=" in window
