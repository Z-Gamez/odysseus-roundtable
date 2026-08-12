"""Every domain must be reachable by the classifier and have a rule pack.

This is the assertion that would have caught five shipped bugs, all one root
cause: a tool is wired into a domain, the classifier can never produce that
domain, and the tool is therefore never offered. The failure is silent and
looks exactly like the model refusing to do something it cannot do.

  * send_imessage  — no messaging domain existed
  * browser/open_url — "open ..." matched the ui domain instead
  * tv_control     — no media domain, so TV requests classified as ui
  * generate_image — no images domain; "generate me an image of a cat" scored
                     low_signal with NO domains, so the agent was handed only
                     memory/ask_user/update_plan and replied it had no such tool
  * media          — the mirror image: a domain in the tool map with no entry
                     in _DOMAIN_RULES, raising KeyError on every TV message

Adding a domain without a probe phrase is itself a failure here, which is the
point: the probe is what proves reachability.
"""
import pytest

from src.agent_loop import (
    _DOMAIN_PROBES,
    _DOMAIN_RULES,
    _DOMAIN_TOOL_MAP,
    _classify_agent_request,
    verify_domain_wiring,
)


def _domains_for(text):
    got = _classify_agent_request([{"role": "user", "content": text}], text)
    return set(got.get("domains") or set())


def test_wiring_invariant_holds():
    """The single check. If this fails, some tool is invisible to the agent."""
    verify_domain_wiring()


@pytest.mark.parametrize("domain", sorted(_DOMAIN_TOOL_MAP))
def test_every_domain_has_rules(domain):
    """A domain without rules raised KeyError on every message that matched it."""
    assert domain in _DOMAIN_RULES


@pytest.mark.parametrize("domain", sorted(_DOMAIN_TOOL_MAP))
def test_every_domain_has_a_probe(domain):
    assert domain in _DOMAIN_PROBES, (
        f"{domain} has no probe phrase, so nothing proves the classifier can "
        f"ever produce it — that is how a tool ships invisible")


@pytest.mark.parametrize("domain,phrase", sorted(_DOMAIN_PROBES.items()))
def test_every_domain_is_reachable(domain, phrase):
    if domain not in _DOMAIN_TOOL_MAP:
        pytest.skip(f"{domain} has no tools mapped")
    assert domain in _domains_for(phrase), (
        f"no classifier pattern produces {domain!r}, so "
        f"{sorted(_DOMAIN_TOOL_MAP[domain])} can never be offered")


def test_no_rules_for_a_domain_nothing_maps_to():
    assert not (set(_DOMAIN_RULES) - set(_DOMAIN_TOOL_MAP))


# ── the reported bug ───────────────────────────────────────────────────────


@pytest.mark.parametrize("phrase", [
    "generate me an image of a cat",
    "draw a picture of a dog",
    "make me a wallpaper of mountains",
    "create a logo for my company",
    "generate an illustration of a robot",
    "can you paint something abstract",
])
def test_image_requests_reach_the_image_tools(phrase):
    assert "images" in _domains_for(phrase), f"{phrase!r} did not classify as images"


def test_image_request_is_not_low_signal():
    """It scored low_signal=True with no domains, which is why the agent got
    only memory/ask_user/update_plan and said it had no such tool."""
    got = _classify_agent_request(
        [{"role": "user", "content": "generate me an image of a cat"}],
        "generate me an image of a cat")
    assert got.get("low_signal") is False
    assert "images" in (got.get("domains") or set())


def test_generate_image_is_actually_in_the_images_domain():
    assert "generate_image" in _DOMAIN_TOOL_MAP["images"]


# ── plural nouns the patterns used to miss ─────────────────────────────────
#
# Found by the invariant above, not by hand: the sessions and settings
# patterns matched only the singular, so "list my sessions" reached no domain
# and "change my settings" fell through to ui.


@pytest.mark.parametrize("phrase,domain", [
    ("list my sessions", "sessions"),
    ("delete these sessions", "sessions"),
    ("change my settings", "settings"),
    ("open my preferences", "settings"),
    ("show my api tokens", "settings"),
])
def test_plural_phrasings_reach_their_domain(phrase, domain):
    assert domain in _domains_for(phrase)


def test_unrelated_talk_does_not_drag_in_image_tools():
    """The image nouns are common; they must not hijack ordinary conversation."""
    for phrase in ("send an email to bob", "what models are running",
                   "read the file config.json"):
        assert "images" not in _domains_for(phrase), phrase
