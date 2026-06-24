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

        run_id = "rt_" + uuid.uuid4().hex[:12]
        gen = orchestrator.run_pipeline(
            run_id, title, description, acceptance, workspace, user or ""
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

    @router.get("/runs")
    async def list_runs(request: Request) -> Dict[str, Any]:
        return {"runs": store.list_runs(50)}

    @router.get("/{run_id}")
    async def get_run(request: Request, run_id: str) -> Any:
        run = store.get_run(run_id)
        if not run:
            return JSONResponse({"error": "Run not found."}, status_code=404)
        run["active"] = agent_runs.is_active(run_id)
        run["live_status"] = agent_runs.get_status(run_id)
        return run

    return router
