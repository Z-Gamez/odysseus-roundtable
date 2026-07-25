"""open_in_safari — open a URL in the user's REAL browser.

Safari's WebDriver session (the `browser` tool's safari backend) is isolated
by Apple's design: no user cookies/logins. So "open safari and go to X" for
the user must NOT go through that; it shells `open -a Safari <url>`, which uses
the real Safari profile and needs no Automation permission. These tests cover
URL normalization/safety, the macOS invocation, the non-mac fallback, and the
intent routing that surfaces the tool.
"""
import asyncio

import pytest

from src.agent_tools import open_url as ou
from src.agent_tools.open_url import OpenUrlTool


@pytest.mark.parametrize("raw,expected", [
    ("youtube.com", "https://youtube.com"),
    ("http://youtube.com", "http://youtube.com"),
    ("https://x.com/path?q=1", "https://x.com/path?q=1"),
    ("  github.com  ", "https://github.com"),
])
def test_normalize_adds_scheme(raw, expected):
    assert ou._normalize_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ",
    "file:///etc/passwd",          # non-web scheme must be rejected
    "javascript:alert(1)",
    "tel:+15551234567",
    "https://",                     # no host
])
def test_normalize_rejects_unsafe(raw):
    assert ou._normalize_url(raw) is None


def _run(tool, content, ctx=None):
    return asyncio.run(tool.execute(content, ctx or {}))


def test_macos_opens_real_safari(monkeypatch):
    seen = {}

    class _Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(ou.sys, "platform", "darwin")
    monkeypatch.setattr(ou.subprocess, "run", fake_run)
    out = _run(OpenUrlTool(), '{"url":"youtube.com"}')
    assert out["exit_code"] == 0
    assert "Safari" in out["output"]
    # Real profile is used because we launch the actual Safari app; url is argv.
    assert seen["cmd"] == ["open", "-a", "Safari", "https://youtube.com"]


def test_bare_string_argument_is_treated_as_url(monkeypatch):
    seen = {}

    class _Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(ou.sys, "platform", "darwin")
    monkeypatch.setattr(ou.subprocess, "run", fake_run)
    out = _run(OpenUrlTool(), "youtube.com")
    assert out["exit_code"] == 0
    assert seen["cmd"][-1] == "https://youtube.com"


def test_rejects_bad_url(monkeypatch):
    monkeypatch.setattr(ou.sys, "platform", "darwin")
    out = _run(OpenUrlTool(), '{"url":"file:///etc/passwd"}')
    assert out["exit_code"] == 1 and "valid http" in out["error"]


def test_open_failure_surfaces_stderr(monkeypatch):
    class _Proc:
        returncode = 1
        stderr = "kLSNoExecutableErr"

    monkeypatch.setattr(ou.sys, "platform", "darwin")
    monkeypatch.setattr(ou.subprocess, "run", lambda cmd, **k: _Proc())
    out = _run(OpenUrlTool(), '{"url":"youtube.com"}')
    assert out["exit_code"] == 1 and "kLSNoExecutableErr" in out["error"]


def test_non_mac_uses_default_browser(monkeypatch):
    opened = {}
    monkeypatch.setattr(ou.sys, "platform", "win32")
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.setdefault("url", u))
    out = _run(OpenUrlTool(), '{"url":"youtube.com"}')
    assert out["exit_code"] == 0
    assert opened["url"] == "https://youtube.com"


# --- intent routing (regression: these used to hit ui/low-signal) -----------

def test_intent_routes_open_phrasings_to_open_url():
    from src.agent_loop import _classify_agent_request
    from src.tool_policy import known_tool_names
    tn = set(known_tool_names())
    msgs = [{"role": "user", "content": "x"}]
    for q in ("open safari and go to http://youtube.com",
              "navigate to http://youtube.com",
              "open youtube.com",
              "pull up github.com",
              "open safari"):
        r = _classify_agent_request(msgs, q, tn)
        assert r["low_signal"] is False, q
        assert "open_url" in r["domains"], q


def test_open_url_domain_maps_to_tool_with_rules():
    from src.agent_loop import _DOMAIN_TOOL_MAP, _DOMAIN_RULES, _domain_rules_for_tools
    assert "open_in_safari" in _DOMAIN_TOOL_MAP["open_url"]
    assert "open_url" in _DOMAIN_RULES  # else _domain_rules_for_tools KeyErrors
    assert _domain_rules_for_tools({"open_in_safari"})


def test_open_in_safari_schema_exposed():
    import src.tool_schemas as ts
    names = [s["function"]["name"] for s in ts.FUNCTION_TOOL_SCHEMAS if "function" in s]
    assert "open_in_safari" in names
