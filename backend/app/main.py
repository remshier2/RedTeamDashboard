"""
RedTeamDashboard — Defensive Security Operations and Governance Platform

This FastAPI application provides the HTTP and MCP (Model Context Protocol) surface
for managing authorized security engagements.

**Charter:**
- Approval-gated execution: Every active tool call passes a scope + risk gate and
  is recorded as an Approval with an immutable audit_log.
- Agents assist, analysts decide: Automated agents perform enumeration and scanning
  only. Validation/proof-of-concept work is analyst-only.
- In-scope enforcement: Recon/OSINT tooling runs only against targets explicitly
  defined by the analyst as in-scope.

The MCP server exposes tools for Claude Code and other AI assistants, with the same
approval gates and audit logging as the web UI.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
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
from app.api.provider_keys import router as provider_keys_router
from app.api.reports import router as reports_router
from app.api.workflow_templates import router as workflow_templates_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.mcp.auth import MCPAuthMiddleware
from app.mcp.server import mcp

configure_logging(settings.env)
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks for the API process.

    On startup we idempotently seed the ``is_system=True`` workflow
    templates (Phase 10) so the starter packs (Network Recon, OSINT
    Enum, Web App) exist after any deploy. The seed function is a
    no-op when the named rows already exist, so it's safe on every
    boot. Failures are caught + logged but never block startup —
    we'd rather serve traffic without templates than refuse to come up.
    """
    try:
        from app.services.workflow_templates import seed_system_templates

        session = SessionLocal()
        try:
            inserted = seed_system_templates(session)
            session.commit()
            if inserted:
                log.info("startup.workflow_templates_seeded", inserted=inserted)
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — never block startup on seed
        log.exception("startup.workflow_templates_seed_failed")
    yield


app = FastAPI(title="Red Team Dashboard API", version="0.0.1", lifespan=lifespan)

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
app.include_router(provider_keys_router)
app.include_router(reports_router)
app.include_router(workflow_templates_router)

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
