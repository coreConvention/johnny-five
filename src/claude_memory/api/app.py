"""Standalone HTTP app hosting the REST API + the read-only dashboard (A1).

This is a **separate service** from the MCP server (``server.py``). It is
deliberately *not* mounted into the MCP Starlette app — hosting option (a) of
the Tier A design (§A1/§2): the MCP stdio/sse transport stays completely
untouched, and this app runs in its own uvicorn process on its own port.

Run it via the ``johnny-five`` entrypoint::

    johnny-five --transport api --host 127.0.0.1 --port 8788

The dashboard exposes *mutating* endpoints (archive, importance edit), so the
CLI default binds to ``127.0.0.1`` — keep it host-local. In docker-compose the
container binds ``0.0.0.0`` but the host port is published only on
``127.0.0.1`` so the mutation surface never reaches the LAN.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from claude_memory.api import routes

# src/claude_memory/dashboard/  (packaged with the wheel; present in the image).
_DASHBOARD_DIR: Path = Path(__file__).resolve().parent.parent / "dashboard"
_INDEX_HTML: Path = _DASHBOARD_DIR / "index.html"


def create_app() -> FastAPI:
    """Build the dashboard/REST FastAPI app (the router + the static page).

    Returns a fresh app each call (no global state). The MCP server object in
    ``server.py`` is never imported or mounted here.
    """
    app = FastAPI(
        title="johnny-five memory dashboard",
        description="Read-only corpus inspection + hygiene (Tier A / A1).",
        version="0.1.0",
    )
    app.include_router(routes.router)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """Serve the self-contained dashboard page."""
        return FileResponse(_INDEX_HTML)

    # Optional static mount for any future assets; index is served explicitly
    # above so this mount never shadows the /api routes.
    if _DASHBOARD_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(_DASHBOARD_DIR)),
            name="static",
        )

    return app


def run_api(host: str = "127.0.0.1", port: int = 8788) -> None:
    """Serve :func:`create_app` via uvicorn (a standalone process).

    Imports uvicorn lazily so the MCP stdio/sse import path never pulls in the
    dashboard stack.
    """
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)
