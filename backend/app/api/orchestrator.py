"""HTTP surface for the Phase 9 orchestrator.

This module provides the API for the Strategic and Tactical orchestrator agents
that assist analysts during authorized security engagements.

Agents perform **enumeration and scanning only**. They analyze findings and suggest
tasks, but validation/proof-of-concept work (TaskKind.exploit) is **analyst-only**
— the service layer refuses to dispatch such tasks to agents. All agent actions
are audit-logged via AgentExecution records.

Endpoints::

    POST   /findings/{finding_id}/analyze              -> Strategic on demand
    GET    /engagements/{slug}/suggestions             -> list (?status filter)
    POST   /suggestions/{suggestion_id}/accept         -> mint Task (+ dispatch)
    POST   /suggestions/{suggestion_id}/dismiss        -> close without acting
    GET    /engagements/{slug}/tasks                   -> list (?status filter)

Accept implicitly dispatches when the suggestion's task would be agent-eligible
(scan/enum + owner_eligibility != analyst). The dispatched run lands on the
worker's existing inbound stream and goes through the same approval gate as a
hand-started run, so an active tool still pauses for an analyst decision.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import StrategicAgent, TacticalAgent, TacticalRefusedExploit
from app.api.deps import CurrentUser, DbSession, RedisClient
from app.core import pricing
from app.models import (
    ActorType,
    AgentExecution,
    AgentName,
    AgentTrigger,
    AuditLog,
    Engagement,
    Finding,
    OwnerEligibility,
    Suggestion,
    SuggestionKind,
    SuggestionStatus,
    Task,
    TaskKind,
    TaskStatus,
)
from app.schemas.cost import AgentCost, CostBucket, CostRollup, ModelCost
from app.schemas.orchestrator import (
    AcceptSuggestionResponse,
    AnalyzeFindingResponse,
    SuggestionRead,
    TaskRead,
)

router = APIRouter()


def _engagement_by_slug(session: Session, slug: str) -> Engagement:
    eng = session.execute(
        select(Engagement).where(Engagement.slug == slug)
    ).scalar_one_or_none()
    if eng is None:
        raise HTTPException(status_code=404, detail=f"engagement '{slug}' not found")
    return eng


# ---------------------------------------------------------------------------
# Analyze a finding (manual Strategic trigger)
# ---------------------------------------------------------------------------


@router.post(
    "/findings/{finding_id}/analyze",
    response_model=AnalyzeFindingResponse,
)
def analyze_finding(
    finding_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> AnalyzeFindingResponse:
    """Run the Strategic watcher synchronously over one finding.

    Used by the findings slide-over's Agent button: the analyst clicks,
    Strategic plans, suggestions render inline. The event-driven path (worker
    subscriber) writes to the same tables out-of-band.
    """
    finding = session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")

    agent = StrategicAgent()
    execution, suggestions = agent.analyze_finding(
        session, finding=finding, trigger=AgentTrigger.manual
    )

    session.add(
        AuditLog(
            engagement_id=finding.engagement_id,
            actor_type=ActorType.user,
            actor_id=str(user.id),
            event_type="strategic.analyzed",
            payload={
                "finding_id": str(finding.id),
                "execution_id": str(execution.id),
                "suggestion_count": len(suggestions),
            },
        )
    )
    session.commit()
    for s in suggestions:
        session.refresh(s)

    return AnalyzeFindingResponse(
        execution_id=execution.id,
        suggestions=[SuggestionRead.model_validate(s) for s in suggestions],
    )


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------


@router.get(
    "/engagements/{slug}/suggestions",
    response_model=list[SuggestionRead],
)
def list_suggestions(
    slug: str,
    session: DbSession,
    _user: CurrentUser,
    suggestion_status: Annotated[
        SuggestionStatus | None,
        Query(alias="status", description="Filter by status (default: open)."),
    ] = SuggestionStatus.open,
) -> list[Suggestion]:
    eng = _engagement_by_slug(session, slug)
    stmt = select(Suggestion).where(Suggestion.engagement_id == eng.id)
    if suggestion_status is not None:
        stmt = stmt.where(Suggestion.status == suggestion_status)
    stmt = stmt.order_by(Suggestion.created_at.desc())
    return list(session.execute(stmt).scalars())


@router.post(
    "/suggestions/{suggestion_id}/accept",
    response_model=AcceptSuggestionResponse,
)
def accept_suggestion(
    suggestion_id: uuid.UUID,
    session: DbSession,
    redis_client: RedisClient,
    user: CurrentUser,
) -> AcceptSuggestionResponse:
    """Accept a Strategic suggestion.

    For ``kind=task`` suggestions: mint a ``Task`` row, then (if it's agent-
    eligible scan/enum) ask Tactical to dispatch it immediately. The dispatched
    run still hits the existing approval gate for active tools.
    """
    suggestion = session.get(Suggestion, suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    if suggestion.status != SuggestionStatus.open:
        raise HTTPException(
            status_code=409,
            detail=f"suggestion is {suggestion.status.value}; cannot accept",
        )

    suggestion.status = SuggestionStatus.accepted
    suggestion.decided_by = user.id
    suggestion.decided_at = datetime.now(tz=UTC)

    task: Task | None = None
    dispatched = False

    if suggestion.kind == SuggestionKind.task:
        payload = dict(suggestion.payload or {})
        kind_raw = payload.get("task_kind") or TaskKind.enum.value
        owner_raw = payload.get("owner_eligibility") or OwnerEligibility.either.value
        try:
            task_kind = TaskKind(kind_raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"invalid task_kind on suggestion payload: {kind_raw!r}",
            ) from exc
        try:
            owner_eligibility = OwnerEligibility(owner_raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"invalid owner_eligibility: {owner_raw!r}",
            ) from exc

        task = Task(
            engagement_id=suggestion.engagement_id,
            finding_id=suggestion.finding_id,
            title=suggestion.title,
            kind=task_kind,
            owner_eligibility=owner_eligibility,
            status=TaskStatus.pending,
            payload=payload,
        )
        session.add(task)
        session.flush()
        suggestion.task_id = task.id

        # Auto-dispatch agent-eligible scan/enum tasks. Analyst-only or
        # exploit tasks stay pending for manual action.
        agent_eligible_owner = owner_eligibility in (
            OwnerEligibility.agent,
            OwnerEligibility.either,
        )
        agent_eligible_kind = task_kind in (TaskKind.scan, TaskKind.enum)
        if agent_eligible_owner and agent_eligible_kind:
            tactical = TacticalAgent(redis_client)
            try:
                tactical.dispatch(
                    session, task=task, trigger=AgentTrigger.manual
                )
                dispatched = True
            except TacticalRefusedExploit:
                # Defense-in-depth — shouldn't fire since we checked kind, but
                # if it ever does, swallow and leave the task pending.
                dispatched = False

    session.add(
        AuditLog(
            engagement_id=suggestion.engagement_id,
            actor_type=ActorType.user,
            actor_id=str(user.id),
            event_type="suggestion.accepted",
            payload={
                "suggestion_id": str(suggestion.id),
                "task_id": str(task.id) if task else None,
                "dispatched": dispatched,
            },
        )
    )
    session.commit()
    session.refresh(suggestion)
    if task is not None:
        session.refresh(task)

    return AcceptSuggestionResponse(
        suggestion=SuggestionRead.model_validate(suggestion),
        task=TaskRead.model_validate(task) if task else None,
        dispatched=dispatched,
    )


@router.post(
    "/suggestions/{suggestion_id}/dismiss",
    response_model=SuggestionRead,
)
def dismiss_suggestion(
    suggestion_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> Suggestion:
    suggestion = session.get(Suggestion, suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    if suggestion.status != SuggestionStatus.open:
        raise HTTPException(
            status_code=409,
            detail=f"suggestion is {suggestion.status.value}; cannot dismiss",
        )
    suggestion.status = SuggestionStatus.dismissed
    suggestion.decided_by = user.id
    suggestion.decided_at = datetime.now(tz=UTC)
    session.add(
        AuditLog(
            engagement_id=suggestion.engagement_id,
            actor_type=ActorType.user,
            actor_id=str(user.id),
            event_type="suggestion.dismissed",
            payload={"suggestion_id": str(suggestion.id)},
        )
    )
    session.commit()
    session.refresh(suggestion)
    return suggestion


# ---------------------------------------------------------------------------
# Tasks (read-only for now; mutation happens via accept/dismiss)
# ---------------------------------------------------------------------------


@router.get("/engagements/{slug}/tasks", response_model=list[TaskRead])
def list_tasks(
    slug: str,
    session: DbSession,
    _user: CurrentUser,
    task_status: Annotated[
        TaskStatus | None,
        Query(alias="status", description="Filter by task status."),
    ] = None,
) -> list[Task]:
    eng = _engagement_by_slug(session, slug)
    stmt = select(Task).where(Task.engagement_id == eng.id)
    if task_status is not None:
        stmt = stmt.where(Task.status == task_status)
    stmt = stmt.order_by(Task.created_at.desc())
    return list(session.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# Costs (Phase 11) — per-engagement LLM spend roll-up over agent_executions
# ---------------------------------------------------------------------------


def _new_bucket() -> dict:
    return {"executions": 0, "tokens_in": 0, "tokens_out": 0, "cost": Decimal(0)}


def _as_bucket(acc: dict) -> CostBucket:
    return CostBucket(
        executions=acc["executions"],
        tokens_in=acc["tokens_in"],
        tokens_out=acc["tokens_out"],
        cost_usd=float(acc["cost"]),
    )


@router.get("/engagements/{slug}/costs", response_model=CostRollup)
def engagement_costs(
    slug: str,
    session: DbSession,
    _user: CurrentUser,
) -> CostRollup:
    """Roll up every Strategic/Tactical LLM call for the engagement by agent and
    by model, summing tokens and deriving USD via ``app.core.pricing``. Models
    with no pricing entry are counted but contribute $0 and are surfaced in
    ``unpriced_models`` so the UI can flag them."""
    eng = _engagement_by_slug(session, slug)
    rows = list(
        session.execute(
            select(AgentExecution).where(AgentExecution.engagement_id == eng.id)
        ).scalars()
    )

    total = _new_bucket()
    by_agent: dict[AgentName, dict] = {}
    by_model: dict[tuple[str | None, str | None], dict] = {}
    unpriced: set[str] = set()

    for ex in rows:
        used_in = ex.tokens_in or 0
        used_out = ex.tokens_out or 0
        derived = pricing.cost_usd(
            ex.model_name, used_in, used_out, ex.model_provider
        )
        priced = derived is not None
        cost = derived if priced else Decimal(0)
        if not priced and ex.model_name and (used_in or used_out):
            unpriced.add(ex.model_name)

        agent_acc = by_agent.setdefault(ex.agent, _new_bucket())
        model_acc = by_model.setdefault(
            (ex.model_provider, ex.model_name), _new_bucket()
        )
        for acc in (total, agent_acc, model_acc):
            acc["executions"] += 1
            acc["tokens_in"] += used_in
            acc["tokens_out"] += used_out
            acc["cost"] += cost
        model_acc["priced"] = priced

    return CostRollup(
        engagement_id=eng.id,
        engagement_slug=eng.slug,
        total=_as_bucket(total),
        by_agent=[
            AgentCost(agent=agent, **_as_bucket(acc).model_dump())
            for agent, acc in sorted(
                by_agent.items(), key=lambda kv: kv[1]["cost"], reverse=True
            )
        ],
        by_model=[
            ModelCost(
                provider=key[0],
                model=key[1],
                priced=acc.get("priced", False),
                **_as_bucket(acc).model_dump(),
            )
            for key, acc in sorted(
                by_model.items(), key=lambda kv: kv[1]["cost"], reverse=True
            )
        ],
        unpriced_models=sorted(unpriced),
    )
