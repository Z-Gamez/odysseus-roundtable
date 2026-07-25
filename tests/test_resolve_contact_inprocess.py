"""resolve_contact must not call Odysseus's own API over HTTP.

A server-side httpx request to /api/email/resolve-contact carries no session
cookie, so the auth dependency correctly rejected it (403) and the agent
silently saw zero email-history contacts. The fix is the in-process pattern
already used for the CardDAV lookup and manage_contact — share the search
function — NOT relaxing the route's auth. These tests pin both halves.
"""
import asyncio
import inspect
import re

import pytest

import src.tools.contacts as contacts_tool
from routes import email_helpers

# NOTE: patches below use the dotted-path form on purpose. Another suite
# (tests/test_security_regressions.py) evicts routes.email_helpers from
# sys.modules, which would leave a module-object patch pointing at a stale
# copy while do_resolve_contact re-imports a fresh one.


def _executable_source(fn) -> str:
    """Function source with comments and the docstring stripped, so assertions
    match real calls rather than prose that mentions httpx."""
    src = inspect.getsource(fn)
    src = re.sub(r'"""".*?"""', "", src, flags=re.S)
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def test_tool_makes_no_internal_http_call():
    code = _executable_source(contacts_tool.do_resolve_contact)
    for forbidden in ("httpx", "_INTERNAL_BASE", "AsyncClient", "client.get"):
        assert forbidden not in code, (
            f"resolve_contact reintroduced an internal HTTP call ({forbidden}); "
            "server-side calls carry no session cookie and get rejected"
        )
    assert "search_mail_contacts" in code


def test_shared_helper_exists_and_is_sync():
    # Sync because the IMAP client is blocking; the tool runs it in a thread.
    assert callable(email_helpers.search_mail_contacts)
    assert not inspect.iscoroutinefunction(email_helpers.search_mail_contacts)
    params = inspect.signature(email_helpers.search_mail_contacts).parameters
    assert "name" in params and "owner" in params


def test_tool_uses_email_history_results(monkeypatch):
    """The tool surfaces what the shared search returns, tagged as email history."""
    monkeypatch.setattr("routes.email_helpers.search_mail_contacts",
                        lambda name, owner="", limit=10: [
                            {"email": "Michaela@Example.com", "name": "Michaela"}])
    # No CardDAV configured in the test env — that branch degrades quietly.
    out = asyncio.run(contacts_tool.do_resolve_contact('{"name":"Michaela"}', owner="admin"))
    assert out["exit_code"] == 0
    assert "michaela@example.com" in out["output"].lower()   # normalized to lowercase
    assert "email history" in out["output"]


def test_tool_passes_owner_through(monkeypatch):
    seen = {}

    def fake_search(name, owner="", limit=10):
        seen["name"], seen["owner"] = name, owner
        return []

    monkeypatch.setattr("routes.email_helpers.search_mail_contacts", fake_search)
    asyncio.run(contacts_tool.do_resolve_contact('{"name":"Bob"}', owner="admin"))
    assert seen == {"name": "Bob", "owner": "admin"}


def test_search_failure_does_not_break_the_tool(monkeypatch):
    def boom(name, owner="", limit=10):
        raise RuntimeError("IMAP down")

    monkeypatch.setattr("routes.email_helpers.search_mail_contacts", boom)
    out = asyncio.run(contacts_tool.do_resolve_contact('{"name":"Nobody"}', owner="admin"))
    assert out["exit_code"] == 0
    assert "No contacts found" in out["output"]


def test_route_auth_is_unchanged():
    """The endpoint must still require auth — the fix was to stop calling it
    over HTTP, not to open it up."""
    src = inspect.getsource(__import__("routes.email_routes", fromlist=["x"]))
    idx = src.index('@router.get("/resolve-contact")')
    handler = src[idx:idx + 700]
    assert "Depends(require_owner)" in handler
