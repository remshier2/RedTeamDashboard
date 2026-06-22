"""Schemas for the Costs tab roll-up (GET /engagements/{slug}/costs)."""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from app.models import AgentName


class CostBucket(BaseModel):
    """Summed usage over a set of agent executions."""

    executions: int
    tokens_in: int
    tokens_out: int
    cost_usd: float


class AgentCost(CostBucket):
    agent: AgentName


class ModelCost(CostBucket):
    provider: str | None
    model: str | None
    # False when the model has no entry in the pricing table — its tokens are
    # counted but cost_usd is $0 and the name appears in unpriced_models.
    priced: bool


class CostRollup(BaseModel):
    engagement_id: UUID
    engagement_slug: str
    total: CostBucket
    by_agent: list[AgentCost]
    by_model: list[ModelCost]
    unpriced_models: list[str]
