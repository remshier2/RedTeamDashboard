"""Approvals HTTP surface.

- ``GET  /engagements/{eid}/approvals?status=pending`` — list rows for an engagement
- ``GET  /approvals/{id}``                            — fetch one
- ``POST /approvals/{id}/decision``                   — decide a pending approval

The decision endpoint updates the row in-place and pushes a ``run.resume``
envelope onto ``runs:{engagement_id}:in`` so the worker can resume the paused
LangGraph thread with ``Command(resume=...)``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, RedisClient
from app.models import (
    ActorType,
    Approval,
    ApprovalStatus,
    AuditLog,
    Authorization,
)
from app.runs.events import encode_command
from app.runs.streams import inbound_stream, load_run_model
from app.schemas.approval import ApprovalDecision, ApprovalRead

router = APIRouter()


@router.get(
    "/engagements/{engagement_id}/approvals",
    response_model=list[ApprovalRead],
)
def list_approvals(
    engagement_id: UUID,
    session: DbSession,
    status: Annotated[ApprovalStatus | None, Query()] = None,
) -> list[Approval]:
    stmt = select(Approval).where(Approval.engagement_id == engagement_id)
    if status is not None:
        stmt = stmt.where(Approval.status == status)
    stmt = stmt.order_by(Approval.created_at.desc())
    return list(session.execute(stmt).scalars())


@router.get("/approvals/{approval_id}", response_model=ApprovalRead)
def get_approval(approval_id: UUID, session: DbSession) -> Approval:
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return approval


@router.post(
    "/approvals/{approval_id}/decision",
    response_model=ApprovalRead,
)
def decide_approval(
    approval_id: UUID,
    body: ApprovalDecision,
    session: DbSession,
    redis_client: RedisClient,
    user: CurrentUser,
) -> Approval:
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if approval.status is not ApprovalStatus.pending:
        raise HTTPException(
            status_code=409,
            detail=f"approval is {approval.status.value}, not pending",
        )

    if body.approved:
        approval.status = (
            ApprovalStatus.edited if body.edited_args else ApprovalStatus.approved
        )
    else:
        approval.status = ApprovalStatus.denied
    approval.decided_by = user.id
    approval.decided_at = datetime.now(tz=UTC)

    decision_args: dict[str, object] = {"approved": body.approved}
    if body.edited_args:
        decision_args["edited_args"] = body.edited_args
    if body.reason:
        decision_args["reason"] = body.reason
    approval.decision_args = decision_args

    # Approving "for the session" grants a standing per-(engagement, tool)
    # authorization so future in-scope calls to this tool auto-run. Reuse an
    # existing active grant rather than duplicating it.
    if body.approved and body.remember_for_session:
        grant = session.execute(
            select(Authorization).where(
                Authorization.engagement_id == approval.engagement_id,
                Authorization.tool_name == approval.tool_name,
                Authorization.revoked_at.is_(None),
            )
        ).scalar_one_or_none()
        if grant is None:
            grant = Authorization(
                engagement_id=approval.engagement_id,
                tool_name=approval.tool_name,
                granted_by=user.id,
                note=f"granted while approving a {approval.tool_name} call",
            )
            session.add(grant)
            session.flush()  # assign grant.id
            session.add(
                AuditLog(
                    engagement_id=approval.engagement_id,
                    actor_type=ActorType.user,
                    actor_id=str(user.id),
                    event_type="authorization.granted",
                    payload={
                        "authorization_id": str(grant.id),
                        "tool": approval.tool_name,
                        "via_approval_id": str(approval.id),
                    },
                )
            )
        approval.authorization_id = grant.id

    session.add(
        AuditLog(
            engagement_id=approval.engagement_id,
            actor_type=ActorType.user,
            actor_id=str(user.id),
            event_type="approval.decided",
            payload={
                "approval_id": str(approval.id),
                "thread_id": approval.thread_id,
                "tool": approval.tool_name,
                "status": approval.status.value,
                "approved": body.approved,
                **({"edited_args": body.edited_args} if body.edited_args else {}),
                **({"reason": body.reason} if body.reason else {}),
            },
        )
    )
    session.commit()
    session.refresh(approval)

    resume_payload: dict[str, object] = {
        "type": "run.resume",
        "thread_id": approval.thread_id,
        "approved": body.approved,
    }
    if body.edited_args:
        resume_payload["edited_args"] = body.edited_args
    if body.reason:
        resume_payload["reason"] = body.reason

    # Carry the original run's model choice forward so the worker uses the
    # same LLM on resume. Missing only if the cache TTL expired (>6h since
    # run.start) — in that case the worker falls back to env defaults.
    cached_model = load_run_model(redis_client, approval.thread_id)
    if cached_model is not None:
        resume_payload["model"] = cached_model

    redis_client.xadd(
        inbound_stream(approval.engagement_id),
        encode_command(resume_payload),
    )

    return approval
