from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid7


class TaskKind(enum.StrEnum):
    """What kind of work the task is. Agents may run scan/enum only;
    exploit is analyst-owned (CHARTER invariant — enforced in
    ``TacticalAgent.dispatch``)."""

    scan = "scan"
    enum = "enum"
    exploit = "exploit"


class OwnerEligibility(enum.StrEnum):
    agent = "agent"
    analyst = "analyst"
    either = "either"


class TaskStatus(enum.StrEnum):
    pending = "pending"
    dispatched = "dispatched"
    running = "running"
    completed = "completed"
    failed = "failed"
    deferred = "deferred"
    cancelled = "cancelled"


class Task(Base, TimestampMixin):
    """A unit of orchestrator-emitted work tied to an engagement.

    Tasks may originate from an accepted ``Suggestion`` (Strategic) or be
    minted directly by analyst action. ``payload`` carries the tool name +
    args Tactical needs to launch a worker run.
    """

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("engagements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    finding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("findings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[TaskKind] = mapped_column(
        Enum(TaskKind, name="task_kind"), nullable=False
    )
    owner_eligibility: Mapped[OwnerEligibility] = mapped_column(
        Enum(OwnerEligibility, name="task_owner_eligibility"), nullable=False
    )
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"),
        default=TaskStatus.pending,
        nullable=False,
        index=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
