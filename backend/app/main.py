from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.api_keys import router as api_keys_router
from app.api.approvals import router as approvals_router
from app.api.authorizations import router as authorizations_router
from app.api.deps import AsyncRedisClient, DbSession
from app.api.engagements import router as engagements_router
from app.api.events import router as events_router
from app.api.orchestrator import router as orchestrator_router
from app.api.reports import router as reports_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.mcp.auth import MCPAuthMiddleware
from app.mcp.server import mcp

configure_logging(settings.env)

app = FastAPI(title="Red Team Dashboard API", version="0.0.1")

# CORS for the browser viewer. Defaults cover local dev; Phase 6 central
# viewer adds its origin via the CORS_ALLOW_ORIGINS env var (Bicep param).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Last-Event-ID"],
)

app.include_router(engagements_router)
app.include_router(approvals_router)
app.include_router(authorizations_router)
app.include_router(api_keys_router)
app.include_router(events_router)
app.include_router(orchestrator_router)
app.include_router(reports_router)

# MCP server — auth-gated SSE endpoint for agent clients (Claude Code, etc.)
# Agents connect via: claude mcp add rtd --transport sse --url https://<fqdn>/mcp/sse
app.mount("/mcp", MCPAuthMiddleware(mcp.sse_app()))


@app.get("/health")
async def health(session: DbSession, redis: AsyncRedisClient) -> JSONResponse:
    """Liveness + dependency readiness probe.

    Returns 200 only if Postgres + Redis both respond. Compose's healthcheck
    polls this, so an unhealthy DB or Redis correctly bubbles up as the
    backend container going unhealthy rather than just "uvicorn is listening".
    """
    db_ok = True
    redis_ok = True
    try:
        session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 — any failure means not ready
        db_ok = False
    try:
        await redis.ping()
    except Exception:  # noqa: BLE001
        redis_ok = False

    healthy = db_ok and redis_ok
    return JSONResponse(
        status_code=(
            status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        content={
            "status": "ok" if healthy else "degraded",
            "env": settings.env,
            "db": db_ok,
            "redis": redis_ok,
        },
    )
