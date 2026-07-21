"""REST endpoints for the macOS Messages approval card (static/js/chat.js).

The agent's send_imessage tool stages a message (src/agent_tools/mac_messages.py)
and the chat renders an Approve/Decline card. These endpoints back the card:
list the staged message, approve (runs the AppleScript that actually sends),
poll status, or decline. Approval — never the model — triggers delivery.
"""
import logging

from fastapi import APIRouter, Request

from routes.email_helpers import require_owner
from src.agent_tools import mac_messages

logger = logging.getLogger(__name__)


def setup_messages_routes() -> APIRouter:
    router = APIRouter(prefix="/api/messages", tags=["messages"])

    @router.get("/pending")
    async def list_pending(request: Request):
        owner = require_owner(request)
        return {"pending": mac_messages.list_pending(owner)}

    @router.post("/pending/{pid}/approve")
    async def approve(pid: str, request: Request):
        owner = require_owner(request)
        row = mac_messages.get(pid)
        if not row or (owner and row.get("owner") != owner):
            return {"success": False, "error": "Message not found or already handled"}
        return mac_messages.approve(pid)

    @router.get("/pending/{pid}/status")
    async def status(pid: str, request: Request):
        owner = require_owner(request)
        row = mac_messages.get(pid)
        if not row or (owner and row.get("owner") != owner):
            return {"status": "unknown"}
        return mac_messages.status(pid)

    @router.delete("/pending/{pid}")
    async def decline(pid: str, request: Request):
        owner = require_owner(request)
        row = mac_messages.get(pid)
        if not row or (owner and row.get("owner") != owner):
            return {"success": False, "error": "Message not found"}
        return mac_messages.decline(pid)

    return router
