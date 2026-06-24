"""HTTP surface for workflow templates — Phase 10 starter packs.

Two endpoints in v1:

    GET    /workflow-templates                              -> list all
    POST   /engagements/{slug}/templates/{template_id}/apply -> mint Tasks

Apply creates ``Task`` rows in ``status=pending``; the analyst still
has to dispatch each via the existing Tactical path. Matches the
Strategic-suggestion flow's posture (suggest, don't auto-run).

User-authored templates (``is_system=false``) CRUD is deferred to a
follow-on PR; this module only reads + applies.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models import ActorType, AuditLog, Engagement, Task, WorkflowTemplate
from app.services import workflow_templates as wt_service

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class WorkflowTemplateStep(BaseModel):
    """A single step in a template, mirrored verbatim from the JSONB column."""

    tool: str
    kind: str
    owner_eligibility: str
    title: str
    rationale: str | None = None


class WorkflowTemplateRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_system: bool
    target_kind: str
    steps: list[dict[str, Any]] = Field(default_factory=list)


class ApplyTemplateRequest(BaseModel):
    target: str = Field(
        ...,
        min_length=1,
        description="Value substituted into each step's payload.target.",
    )


class AppliedTaskRead(BaseModel):
    id: uuid.UUID
    title: str
    kind: str
    owner_eligibility: str
    status: str
    payload: dict[str, Any]


class ApplyTemplateResponse(BaseModel):
    template_id: uuid.UUID
    template_name: str
    target: str
    tasks: list[AppliedTaskRead]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engagement_by_slug(session, slug: str) -> Engagement:
    eng = session.execute(
        select(Engagement).where(Engagement.slug == slug)
    ).scalar_one_or_none()
    if eng is None:
        raise HTTPException(
            status_code=404, detail=f"engagement '{slug}' not found"
        )
    return eng


def _template_to_read(tpl: WorkflowTemplate) -> WorkflowTemplateRead:
    return WorkflowTemplateRead(
        id=tpl.id,
        name=tpl.name,
        description=tpl.description,
        is_system=tpl.is_system,
        target_kind=tpl.target_kind,
        steps=list(tpl.steps or []),
    )


def _task_to_read(task: Task) -> AppliedTaskRead:
    return AppliedTaskRead(
        id=task.id,
        title=task.title,
        kind=task.kind.value,
        owner_eligibility=task.owner_eligibility.value,
        status=task.status.value,
        payload=dict(task.payload or {}),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/workflow-templates",
    response_model=list[WorkflowTemplateRead],
)
def list_workflow_templates(session: DbSession) -> list[WorkflowTemplateRead]:
    """All templates, system-first."""
    return [_template_to_read(t) for t in wt_service.list_templates(session)]


@router.post(
    "/engagements/{slug}/templates/{template_id}/apply",
    response_model=ApplyTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
def apply_workflow_template(
    slug: str,
    template_id: uuid.UUID,
    body: ApplyTemplateRequest,
    session: DbSession,
    user: CurrentUser,
) -> ApplyTemplateResponse:
    """Mint one Task per step in the template, parametrized by ``target``.

    Tasks land ``status=pending``; analyst dispatches via the existing
    Tactical path. Audit row records the apply for traceability.
    """
    eng = _engagement_by_slug(session, slug)
    tpl = wt_service.get_template_or_none(session, template_id)
    if tpl is None:
        raise HTTPException(
            status_code=404, detail=f"workflow template {template_id} not found"
        )
    try:
        tasks = wt_service.apply_template(
            session, template=tpl, engagement=eng, target=body.target
        )
    except wt_service.WorkflowTemplateApplyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session.add(
        AuditLog(
            engagement_id=eng.id,
            actor_type=ActorType.user,
            actor_id=str(user.id),
            event_type="workflow_template.applied",
            payload={
                "template_id": str(tpl.id),
                "template_name": tpl.name,
                "target": body.target,
                "tasks_created": len(tasks),
            },
        )
    )
    session.commit()
    for t in tasks:
        session.refresh(t)

    return ApplyTemplateResponse(
        template_id=tpl.id,
        template_name=tpl.name,
        target=body.target,
        tasks=[_task_to_read(t) for t in tasks],
    )
