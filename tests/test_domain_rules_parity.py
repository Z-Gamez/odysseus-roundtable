"""A domain without a rule pack must not take the app down.

_domain_rules_for_tools ran _DOMAIN_RULES[domain]. Adding 'media' to the
classifier and _DOMAIN_TOOL_MAP without a matching rules entry raised KeyError
during prompt assembly — before any model call, so there was no fallback path.
Every message mentioning a TV returned 500.
"""
import pytest

import src.agent_loop as A


def test_every_mapped_domain_has_a_rules_pack():
    missing = [d for d in A._DOMAIN_TOOL_MAP if d not in A._DOMAIN_RULES]
    assert not missing, (
        f"domains in _DOMAIN_TOOL_MAP with no _DOMAIN_RULES entry: {missing}. "
        f"Add the rule text, or rely on the .get() fallback — but do not leave "
        f"it ambiguous."
    )


def test_media_domain_resolves():
    assert A._domain_rules_for_tools({"tv_control"})


def test_an_unmapped_domain_does_not_crash(monkeypatch):
    """THE regression. Prompt assembly happens before any model call, so a
    KeyError here is an unrecoverable 500 rather than a degraded answer."""
    monkeypatch.setitem(A._DOMAIN_TOOL_MAP, "__probe__", {"__probe_tool__"})
    try:
        A._domain_rules_for_tools({"__probe_tool__"})
    except KeyError as e:
        pytest.fail(f"a domain with no rules pack still raises: {e}")


def test_the_lookup_is_not_a_bare_subscript():
    import inspect
    src = inspect.getsource(A._domain_rules_for_tools)
    assert "_DOMAIN_RULES[domain]" not in src, (
        "bare subscript reintroduces the 500; use _DOMAIN_RULES.get(domain)"
    )
