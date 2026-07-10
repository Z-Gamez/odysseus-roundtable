"""Ponytail integration — vendored lazy-senior-dev rules behind toggles.

Ruleset from github.com/DietrichGebert/ponytail (MIT), vendored at
config/ponytail.md. Chat: injected into stream_llm's consolidated system
message when ponytail_mode is an active level. Round Table: appended to the
Developer role's system prompt only, when saw_ponytail is on.
"""
import asyncio

import pytest

from src import llm_core


@pytest.fixture(autouse=True)
def _fresh_rules_cache():
    if hasattr(llm_core._ponytail_rules_text, "_cache"):
        del llm_core._ponytail_rules_text._cache
    yield
    if hasattr(llm_core._ponytail_rules_text, "_cache"):
        del llm_core._ponytail_rules_text._cache


def _patch_setting(monkeypatch, overrides):
    import src.settings as settings
    monkeypatch.setattr(
        settings, "get_setting",
        lambda key, default=None: overrides.get(key, default),
    )


def test_vendored_ruleset_loads():
    text = llm_core._ponytail_rules_text()
    assert "YAGNI" in text and "lazy senior developer" in text


def test_system_text_levels():
    assert llm_core._ponytail_system_text("off") == ""
    assert llm_core._ponytail_system_text("") == ""
    assert llm_core._ponytail_system_text("bogus") == ""
    full = llm_core._ponytail_system_text("full")
    assert "YAGNI" in full and "Ponytail level" not in full
    lite = llm_core._ponytail_system_text("lite")
    assert "LITE" in lite and "YAGNI" in lite
    ultra = llm_core._ponytail_system_text("ultra")
    assert "ULTRA" in ultra and "YAGNI" in ultra


def _stream_system_content(monkeypatch, ponytail_mode):
    """Run stream_llm far enough to capture the system message it builds."""
    _patch_setting(monkeypatch, {"ponytail_mode": ponytail_mode})
    seen = {}

    def spy_build(model, messages, *a, **kw):
        seen["messages"] = messages
        return {"model": model, "messages": messages, "stream": True}

    monkeypatch.setattr(llm_core, "_build_ollama_payload", spy_build)
    monkeypatch.setattr(llm_core, "get_context_length", lambda url, model: 0)
    # Short-circuit before HTTP: dead host yields an error chunk and returns,
    # but the payload (and our spy) runs first — same trick as the num_ctx test.
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: True)

    async def collect():
        return [c async for c in llm_core.stream_llm(
            "https://ollama.com/api", "m",
            [{"role": "system", "content": "base prompt"},
             {"role": "user", "content": "hi"}],
        )]

    asyncio.run(collect())
    msgs = seen["messages"]
    return msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""


def test_stream_llm_injects_rules_when_on(monkeypatch):
    sys_content = _stream_system_content(monkeypatch, "full")
    assert sys_content.startswith("base prompt")   # stable prefix kept first
    assert "YAGNI" in sys_content


def test_stream_llm_clean_when_off(monkeypatch):
    sys_content = _stream_system_content(monkeypatch, "off")
    assert sys_content == "base prompt"


def test_dev_role_gets_rules_only_when_enabled(monkeypatch):
    from src.saw.orchestrator import _dev_messages
    from src.saw.roles import RoleSpec
    role = RoleSpec(key="developer", title="Developer",
                    system_prompt="You are the developer.", allowed_tools=set())

    _patch_setting(monkeypatch, {"saw_ponytail": True})
    msgs = _dev_messages(role, "t", "spec", None, "C:/ws")
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].startswith("You are the developer.")
    assert "YAGNI" in msgs[0]["content"]

    _patch_setting(monkeypatch, {"saw_ponytail": False})
    msgs = _dev_messages(role, "t", "spec", None, "C:/ws")
    assert msgs[0]["content"] == "You are the developer."
