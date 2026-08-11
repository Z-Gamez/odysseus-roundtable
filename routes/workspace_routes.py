"""Workspace API - browse server directories to pick a tool workspace folder."""
import os
from fastapi import APIRouter, Request, HTTPException, Query

from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user

# Cap entries returned per directory (mirrors filesystem_tools._CODENAV_MAX_HITS).
# A huge directory shouldn't dump thousands of rows into the picker; the user can
# type/paste a path to jump straight in instead.
_MAX_BROWSE_DIRS = 500


def setup_workspace_routes():
    router = APIRouter(prefix="/api/workspace", tags=["workspace"])

    @router.get("/browse")
    def browse(request: Request, path: str = Query(default="")):
        """List subdirectories of `path` (default: home) so the UI can navigate
        the server filesystem and pick a workspace folder. Directories only.

        ADMIN-ONLY: this enumerates the server filesystem, so it is gated the
        same way the file/shell tools are (read_file/write_file/bash are in
        NON_ADMIN_BLOCKED_TOOLS). A non-admin who can't use those tools must not
        be able to map the host's directory tree either.
        """
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace browsing is admin-only")

        # Resolve symlinks so the reported path is canonical and the UI navigates
        # real directories (defends against symlink games in displayed paths).
        target = os.path.realpath(os.path.expanduser(path.strip() or "~"))
        if not os.path.isdir(target):
            target = os.path.realpath(os.path.expanduser("~"))

        dirs = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    try:
                        # Don't follow symlinks when classifying - a symlinked
                        # dir is skipped rather than letting the browser wander
                        # off via a link. Hidden entries are omitted.
                        if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                            # Build the child path server-side with os.path.join
                            # so it's correct on Windows (backslashes) and Linux.
                            dirs.append({"name": entry.name, "path": os.path.join(target, entry.name)})
                    except OSError:
                        continue
        except (PermissionError, OSError):
            dirs = []

        dirs_sorted = sorted(dirs, key=lambda d: d["name"].lower())
        truncated = len(dirs_sorted) > _MAX_BROWSE_DIRS
        parent = os.path.dirname(target)
        from src.tool_execution import vet_workspace
        return {
            "path": target,
            "parent": parent if parent and parent != target else None,
            "dirs": dirs_sorted[:_MAX_BROWSE_DIRS],
            "truncated": truncated,
            # Whether this directory may be bound as a workspace (filesystem
            # roots and sensitive dirs may be browsed through but not chosen).
            "selectable": vet_workspace(target) is not None,
        }

    @router.get("/vet")
    def vet(request: Request, path: str = Query(default="")):
        """Validate a workspace path without binding it.

        The UI calls this before persisting a manually typed path (/workspace
        set) so a typo, file path, deleted folder, sensitive dir, or filesystem
        root is rejected up front with the canonical path returned on success,
        instead of being stored client-side and silently dropped at chat time.
        Admin-gated like /browse: it confirms path existence on the host.
        """
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace selection is admin-only")
        from src.tool_execution import vet_workspace
        resolved = vet_workspace(path)
        return {"ok": resolved is not None, "path": resolved}

    # ── run a browser-runnable project the agent just built ────────────────
    #
    # One STABLE url per workspace — /api/workspace/preview/ serves its
    # index.html — so the assistant can hand the user a link without composing
    # a path, and the same link keeps working as the project is edited.
    _PREVIEW_INDEXES = ("index.html", "index.htm")
    # Never serve these out of a workspace even though the agent may write them.
    _PREVIEW_DENY = {".env", ".env.local", ".git-credentials", "id_rsa", "id_ed25519"}

    def _preview_root() -> str:
        from src.tool_execution import get_active_workspace
        ws = get_active_workspace()
        if not ws or not os.path.isdir(ws):
            raise HTTPException(status_code=404,
                                detail="No workspace is set, so there is nothing to preview.")
        return os.path.realpath(ws)

    @router.get("/preview")
    @router.get("/preview/{subpath:path}")
    def preview(request: Request, subpath: str = ""):
        """Serve a file from the active workspace so a built project can run.

        Admin-gated exactly like /browse and /vet: this reads files off the
        host, and a user who cannot use read_file must not read them here
        either.
        """
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace preview is admin-only")

        root = _preview_root()
        # Confinement: resolve first, THEN check containment, so symlinks and
        # ../ both collapse before the comparison rather than after it.
        target = os.path.realpath(os.path.join(root, subpath))
        if target != root and not target.startswith(root + os.sep):
            raise HTTPException(status_code=403, detail="Path escapes the workspace")

        if os.path.isdir(target):
            for name in _PREVIEW_INDEXES:
                cand = os.path.join(target, name)
                if os.path.isfile(cand):
                    target = cand
                    break
            else:
                raise HTTPException(
                    status_code=404,
                    detail="No index.html here — this project may not be one a browser can run.")

        base = os.path.basename(target)
        if base in _PREVIEW_DENY or base.startswith(".env"):
            raise HTTPException(status_code=403, detail="That file is not served")
        if not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="Not found")

        import mimetypes
        from fastapi.responses import FileResponse
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        return FileResponse(
            target,
            media_type=ctype,
            headers={
                # The project is model-written code served from Odysseus's OWN
                # origin, where it would otherwise inherit the session cookie
                # and could call the API as the user. `sandbox` gives the
                # response an opaque origin — scripts still run, so a game
                # works, but it can read no cookies and reach no authenticated
                # endpoint. allow-same-origin is deliberately NOT granted; the
                # cost is that localStorage is unavailable to the page.
                "Content-Security-Policy": "sandbox allow-scripts allow-modals allow-pointer-lock",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    return router
