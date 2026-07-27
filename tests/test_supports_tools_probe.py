"""supports_tools must be measured, not left null and guessed at.

Null is not a harmless "unknown". At request time an endpoint the localhost
heuristic doesn't recognise (LAN box, Tailscale host, remapped container port)
skips the capability probe entirely and falls through to a curated model-name
keyword list; a tool-capable model whose name isn't on that list is silently
downgraded to fenced-block mode. The agent then narrates tool use instead of
performing it — a Round Table Developer turn once emitted 128 fenced blocks in
767s and executed nothing.
"""
import asyncio

import pytest

from routes import model_routes
from src import agent_loop, llm_core


# ── the shared probe ─────────────────────────────────────────────────────────

def test_probe_returns_none_when_nobody_answers(monkeypatch):
    """None must mean "no answer", never a guess. The curated-list fallback in
    local_native_mode is exactly what this helper must NOT do."""
    async def dead(*a, **k):
        return None
    monkeypatch.setattr(llm_core, "ollama_supports_tools", dead)
    monkeypatch.setattr(llm_core, "llamacpp_supports_tools", dead)
    # "ornith" is on the curated list — if that leaked in, this would be True.
    assert asyncio.run(llm_core.probe_supports_tools("http://box:8080/v1", "ornith")) is None


def test_probe_falls_through_ollama_to_llamacpp(monkeypatch):
    async def no_ollama(*a, **k):
        return None

    async def yes_llamacpp(*a, **k):
        return True

    monkeypatch.setattr(llm_core, "ollama_supports_tools", no_ollama)
    monkeypatch.setattr(llm_core, "llamacpp_supports_tools", yes_llamacpp)
    assert asyncio.run(llm_core.probe_supports_tools("http://box:8080/v1", "m")) is True


def test_probe_honours_a_negative_answer(monkeypatch):
    """A server saying "no" must be preserved, not retried into a yes."""
    async def no(*a, **k):
        return False

    async def boom(*a, **k):
        raise AssertionError("llama.cpp must not be consulted after a definite answer")

    monkeypatch.setattr(llm_core, "ollama_supports_tools", no)
    monkeypatch.setattr(llm_core, "llamacpp_supports_tools", boom)
    assert asyncio.run(llm_core.probe_supports_tools("http://h/v1", "m")) is False


# ── creation-time defaulting ─────────────────────────────────────────────────

def _stub_probe(monkeypatch, mapping):
    async def fake(url, model=""):
        return mapping.get(model)
    monkeypatch.setattr(llm_core, "probe_supports_tools", fake)


def test_single_model_server_persists_the_answer(monkeypatch):
    """llama-server serves one model and /props is per-server, so a model-less
    probe is the whole answer."""
    _stub_probe(monkeypatch, {"": True})
    assert model_routes._probe_endpoint_tool_support("http://h:8080/v1", []) is True


def test_unreachable_endpoint_stays_null(monkeypatch):
    _stub_probe(monkeypatch, {})
    assert model_routes._probe_endpoint_tool_support("http://dead/v1", ["a"]) is None


def test_unanimous_models_persist(monkeypatch):
    _stub_probe(monkeypatch, {"a": True, "b": True})
    assert model_routes._probe_endpoint_tool_support("http://h/v1", ["a", "b"]) is True


def test_disagreeing_models_stay_null(monkeypatch):
    """THE regression this guards. supports_tools=True outranks the
    _model_no_tools blocklist in agent_loop, so baking one model's "yes" onto
    an endpoint that also serves deepseek-r1/gpt-oss would force native mode
    for those too. Unanimity, not first-answer-wins."""
    _stub_probe(monkeypatch, {"qwen3": True, "deepseek-r1": False})
    assert model_routes._probe_endpoint_tool_support(
        "http://h/v1", ["qwen3", "deepseek-r1"]) is None


def test_unknown_models_do_not_dilute_a_definite_answer(monkeypatch):
    """A model the server has never heard of answers None; that must not veto
    the models it did answer for."""
    _stub_probe(monkeypatch, {"qwen3": True})
    assert model_routes._probe_endpoint_tool_support(
        "http://h/v1", ["qwen3", "not-installed"]) is True


def test_probe_is_bounded(monkeypatch):
    """A 200-model endpoint must not fire 200 HTTP probes on create."""
    seen = []

    async def fake(url, model=""):
        seen.append(model)
        return True

    monkeypatch.setattr(llm_core, "probe_supports_tools", fake)
    model_routes._probe_endpoint_tool_support("http://h/v1", [f"m{i}" for i in range(200)])
    assert len(seen) <= model_routes._PROBE_MODEL_SAMPLE


def test_probe_failure_never_breaks_endpoint_creation(monkeypatch):
    async def boom(url, model=""):
        raise RuntimeError("network exploded")
    monkeypatch.setattr(llm_core, "probe_supports_tools", boom)
    assert model_routes._probe_endpoint_tool_support("http://h/v1", ["a"]) is None


def test_explicit_user_choice_is_not_overwritten():
    """The probe only fills a BLANK field; an admin's explicit true/false wins."""
    import inspect
    # create_model_endpoint is nested inside the route-registration function,
    # so it isn't a module attribute — read the module source.
    src = inspect.getsource(model_routes)
    idx = src.index("_st_raw = (supports_tools or \"\")")
    window = src[idx:idx + 400]
    assert "if _st is None:" in window
    assert "_probe_endpoint_tool_support" in window


# ── the fenced-downgrade guard ───────────────────────────────────────────────

def test_downgrade_warning_is_deduped_per_endpoint_model():
    assert isinstance(agent_loop._FENCED_DOWNGRADE_WARNED, set)


def test_downgrade_guard_conditions():
    """Fenced mode with zero schemas is legitimate; the warning must fire only
    on the combination that indicates a silent downgrade, or it becomes noise
    that gets filtered out and defeats the purpose."""
    import inspect
    src = inspect.getsource(agent_loop)
    assert "if _relevant_tools and not _tool_names_sent and not _is_api_model:" in src
    idx = src.index("_FENCED_DOWNGRADE_WARNED.add")
    assert "logger.warning" in src[idx:idx + 400]


# ── refresh-time resolution ──────────────────────────────────────────────────
#
# Creation is not the only chance to answer the question. An endpoint added
# while its llama-server was down, or before the model was pulled, gets a NULL
# that nothing else ever fills -- and NULL is precisely what routes the agent
# into fenced blocks. The manual refresh re-probes the model list anyway, so it
# is the natural place to settle it.

def _refresh_window():
    """Source of the manual-refresh branch in list_endpoint_models.

    The route is nested inside the registration function, so it is not a
    module attribute — same reason test_explicit_user_choice_is_not_overwritten
    reads source rather than calling it.
    """
    import inspect
    src = inspect.getsource(model_routes)
    # Anchor on the refresh branch's own log line, then read forward over the
    # whole branch — robust to edits above it.
    idx = src.index('logger.warning("Manual model refresh failed')
    return src[idx:idx + 1800]


def test_refresh_resolves_tool_support():
    window = _refresh_window()
    assert "_probe_endpoint_tool_support" in window, (
        "a manual model refresh re-probes the model list but never revisits "
        "supports_tools, so an endpoint created while its server was down "
        "keeps the NULL that sends the agent to fenced blocks"
    )


def test_refresh_only_fills_a_blank():
    """True/False on the row is a deliberate answer and must survive a refresh.

    supports_tools=True outranks the _model_no_tools blocklist in agent_loop,
    and False is the force-fenced escape hatch for models that break on native
    schemas. Overwriting either from a probe would silently undo the operator's
    choice on a routine refresh.
    """
    window = _refresh_window()
    idx = window.index("_probe_endpoint_tool_support")
    guard = window[:idx]
    assert "if ep.supports_tools is None:" in guard, (
        "the refresh probe is not gated on the field being unset"
    )


def test_refresh_ignores_an_inconclusive_probe():
    """None means nobody answered; writing it back would be a no-op at best and
    must not clobber the column."""
    window = _refresh_window()
    idx = window.index("_probe_endpoint_tool_support")
    after = window[idx:idx + 400]
    assert "is not None" in after, (
        "an inconclusive probe result must not be persisted"
    )
