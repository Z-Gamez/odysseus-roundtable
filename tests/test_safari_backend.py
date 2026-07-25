"""Safari WebDriver backend — request construction + adapter behavior.

safaridriver only exists on macOS, so these tests drive the _WD client and
_SafariPage adapter against a mock httpx transport, pinning the W3C request
shapes (session create, execute wrapping, element find/click, key actions).
The live Safari integration is verified on the Mac.
"""
import asyncio
import json

import httpx
import pytest

from src.agent_tools import safari_backend as sb


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _wd(handler, sid="S1"):
    return sb._WD("http://127.0.0.1:7000", sid, _client(handler))


def test_execute_wraps_arrow_function():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"value": [{"ref": "e1", "tag": "a", "text": "Home"}]})

    wd = _wd(handler)
    out = asyncio.run(wd.execute("() => 1 + 1"))
    assert seen["url"].endswith("/session/S1/execute/sync")
    # The arrow function is invoked, not passed as a bare expression.
    assert seen["body"]["script"] == "return (() => 1 + 1).apply(null, arguments);"
    assert seen["body"]["args"] == []
    assert out[0]["ref"] == "e1"


def test_find_returns_element_id():
    def handler(req):
        return httpx.Response(200, json={"value": {sb._WEB_ELEMENT: "node-42"}})
    wd = _wd(handler)
    assert asyncio.run(wd.find('[data-odys-ref="e5"]')) == "node-42"


def test_find_missing_returns_none():
    def handler(req):
        return httpx.Response(404, json={"value": {"error": "no such element",
                                                   "message": "not found"}})
    wd = _wd(handler)
    assert asyncio.run(wd.find("#nope")) is None


def test_send_keys_uses_text_field():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"value": None})

    wd = _wd(handler)
    asyncio.run(wd.send_keys("node-1", "hello"))
    assert seen["path"] == "/session/S1/element/node-1/value"
    assert seen["body"] == {"text": "hello"}


def test_key_action_uses_enter_code():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"value": None})

    wd = _wd(handler)
    asyncio.run(wd.key(sb._WD_KEYS["enter"]))
    action = seen["body"]["actions"][0]
    assert action["type"] == "key"
    assert action["actions"][0]["value"] == ""  # W3C Enter


def test_error_value_raises():
    def handler(req):
        return httpx.Response(200, json={"value": {"error": "javascript error",
                                                   "message": "boom"}})
    wd = _wd(handler)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(wd.execute("() => x.y"))


def test_page_goto_refreshes_url():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "POST" and req.url.path.endswith("/url"):
            return httpx.Response(200, json={"value": None})
        if req.method == "GET" and req.url.path.endswith("/url"):
            return httpx.Response(200, json={"value": "https://example.com/"})
        return httpx.Response(200, json={"value": None})

    page = sb._SafariPage(_wd(handler))
    asyncio.run(page.goto("https://example.com"))
    assert page.url == "https://example.com/"


def test_key_map_has_valid_w3c_codes():
    # Every mapped key must be a single Unicode PUA char (U+E000..U+F8FF) or a space.
    for name, code in sb._WD_KEYS.items():
        assert len(code) == 1
        assert code == " " or 0xE000 <= ord(code) <= 0xF8FF, f"{name}={code!r}"
