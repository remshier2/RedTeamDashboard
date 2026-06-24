"""Standalone MCP server entrypoint.

Boots only the FastMCP SSE app wrapped in ``MCPAuthMiddleware`` and
serves it via uvicorn on port 8000 under the ``/mcp`` path. Used as the
command override for the secondary "rtd-mcp" Container App in Stage 2:
when a lease has ``requires_container=True`` and the deployment has
``aca_mcp_app_enabled`` on, the worker connects here over SSE instead of
the colocated MCP mounted under the backend at ``/mcp``.

The image and env are identical to the backend — same KV-resolved
secrets (``DATABASE_URL``, ``PROVIDER_KEY_MASTER``, etc.) so the server
validates lease tokens and resolves engagement state against the shared
Postgres exactly like the colocated server does. Only the entrypoint
differs (this module instead of ``app.main``).

Path layout matches the colocated server so the worker envelope's
``mcp_url`` is interchangeable — ``<base>/mcp`` always.
"""
from __future__ import annotations

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

from app.core.config import settings
from app.core.logging import configure_logging
from app.mcp.auth import MCPAuthMiddleware
from app.mcp.server import mcp


def build_app() -> Starlette:
    """ASGI app served by this entrypoint.

    ``mcp.sse_app()`` is wrapped in the auth middleware (X-API-Key +
    optional X-Lease-Token); the Starlette Mount under ``/mcp`` mirrors
    the FastAPI ``app.mount("/mcp", ...)`` used by the colocated path
    so both deployments expose the same URL surface.
    """
    return Starlette(routes=[Mount("/mcp", app=MCPAuthMiddleware(mcp.sse_app()))])


def main() -> None:
    configure_logging(settings.env)
    uvicorn.run(build_app(), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
