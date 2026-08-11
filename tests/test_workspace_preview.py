"""The workspace preview route: run a browser project the agent just built.

Serving model-written files over HTTP from the app's own origin is the risky
part, so the confinement and the sandbox header are what these tests pin.
"""
import os

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes.workspace_routes import setup_workspace_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    (ws / "sub").mkdir(parents=True)
    (ws / "index.html").write_text("<h1>game</h1>", encoding="utf-8")
    (ws / "game.js").write_text("console.log(1)", encoding="utf-8")
    (ws / ".env").write_text("SECRET=hunter2", encoding="utf-8")
    (ws / "sub" / "index.html").write_text("<h1>sub</h1>", encoding="utf-8")
    # A file OUTSIDE the workspace, the thing traversal would reach for.
    (tmp_path / "outside.txt").write_text("do not serve me", encoding="utf-8")

    import src.tool_execution as te
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(ws))
    import src.tool_security as ts
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda o: True)
    import routes.workspace_routes as wr
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda o: True)
    monkeypatch.setattr(wr, "get_current_user", lambda r: "admin")

    app = FastAPI()
    app.include_router(setup_workspace_routes())
    return TestClient(app), ws, tmp_path


def test_root_serves_index_html(client):
    c, _, _ = client
    r = c.get("/api/workspace/preview/")
    assert r.status_code == 200
    assert "game" in r.text


def test_bare_preview_path_also_works(client):
    """The link handed to the user must not depend on a trailing slash."""
    c, _, _ = client
    assert c.get("/api/workspace/preview").status_code == 200


def test_asset_is_served_with_its_own_type(client):
    c, _, _ = client
    r = c.get("/api/workspace/preview/game.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_response_is_sandboxed(client):
    """Model-written HTML runs on Odysseus's origin. Without an opaque origin it
    would inherit the session cookie and could call the API as the user."""
    c, _, _ = client
    csp = c.get("/api/workspace/preview/").headers["content-security-policy"]
    assert "sandbox" in csp
    assert "allow-scripts" in csp        # the game still needs to run
    assert "allow-same-origin" not in csp  # but never with the app's origin
    assert c.get("/api/workspace/preview/").headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("attack", [
    "../outside.txt",
    "..%2Foutside.txt",
    "sub/../../outside.txt",
    "....//outside.txt",
])
def test_traversal_cannot_escape_the_workspace(client, attack):
    c, _, _ = client
    r = c.get(f"/api/workspace/preview/{attack}")
    assert r.status_code in (403, 404), f"{attack} was served: {r.status_code}"
    assert "do not serve me" not in r.text


def test_dotenv_is_never_served(client):
    c, _, _ = client
    r = c.get("/api/workspace/preview/.env")
    assert r.status_code == 403
    assert "hunter2" not in r.text


def test_subdirectory_index_is_served(client):
    c, _, _ = client
    r = c.get("/api/workspace/preview/sub/")
    assert r.status_code == 200
    assert "sub" in r.text


def test_directory_without_index_is_a_clear_404(tmp_path, monkeypatch):
    ws = tmp_path / "noweb"
    ws.mkdir()
    (ws / "main.py").write_text("print(1)", encoding="utf-8")
    import src.tool_execution as te
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(ws))
    import routes.workspace_routes as wr
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda o: True)
    monkeypatch.setattr(wr, "get_current_user", lambda r: "admin")
    app = FastAPI()
    app.include_router(setup_workspace_routes())
    r = TestClient(app).get("/api/workspace/preview/")
    assert r.status_code == 404
    assert "index.html" in r.json()["detail"]


def test_no_workspace_is_a_clear_404(monkeypatch):
    import src.tool_execution as te
    monkeypatch.setattr(te, "get_active_workspace", lambda: None)
    import routes.workspace_routes as wr
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda o: True)
    monkeypatch.setattr(wr, "get_current_user", lambda r: "admin")
    app = FastAPI()
    app.include_router(setup_workspace_routes())
    r = TestClient(app).get("/api/workspace/preview/")
    assert r.status_code == 404
    assert "workspace" in r.json()["detail"].lower()


def test_non_admin_is_refused(tmp_path, monkeypatch):
    """Gated like /browse and /vet — reading host files must not become easier
    through the preview than through read_file."""
    ws = tmp_path / "p"
    ws.mkdir()
    (ws / "index.html").write_text("x", encoding="utf-8")
    import src.tool_execution as te
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(ws))
    import routes.workspace_routes as wr
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda o: False)
    monkeypatch.setattr(wr, "get_current_user", lambda r: "bob")
    app = FastAPI()
    app.include_router(setup_workspace_routes())
    assert TestClient(app).get("/api/workspace/preview/").status_code == 403
