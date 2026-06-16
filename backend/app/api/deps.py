"""Cross-cutting FastAPI dependencies.

- ``db_session``        — yields a SQLAlchemy Session, closed at request end.
- ``redis_client``      — yields a decode-strings Redis client.
- ``api_key_auth``      — resolves ``X-API-Key`` to an ``APIKey`` row, updates
                          ``last_used_at``, refuses if missing/revoked/unknown.
                          Production auth surface.
- ``RequireScope``      — factory dep that wraps ``api_key_auth`` and additionally
                          gates by privilege tier (``admin > cli > viewer``).
- ``current_user``      — back-compat shim. Tries X-API-Key first (returning the
                          minting user); falls back to the dev-time X-User-Id
                          header so existing endpoints + tests keep working
                          without a rewrite. Replace with ``RequireScope`` per
                          endpoint as the migration to API keys proceeds.
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from typing import Annotated

import redis as redis_lib
import redis.asyncio as aioredis
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import APIKey, APIKeyScope, User, scope_satisfies


def db_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def redis_client() -> Iterator[redis_lib.Redis]:
    client = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield client
    finally:
        client.close()


async def async_redis_client() -> AsyncIterator[aioredis.Redis]:
    """Async Redis client for long-lived endpoints (SSE).

    Sync calls would block the event loop for the duration of an XREAD; the
    async client cooperates with the loop so the worker process can serve
    many concurrent SSE subscribers.
    """
    client = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# API key auth
# ---------------------------------------------------------------------------


def hash_api_key(raw: str) -> str:
    """SHA-256 hex digest of the raw token. Deterministic so we can index on it
    and look up in O(1); safe because the input is 32+ bytes of random, not a
    guessable password."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _lookup_api_key(session: Session, raw: str) -> APIKey:
    if not raw:
        raise HTTPException(status_code=401, detail="X-API-Key header is empty")
    digest = hash_api_key(raw)
    key = session.execute(
        select(APIKey).where(APIKey.key_hash == digest)
    ).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    if key.revoked_at is not None:
        raise HTTPException(status_code=401, detail="API key has been revoked")
    # Touch last_used_at — not exact (we don't lock) but good enough to spot
    # stale keys. Done in its own UPDATE so concurrent requests don't fight.
    key.last_used_at = datetime.now(tz=UTC)
    session.commit()
    session.refresh(key)
    return key


def api_key_auth(
    session: Annotated[Session, Depends(db_session)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> APIKey:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header required")
    return _lookup_api_key(session, x_api_key)


def RequireScope(required: APIKeyScope) -> Callable[..., APIKey]:  # noqa: N802 — factory naming
    """Factory dep that requires an API key with at least ``required`` scope.

    Usage::

        @router.post("/api-keys", dependencies=[Depends(RequireScope(APIKeyScope.admin))])
        def mint(...): ...
    """

    def _checker(key: Annotated[APIKey, Depends(api_key_auth)]) -> APIKey:
        if not scope_satisfies(key.scope, required):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"this endpoint requires scope '{required.value}'; "
                    f"key has '{key.scope.value}'"
                ),
            )
        return key

    return _checker


# ---------------------------------------------------------------------------
# Back-compat user resolution
# ---------------------------------------------------------------------------


def _parse_user_identifier(raw: str) -> tuple[uuid.UUID | None, str | None]:
    raw = raw.strip()
    if not raw:
        raise HTTPException(status_code=401, detail="X-User-Id header is empty")
    try:
        return uuid.UUID(raw), None
    except ValueError:
        pass
    if "@" in raw:
        return None, raw
    raise HTTPException(
        status_code=400,
        detail="X-User-Id must be a UUID or an email address",
    )


def upsert_user(session: Session, header_value: str) -> User:
    user_uuid, email = _parse_user_identifier(header_value)

    if user_uuid is not None:
        user = session.get(User, user_uuid)
        if user is None:
            user = User(id=user_uuid, email=f"{user_uuid}@unknown.local")
            session.add(user)
            session.commit()
            session.refresh(user)
        return user

    assert email is not None
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(email=email, display_name=email.split("@", 1)[0])
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def upsert_entra_user(session: Session, claims: dict) -> User:
    """Resolve (and lazily create) the ``User`` for a validated Entra token.

    Matches on the token's ``oid`` first, then email; backfills ``entra_oid`` /
    ``display_name`` on an existing email-matched row so an analyst who used
    ``X-User-Id`` in dev links up to their Entra identity on first SSO.
    """
    oid = claims.get("oid") or claims.get("sub")
    email = (
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("upn")
    )
    name = claims.get("name")

    user: User | None = None
    if oid:
        user = session.execute(
            select(User).where(User.entra_oid == oid)
        ).scalar_one_or_none()
    if user is None and email:
        user = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()

    if user is None:
        user = User(
            email=email or f"{oid}@entra.local",
            display_name=name or (email.split("@", 1)[0] if email else None),
            entra_oid=oid,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    changed = False
    if oid and not user.entra_oid:
        user.entra_oid = oid
        changed = True
    if name and not user.display_name:
        user.display_name = name
        changed = True
    if changed:
        session.commit()
        session.refresh(user)
    return user


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def current_user(
    session: Annotated[Session, Depends(db_session)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> User:
    """Resolve the request's acting user.

    Preference order:
    1. ``X-API-Key`` (CLI / automation) — return the user who minted the key.
       If the key has no ``created_by`` (the bootstrap admin key), synthesize a
       deterministic ``system@deployment.local`` user so audit rows have an id.
    2. ``Authorization: Bearer`` Entra access token (browser SSO) — validated
       against the tenant JWKS, mapped to a ``User`` by ``oid``. Only consulted
       when Entra is configured; an invalid token is a hard 401.
    3. ``X-User-Id`` (dev/test) — upsert the named user.
    """
    if x_api_key:
        key = _lookup_api_key(session, x_api_key)
        if key.created_by is not None:
            user = session.get(User, key.created_by)
            if user is not None:
                return user
        # Bootstrap admin key (no creator): give audit a stable identity.
        system_email = "system@deployment.local"
        user = session.execute(
            select(User).where(User.email == system_email)
        ).scalar_one_or_none()
        if user is None:
            user = User(email=system_email, display_name="system")
            session.add(user)
            session.commit()
            session.refresh(user)
        return user

    token = _bearer_token(authorization)
    if token and settings.entra_enabled:
        from app.core.entra import EntraError, validate_token

        try:
            claims = validate_token(token)
        except EntraError as exc:
            raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc
        return upsert_entra_user(session, claims)

    if not x_user_id:
        raise HTTPException(
            status_code=401,
            detail="X-API-Key, Authorization: Bearer, or X-User-Id header required",
        )
    return upsert_user(session, x_user_id)


DbSession = Annotated[Session, Depends(db_session)]
RedisClient = Annotated[redis_lib.Redis, Depends(redis_client)]
AsyncRedisClient = Annotated[aioredis.Redis, Depends(async_redis_client)]
CurrentUser = Annotated[User, Depends(current_user)]
CurrentAPIKey = Annotated[APIKey, Depends(api_key_auth)]
