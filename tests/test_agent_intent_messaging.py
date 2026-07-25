"""Intent routing for messaging / explicitly-named tools (#imsg-routing).

Regression for: "send a text to <number> using the tool called send_imessage
saying test" was classified low_signal with domains=[] and took the
tool-skipping direct reply path, so send_imessage never ran. Three guards:
an explicitly named tool forces the tool path, a new messaging domain fires
on text/imessage/sms/phone-number, and the _DOMAIN_RULES entry exists so tool
selection doesn't KeyError.
"""
from src.agent_loop import (
    _classify_agent_request,
    _named_registered_tools,
    _domain_rules_for_tools,
    _DOMAIN_TOOL_MAP,
    _DOMAIN_RULES,
)
from src.tool_policy import known_tool_names

_MSGS = [{"role": "user", "content": "prev"}]
_TOOLS = set(known_tool_names())


def test_exact_repro_forces_tool_path():
    q = "send a text to 5551234567 using the tool called send_imessage saying test"
    r = _classify_agent_request(_MSGS, q, _TOOLS)
    assert r["low_signal"] is False
    assert "send_imessage" in (r.get("force_tools") or set())


def test_named_tool_beats_casual_early_return():
    # A message that also looks low-signal but names a tool must still force it.
    r = _classify_agent_request(_MSGS, "yo send_imessage", _TOOLS)
    assert r["low_signal"] is False
    assert "send_imessage" in (r.get("force_tools") or set())


def test_messaging_domain_without_tool_name():
    for q in ("text 5551234567 saying running late",
              "iMessage Alex that I'm on my way",
              "send an sms to mom"):
        r = _classify_agent_request(_MSGS, q, _TOOLS)
        assert r["low_signal"] is False, q
        assert "messaging" in r["domains"], q


def test_bare_phone_number_triggers_messaging():
    r = _classify_agent_request(_MSGS, "shoot 5551234567 a quick note", _TOOLS)
    assert "messaging" in r["domains"]


def test_named_tools_word_boundary():
    names = {"ls", "send_imessage", "python"}
    # "ls" must not fire inside "false"; the underscore token matches whole only.
    assert _named_registered_tools("that is false", names) == set()
    assert _named_registered_tools("use send_imessage now", names) == {"send_imessage"}
    assert _named_registered_tools("run ls here", names) == {"ls"}


def test_casual_still_low_signal():
    assert _classify_agent_request(_MSGS, "hey", _TOOLS)["low_signal"] is True
    assert _classify_agent_request(_MSGS, "thanks!", _TOOLS)["low_signal"] is True


def test_messaging_domain_maps_to_tool_and_has_rules():
    assert "send_imessage" in _DOMAIN_TOOL_MAP["messaging"]
    assert "messaging" in _DOMAIN_RULES  # else _domain_rules_for_tools KeyErrors
    # The rule pack is derivable from a selected send_imessage without raising.
    assert _domain_rules_for_tools({"send_imessage"})


def test_no_registry_still_classifies_domain():
    # Without a tool list (older callers) the messaging domain still fires.
    r = _classify_agent_request(_MSGS, "text 5551234567 hi", None)
    assert "messaging" in r["domains"]
