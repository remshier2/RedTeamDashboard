from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, uuid7
from app.models.suggestion import AgentName


class AgentTrigger(enum.StrEnum):
    """Why the orchestrator agent fired. ``finding`` = a new finding
    landed; ``task`` = a task completed and Strategic wants to re-plan;
    ``manual`` = analyst clicked the slide-over button; ``tick`` = periodic
    watcher cadence (Phase 10)."""

    finding = "finding"
    task = "task"
    manual = "manual"
    tick = "tick"


class AgentExecutionStatus(enum.StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"


class AgentExecution(Base):
    """One Strategic or Tactical LLM call. Used for trace + Costs tab roll-up."""

    __tablename__ = "agent_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("engagements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent: Mapped[AgentName] = mapped_column(
        Enum(AgentName, name="agent_name"), nullable=False, index=True
    )
    trigger: Mapped[AgentTrigger] = mapped_column(
        Enum(AgentTrigger, name="agent_trigger"), nullable=False
    )
    input: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    model_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    status: Mapped[AgentExecutionStatus] = mapped_column(
        Enum(AgentExecutionStatus, name="agent_execution_status"),
        default=AgentExecutionStatus.running,
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
