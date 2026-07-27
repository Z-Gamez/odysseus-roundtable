"""Successful runs must teach the agent something.

The skill loop was half-wired: retrieval, relevance matching, confidence
gating and prompt injection all existed, but the only writer was
teacher_escalation — which needs `teacher_enabled` AND a configured
`teacher_model`, and only fires when the student FAILS. On an install with no
teacher configured (the common case) that conjunction never held, so
data/skills held nothing but imported bundles after months of use and every
successful multi-step run was thrown away.
"""
import asyncio
import json

import pytest

from src import skill_capture


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    """Neutral settings so a test asserts behaviour, not the user's config."""
    values = {"skill_capture_enabled": True,
              "skill_capture_min_tool_calls": 3,
              "skill_autosave_min_confidence": 0.85,
              "skill_capture_model": ""}
    monkeypatch.setattr(skill_capture, "_setting",
                        lambda k, d: values.get(k, d))
    return values


def _calls(n):
    return [{"tool": f"t{i}", "results": "ok"} for i in range(n)]


# ── the gate ─────────────────────────────────────────────────────────────────

def test_substantive_successful_run_is_captured():
    ok, why = skill_capture.should_capture(
        mode="agent", rounds=3, tool_results=_calls(4), agent_reply="Done.")
    assert ok, why


def test_trivial_run_is_not_captured():
    """A skill that restates one tool call costs prompt budget forever."""
    ok, why = skill_capture.should_capture(
        mode="agent", rounds=2, tool_results=_calls(1), agent_reply="Done.")
    assert not ok and "need 3" in why


def test_single_round_is_not_captured():
    ok, why = skill_capture.should_capture(
        mode="agent", rounds=1, tool_results=_calls(5), agent_reply="Done.")
    assert not ok and "single round" in why


def test_failed_run_is_not_captured():
    """THE point of gating on success: never persist a broken procedure."""
    ok, why = skill_capture.should_capture(
        mode="agent", rounds=3,
        tool_results=[{"tool": "bash", "error": "command not found"}] + _calls(3),
        agent_reply="Done.")
    assert not ok and "did not succeed" in why


def test_chat_mode_is_not_captured():
    ok, _ = skill_capture.should_capture(
        mode="chat", rounds=3, tool_results=_calls(5), agent_reply="Done.")
    assert not ok


def test_gate_always_explains_itself():
    """The bug being fixed was an empty skills dir with no explanation; a
    silent gate would recreate it one level down."""
    for kwargs in (dict(mode="chat", rounds=3, tool_results=_calls(5)),
                   dict(mode="agent", rounds=1, tool_results=_calls(5)),
                   dict(mode="agent", rounds=3, tool_results=_calls(0))):
        ok, why = skill_capture.should_capture(agent_reply="Done.", **kwargs)
        assert not ok and why and why != "eligible"


# ── distillation ─────────────────────────────────────────────────────────────

def _run_capture(monkeypatch, response, written, *, revise=None):
    async def fake_model(target, prompt, owner=None):
        written.setdefault("prompts", []).append(prompt)
        return response

    async def fake_write(content, owner=None):
        written["skill"] = json.loads(content)
        return {"results": "ok"}

    monkeypatch.setattr(skill_capture, "_call_model", fake_model)
    import src.tools.system as system
    monkeypatch.setattr(system, "do_manage_skills", fake_write)
    return asyncio.run(skill_capture._capture(
        user_request="ship the thing",
        tool_results=_calls(4),
        agent_reply="Done.",
        target={},
        revise_skill=revise,
        owner="admin",
    ))


_GOOD = """Here you go.

```json
{"action": "add", "name": "ship-the-thing", "description": "d",
 "when_to_use": "w", "procedure": ["a", "b"], "confidence": 0.9}
```"""


def test_confident_skill_is_written(monkeypatch):
    written = {}
    assert _run_capture(monkeypatch, _GOOD, written) == "ship-the-thing"
    assert written["skill"]["name"] == "ship-the-thing"


def test_low_confidence_is_discarded(monkeypatch):
    low = _GOOD.replace('"confidence": 0.9', '"confidence": 0.3')
    written = {}
    assert _run_capture(monkeypatch, low, written) is None
    assert "skill" not in written, "a skill below the injection threshold was persisted"


def test_capture_is_always_a_draft(monkeypatch):
    """A distillation has had no human review. status=published would put one
    bad capture into every future matching prompt."""
    written = {}
    _run_capture(monkeypatch, _GOOD, written)
    assert written["skill"]["status"] == "draft"


def test_model_can_decline(monkeypatch):
    written = {}
    assert _run_capture(monkeypatch, "NO_SKILL", written) is None
    assert "skill" not in written


def test_unparseable_response_writes_nothing(monkeypatch):
    written = {}
    assert _run_capture(monkeypatch, "sure, sounds good", written) is None
    assert "skill" not in written


# ── prompt-injection containment ─────────────────────────────────────────────

def test_trace_is_marked_untrusted(monkeypatch):
    """A captured trace is attacker-controllable execution output, and a
    payload distilled into a PERSISTED skill outlives the poisoned source and
    is re-injected as trusted guidance on every future match."""
    written = {}
    _run_capture(monkeypatch, _GOOD, written)
    prompt = written["prompts"][0]
    assert "<<<UNTRUSTED_TRACE>>>" in prompt
    assert "UNTRUSTED TRACE DATA" in prompt
    assert "not instructions" in prompt.lower()


# ── revision in place ────────────────────────────────────────────────────────

def test_reuse_revises_instead_of_duplicating(monkeypatch):
    """Otherwise the library grows a near-duplicate per run instead of one
    skill that gets better."""
    written = {}
    existing = {"name": "ship-the-thing", "procedure": ["a"]}
    _run_capture(monkeypatch, _GOOD, written, revise=existing)
    prompt = written["prompts"][0]
    assert "used an existing skill" in prompt
    assert "ship-the-thing" in prompt


def test_capture_never_raises_into_the_agent_loop(monkeypatch):
    """It runs at end-of-turn; an exception here must not surface as a failed
    response for work that already succeeded."""
    monkeypatch.setattr(skill_capture, "_setting",
                        lambda k, d: {"skill_capture_enabled": True,
                                      "skill_capture_min_tool_calls": 3}.get(k, d))

    def boom(*a, **k):
        raise RuntimeError("skills backend down")
    monkeypatch.setattr(skill_capture, "_skill_to_revise", boom)
    # Documented "safe to call unconditionally" — it must honour that itself
    # rather than relying on every call site remembering a try/except.
    assert skill_capture.maybe_capture(
        mode="agent", user_request="x", tool_results=_calls(4),
        agent_reply="Done.", rounds=3, owner="admin") is None
