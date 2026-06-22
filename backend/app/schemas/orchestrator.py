"""Wire schemas for the Phase 9 orchestrator layer.

Mirrors the SQLAlchemy models in ``app/models/{task,suggestion,agent_execution}.py``.
Kept in a single file because the three entities are read together (the slide-
over surface shows suggestions + the tasks they spawned).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.agent_execution import AgentExecutionStatus, AgentTrigger
from app.models.suggestion import AgentName, SuggestionKind, SuggestionStatus
from app.models.task import OwnerEligibility, TaskKind, TaskStatus


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    engagement_id: UUID
    finding_id: UUID | None
    title: str
    kind: TaskKind
    owner_eligibility: OwnerEligibility
    status: TaskStatus
    payload: dict[str, Any]
    run_id: UUID | None
    dispatched_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SuggestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    engagement_id: UUID
    finding_id: UUID | None
    title: str
    body: str | None
    kind: SuggestionKind
    payload: dict[str, Any]
    status: SuggestionStatus
    created_by_agent: AgentName
    decided_by: UUID | None
    decided_at: datetime | None
    task_id: UUID | None
    created_at: datetime
    updated_at: datetime


class AnalyzeFindingResponse(BaseModel):
    """What ``POST /findings/{id}/analyze`` returns: the Strategic agent's
    suggestions plus the AgentExecution id so the caller can correlate."""

    execution_id: UUID
    suggestions: list[SuggestionRead]


class AcceptSuggestionResponse(BaseModel):
    """Returned by ``POST /suggestions/{id}/accept``.

    ``task`` is the newly minted Task row (only for kind=``task``).
    ``dispatched`` is true when Tactical immediately fired a worker run for it
    (agent-eligible + scan/enum). Active/destructive tools still pause at the
    existing approval gate inside the worker."""

    suggestion: SuggestionRead
    task: TaskRead | None
    dispatched: bool


class AgentExecutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    engagement_id: UUID
    agent: AgentName
    trigger: AgentTrigger
    input: dict[str, Any]
    output: dict[str, Any] | None
    model_provider: str | None
    model_name: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None = Field(default=None)
    status: AgentExecutionStatus
    error: str | None
    started_at: datetime
    completed_at: datetime | None
