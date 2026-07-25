"""HTTP-level tests for the /api/messages approval endpoints.

Regression: these routes originally used require_owner, whose signature
declares `account_id: str | None = Query(None)`. They take `pid` as a PATH
param and no account_id, so the Query OBJECT (truthy) reached the
account-ownership SQL check and raised sqlite3.ProgrammingError -> 503 on
every call. The approval card then loaded nothing and Approve reported a
generic failure. require_user is the auth-only dependency for path-param
routes; these tests drive the real routes to prove they return 200 with data.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.email_helpers as email_helpers
import routes.messages_routes as messages_routes
from src.agent_tools import mac_messages


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Isolate the pending store and authenticate as a named user — the exact
    # shape that tripped the Query collision (named owner + path param).
    monkeypatch.setattr(mac_messages, "_STORE", str(tmp_path / "pending_messages.json"))
    monkeypatch.setattr(email_helpers, "_require_auth", lambda request: "admin")
    app = FastAPI()
    app.include_router(messages_routes.setup_messages_routes())
    return TestClient(app)


def test_list_pending_returns_rows_not_503(client):
    mac_messages.stage("+15551234567", "hello there", "sms", "admin")
    r = client.get("/api/messages/pending")
    assert r.status_code == 200, r.text
    rows = r.json()["pending"]
    assert len(rows) == 1
    # The exact keys the approval card reads.
    assert rows[0]["to"] == "+15551234567"
    assert rows[0]["body"] == "hello there"
    assert rows[0]["service"] == "sms"


def test_approve_attempts_send_and_returns_200(client, monkeypatch):
    calls = {}

    def fake_send(recipient, body, service=""):
        calls["args"] = (recipient, body)
        return None  # delivered

    monkeypatch.setattr(mac_messages, "deliver", fake_send)
    pid = mac_messages.stage("+1555", "hi", "imessage", "admin")
    r = client.post(f"/api/messages/pending/{pid}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert calls["args"] == ("+1555", "hi")      # actually attempted the send
    assert mac_messages.status(pid)["status"] == "sent"


def test_approve_surfaces_real_error_text(client, monkeypatch):
    monkeypatch.setattr(mac_messages, "deliver",
                        lambda r, b, s="": "Automation permission missing for Messages.")
    pid = mac_messages.stage("+1555", "hi", "imessage", "admin")
    r = client.post(f"/api/messages/pending/{pid}/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is False
    assert "Automation permission" in body["error"]   # not a generic "send failed"


def test_status_and_decline_reachable(client):
    pid = mac_messages.stage("+1555", "hi", "imessage", "admin")
    assert client.get(f"/api/messages/pending/{pid}/status").status_code == 200
    d = client.delete(f"/api/messages/pending/{pid}")
    assert d.status_code == 200 and d.json()["success"] is True
    assert mac_messages.get(pid)["status"] == "declined"


def test_routes_never_call_require_owner():
    """Guard the regression directly: no /api/messages handler may CALL
    require_owner, whose Query default collides with these path-param routes.
    (The name may still appear in the explanatory comment.)"""
    import inspect
    src = inspect.getsource(messages_routes)
    assert "require_owner(" not in src, "path-param route calling require_owner"
    assert "require_user(request)" in src
