"""Wire-format models for engagements, scope items, and run kickoff."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models import EngagementStatus, ScopeKind

LLMProvider = Literal["anthropic", "openai", "azure", "ollama"]


class EngagementCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(
        default=None,
        max_length=200,
        description="Optional. Auto-generated from `name` if omitted.",
    )


class EngagementUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: EngagementStatus | None = Field(
        default=None,
        description=(
            "Only `active` or `archived` are accepted via PATCH. Use "
            "POST /engagements/{slug}/flush for irreversible deletion."
        ),
    )


class EngagementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    status: EngagementStatus
    created_by: UUID | None
    archived_at: datetime | None
    flushed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ScopeItemCreate(BaseModel):
    kind: ScopeKind
    value: str = Field(min_length=1, max_length=500)
    is_exclusion: bool = False
    note: str | None = Field(default=None, max_length=500)


class ScopeItemUpdate(BaseModel):
    value: str | None = Field(default=None, min_length=1, max_length=500)
    is_exclusion: bool | None = None
    note: str | None = Field(default=None, max_length=500)


class ScopeItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    engagement_id: UUID
    kind: ScopeKind
    value: str
    is_exclusion: bool
    note: str | None
    created_at: datetime
    updated_at: datetime


class RunModel(BaseModel):
    """Per-run LLM choice — overrides the worker's env defaults."""

    provider: LLMProvider
    name: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Model id passed to the provider's SDK (e.g. 'claude-opus-4-7', "
            "'gpt-4o-mini'). Not whitelisted server-side — model names churn "
            "faster than this repo."
        ),
    )


class RunStart(BaseModel):
    prompt: str = Field(min_length=1)
    model: RunModel | None = Field(
        default=None,
        description=(
            "Optional per-run LLM. If omitted, the worker uses its env "
            "defaults (LLM_PROVIDER + provider-specific model env)."
        ),
    )


class RunStartResponse(BaseModel):
    engagement_id: UUID
    thread_id: UUID
    events_stream: str
    model: RunModel
    "The effective model used for this run (echoes the request, or the env default)."
