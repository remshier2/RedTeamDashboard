"""MCP lease lifecycle service.

The lease is the authoritative store for the per-task MCP surface the
Execution Agent is allowed to see. Mint when Tactical dispatches a Task,
release when the run completes/errors, expire on TTL otherwise. Release
is idempotent so a redelivered terminal event doesn't blow up.

The lease's UUID is the bearer token — Postgres-native, unguessable
enough for an internal-network use case (Stage 1). When Stage 2 ships
real ephemeral containers, the token will pass through the container's
own auth on top of this lookup.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import MCPLease, MCPLeaseStatus, Task

logger = structlog.get_logger(__name__)


def mint(
    session: Session,
    *,
    task: Task,
    allowed_tools: list[str],
    context: dict[str, Any],
    prompt_keys: list[str],
    ttl_seconds: int = 3600,
    requires_container: bool = False,
) -> MCPLease:
    """Create a new active lease for ``task``. Caller commits.

    ``requires_container`` opts into Stage 2 ephemeral MCP hosting for
    this lease — Tactical reads the column to decide whether to provision
    an Azure Container Apps Job per dispatch or use the colocated MCP.
    """
    now = datetime.now(tz=UTC)
    lease = MCPLease(
        task_id=task.id,
        engagement_id=task.engagement_id,
        allowed_tools=list(allowed_tools),
        context=dict(context),
        prompt_keys=list(prompt_keys),
        status=MCPLeaseStatus.active.value,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        requires_container=requires_container,
    )
    session.add(lease)
    session.flush()
    logger.info(
        "mcp_lease.minted",
        lease_id=str(lease.id),
        task_id=str(task.id),
        engagement_id=str(task.engagement_id),
        tools=allowed_tools,
        ttl_seconds=ttl_seconds,
        requires_container=requires_container,
    )
    return lease


def release(
    session: Session,
    *,
    lease_id: uuid.UUID,
    reason: str | None = None,
) -> MCPLease | None:
    """Flip an active lease to released. Idempotent — returns None if the
    lease doesn't exist; logs a no-op if it's already released/expired."""
    lease = session.get(MCPLease, lease_id)
    if lease is None:
        logger.warning("mcp_lease.release_unknown", lease_id=str(lease_id))
        return None
    if lease.status != MCPLeaseStatus.active.value:
        logger.info(
            "mcp_lease.release_noop",
            lease_id=str(lease_id),
            current_status=lease.status,
            reason=reason,
        )
        return lease
    lease.status = MCPLeaseStatus.released.value
    lease.released_at = datetime.now(tz=UTC)
    session.flush()
    logger.info(
        "mcp_lease.released",
        lease_id=str(lease_id),
        reason=reason,
    )
    return lease


def extend(
    session: Session,
    *,
    lease_id: uuid.UUID,
    additional_seconds: int,
) -> MCPLease | None:
    """Push out an active lease's expiry. No-op on released/expired leases."""
    lease = session.get(MCPLease, lease_id)
    if lease is None:
        return None
    if lease.status != MCPLeaseStatus.active.value:
        logger.info(
            "mcp_lease.extend_noop",
            lease_id=str(lease_id),
            current_status=lease.status,
        )
        return lease
    lease.expires_at = lease.expires_at + timedelta(seconds=additional_seconds)
    session.flush()
    return lease


def sweep_expired(session: Session) -> int:
    """Flip active leases whose expires_at has passed to status=expired.

    Returns the number of leases swept. Called by the background sweeper.
    """
    now = datetime.now(tz=UTC)
    result = session.execute(
        update(MCPLease)
        .where(
            MCPLease.status == MCPLeaseStatus.active.value,
            MCPLease.expires_at < now,
        )
        .values(status=MCPLeaseStatus.expired.value)
    )
    session.flush()
    count = result.rowcount or 0
    if count:
        logger.info("mcp_lease.swept_expired", count=count)
    return count


def find_active_for_task(
    session: Session, task_id: uuid.UUID
) -> MCPLease | None:
    """Most recent active lease for ``task_id``, or None.

    Multiple active leases for one task shouldn't happen normally — if it
    does, ``ORDER BY created_at DESC`` gives the consumer the freshest."""
    return session.execute(
        select(MCPLease)
        .where(
            MCPLease.task_id == task_id,
            MCPLease.status == MCPLeaseStatus.active.value,
        )
        .order_by(MCPLease.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def validate_token(session: Session, token: str) -> MCPLease | None:
    """Resolve a bearer token to an active, unexpired lease. Returns None
    on malformed UUID, unknown lease, or non-active/expired status. The
    caller is responsible for any 401 mapping."""
    try:
        lease_id = uuid.UUID(token)
    except (ValueError, TypeError):
        return None
    lease = session.get(MCPLease, lease_id)
    if lease is None:
        return None
    if lease.status != MCPLeaseStatus.active.value:
        return None
    if lease.expires_at < datetime.now(tz=UTC):
        return None
    return lease
