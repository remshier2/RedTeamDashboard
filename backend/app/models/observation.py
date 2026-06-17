from __future__ import annotations

import uuid

from sqlalchemy import Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid7
from app.models.finding import FindingPhase


class Observation(Base, TimestampMixin):
    """Freeform analyst note attached to an engagement.

    Sits between the live Redis event stream (ephemeral) and a formal Finding
    (requires validation). Use for things noticed during recon that don't yet
    warrant a finding — cert oddities, interesting headers, login portal details.
    """

    __tablename__ = "observations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("engagements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[FindingPhase | None] = mapped_column(
        Enum(FindingPhase, name="finding_phase"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
