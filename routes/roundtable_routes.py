"""Round Table API — start / stream / inspect SAW orchestration runs.

Thin HTTP surface over src.saw.orchestrator. Streaming reuses Odysseus's detached
agent-run pub-sub (src.agent_runs): start() drains the orchestrator generator
server-side; subscribe() replays + streams it live (survives tab close/refresh).
"""
import os
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.auth_helpers import get_current_user
from src import agent_runs
from src.saw import orchestrator, store
from src.saw.roles import PIPELINE

# Phase-1 default workspace (the Developer role writes here). Overridable per
# request from the UI, or globally via SAW_WORKSPACE.
DEFAULT_WORKSPACE = os.environ.get("SAW_WORKSPACE", r"C:\Odysseus\saw-sandbox")


def _available_models(owner):
    """Enabled endpoints + their (non-hidden) chat models, for per-role dropdowns."""
    out = []
    try:
        from core.database import SessionLocal, ModelEndpoint
        from src.endpoint_resolver import _endpoint_enabled_models, normalize_base
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
            if owner:
                from src.auth_helpers import owner_filter
                q = owner_filter(q, ModelEndpoint, owner)
            for ep in q.all():
                models = _endpoint_enabled_models(ep)
                if not models:
                    continue
                label = (getattr(ep, "name", None) or getattr(ep, "label", None)
                         or normalize_base(getattr(ep, "base_url", "") or ""))
                out.append({"endpoint_id": ep.id, "label": label, "models": models})
        finally:
            db.close()
    except Exception:
        pass
    return out


def setup_roundtable_routes():
    router = APIRouter(prefix="/api/roundtable", tags=["roundtable"])

    @router.post("/start")
    async def start_run(request: Request) -> Any:
        user = get_current_user(request)  # username, or None when auth disabled
        try:
            body = await request.json()
        except Exception:
            body = {}
        title = (body.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "A ticket title is required."}, status_code=400)
        description = (body.get("description") or "").strip()
        acceptance = (body.get("acceptance") or "").strip()
        workspace = (body.get("workspace") or "").strip() or DEFAULT_WORKSPACE
        parent_run_id = (body.get("parent_run_id") or "").strip()

        run_id = "rt_" + uuid.uuid4().hex[:12]
        gen = orchestrator.run_pipeline(
            run_id, title, description, acceptance, workspace, user or "", parent_run_id
        )
        # Detached: runs to completion server-side even if nobody is subscribed.
        agent_runs.start(run_id, gen)
        return {"run_id": run_id, "workspace": workspace,
                "pipeline": [r.key for r in PIPELINE]}

    @router.get("/{run_id}/stream")
    async def stream_run(request: Request, run_id: str) -> StreamingResponse:
        return StreamingResponse(
            agent_runs.subscribe(run_id), media_type="text/event-stream"
        )

    @router.post("/{run_id}/stop")
    async def stop_run(request: Request, run_id: str) -> Dict[str, Any]:
        return {"stopped": agent_runs.stop(run_id)}

    @router.post("/{run_id}/merge")
    async def merge_run_ep(request: Request, run_id: str) -> Any:
        run = store.get_run(run_id)
        if not run:
            return JSONResponse({"error": "Run not found."}, status_code=404)
        from src.saw.orchestrator import merge_run
        return merge_run(run.get("workspace") or "", run_id)

    @router.get("/runs")
    async def list_runs(request: Request) -> Dict[str, Any]:
        return {"runs": store.list_runs(50)}

    # ---- per-role model config (local vs API, per role) ----
    @router.get("/models")
    async def list_models(request: Request) -> Dict[str, Any]:
        return {"endpoints": _available_models(get_current_user(request))}

    @router.get("/config")
    async def get_config(request: Request) -> Dict[str, Any]:
        from src.settings import get_setting
        cfg = get_setting("saw_role_models", {}) or {}
        roles = []
        for r in PIPELINE:
            rc = cfg.get(r.key) or {}
            roles.append({"key": r.key, "title": r.title, "purpose": r.endpoint_purpose,
                          "endpoint_id": rc.get("endpoint_id", ""), "model": rc.get("model", "")})
        return {"roles": roles, "endpoints": _available_models(get_current_user(request)),
                "rte_mode": get_setting("saw_rte_mode", "dry_run"),
                "max_iterations": get_setting("saw_max_iterations", 3)}

    @router.post("/config")
    async def set_config(request: Request) -> Any:
        try:
            body = await request.json()
        except Exception:
            body = {}
        from src.settings import load_settings, save_settings
        s = load_settings()
        if "rte_mode" in body:   # release mode toggle (dry_run | github)
            mode = "github" if str(body.get("rte_mode")).lower() == "github" else "dry_run"
            s["saw_rte_mode"] = mode
            save_settings(s)
            return {"ok": True, "rte_mode": mode}
        if "max_iterations" in body:   # Dev<->QAS retry budget (1..10)
            try:
                n = max(1, min(int(body.get("max_iterations")), 10))
            except (TypeError, ValueError):
                return JSONResponse({"error": "max_iterations must be a number 1-10"}, status_code=400)
            s["saw_max_iterations"] = n
            save_settings(s)
            return {"ok": True, "max_iterations": n}
        key = (body.get("role") or "").strip()
        if key not in {r.key for r in PIPELINE}:
            return JSONResponse({"error": "unknown role"}, status_code=400)
        cfg = dict(s.get("saw_role_models") or {})
        ep_id = (body.get("endpoint_id") or "").strip()
        if ep_id:
            cfg[key] = {"endpoint_id": ep_id, "model": (body.get("model") or "").strip()}
        else:
            cfg.pop(key, None)  # cleared -> back to the role's tier default
        s["saw_role_models"] = cfg
        save_settings(s)
        return {"ok": True, "role": key, "config": cfg.get(key)}

    @router.get("/{run_id}")
    async def get_run(request: Request, run_id: str) -> Any:
        run = store.get_run(run_id)
        if not run:
            return JSONResponse({"error": "Run not found."}, status_code=404)
        run["active"] = agent_runs.is_active(run_id)
        run["live_status"] = agent_runs.get_status(run_id)
        return run

    return router
