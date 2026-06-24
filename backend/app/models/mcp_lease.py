"""Per-task MCP lease: the curated tool/context/prompt surface a single
Execution Agent run is allowed to see.

Strategic mints one of these via ``app.services.mcp_lease.mint`` before
Tactical dispatches a Task; the lease's id is the bearer token the worker
includes on its envelope. The MCP server filters every ``tools/list``,
``prompts/list``, and tool invocation by the active lease — so even though
the same MCP process serves every task, each task sees a private surface.

The released/expired lifecycle is owned by the strategic consumer and the
periodic sweeper; nothing user-facing reads this table directly.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, uuid7


class MCPLeaseStatus(enum.StrEnum):
    active = "active"
    released = "released"
    expired = "expired"


class MCPLease(Base):
    """One per-task lease. Status transitions: active → (released | expired)."""

    __tablename__ = "mcp_leases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'released', 'expired')",
            name="ck_mcp_leases_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("engagements.id", ondelete="CASCADE"),
        nullable=False,
    )
    allowed_tools: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    prompt_keys: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Stage 2: when True, Tactical provisions an ephemeral Azure Container
    # Apps Job to host the MCP server for this run instead of using the
    # colocated server. Strategic decides; default False so the rollout is
    # opt-in until LLM-driven policy lands in Stage 3.
    requires_container: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
