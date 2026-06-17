"""Pure-ASGI auth middleware for the MCP sub-app.

Every request to /mcp/* must carry X-API-Key. This middleware validates the
key, touches last_used_at, resolves the acting User, and stores both in
ContextVars so MCP tool functions can read them without FastAPI dependency
injection (which doesn't apply inside FastMCP handlers).

Why pure ASGI instead of BaseHTTPMiddleware: BaseHTTPMiddleware buffers the
response body before returning it, which breaks SSE streams. Pure ASGI passes
the send callable straight through, so long-lived SSE connections work correctly.

ContextVar propagation to sync tools: asyncio copies the current context when
running a coroutine or spawning an executor task (anyio.to_thread.run_sync
included). FastMCP runs sync @mcp.tool() functions via anyio, so the ContextVars
set here are readable from inside every tool function.
"""
from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from datetime import UTC, datetime

from app.models import APIKey, User

# ---------------------------------------------------------------------------
# ContextVars — set by middleware, read by tool functions
# ---------------------------------------------------------------------------

_current_key: ContextVar[APIKey] = ContextVar("mcp_current_key")
_current_user: ContextVar[User] = ContextVar("mcp_current_user")


def get_current_key() -> APIKey:
    """Return the validated APIKey for the current MCP request."""
    try:
        return _current_key.get()
    except LookupError:
        raise RuntimeError("MCP auth context not set — is MCPAuthMiddleware installed?")


def get_current_user() -> User:
    """Return the acting User for the current MCP request."""
    try:
        return _current_user.get()
    except LookupError:
        raise RuntimeError("MCP auth context not set — is MCPAuthMiddleware installed?")


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


async def _reject(send, status: int, message: str) -> None:
    body = json.dumps({"error": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class MCPAuthMiddleware:
    """Validates X-API-Key on every HTTP request before passing to the MCP app."""

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_key = headers.get(b"x-api-key", b"").decode("utf-8", errors="replace").strip()

        if not raw_key:
            await _reject(send, 401, "X-API-Key header required")
            return

        from sqlalchemy import select

        from app.db.session import SessionLocal
        from app.models import APIKey, User

        session = SessionLocal()
        try:
            digest = hashlib.sha256(raw_key.encode()).hexdigest()
            key: APIKey | None = session.execute(
                select(APIKey).where(APIKey.key_hash == digest)
            ).scalar_one_or_none()

            if key is None:
                await _reject(send, 401, "invalid API key")
                return
            if key.revoked_at is not None:
                await _reject(send, 401, "API key has been revoked")
                return

            key.last_used_at = datetime.now(tz=UTC)
            session.commit()
            session.refresh(key)

            # Resolve the acting user (mirrors the logic in deps.current_user).
            user: User | None = None
            if key.created_by is not None:
                user = session.get(User, key.created_by)
            if user is None:
                system_email = "system@deployment.local"
                user = session.execute(
                    select(User).where(User.email == system_email)
                ).scalar_one_or_none()
                if user is None:
                    user = User(email=system_email, display_name="system")
                    session.add(user)
                    session.commit()
                    session.refresh(user)

            tok_key = _current_key.set(key)
            tok_user = _current_user.set(user)
            try:
                await self._app(scope, receive, send)
            finally:
                _current_key.reset(tok_key)
                _current_user.reset(tok_user)

        except Exception:
            await _reject(send, 500, "authentication error")
        finally:
            session.close()
