from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid7


class Severity(enum.StrEnum):
    info = "info"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class FindingPhase(enum.StrEnum):
    """Engagement phase a finding belongs to — drives which tab it shows in."""

    osint = "osint"
    vuln_scan = "vuln_scan"
    exploit = "exploit"
    phishing = "phishing"
    general = "general"


class FindingStatus(enum.StrEnum):
    """Validation state. Agent/tool findings start ``pending_validation`` and
    only become report-eligible once an analyst marks them ``validated``."""

    pending_validation = "pending_validation"
    validated = "validated"
    rejected = "rejected"
    false_positive = "false_positive"


class Finding(Base, TimestampMixin):
    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("engagements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    severity: Mapped[Severity] = mapped_column(
        Enum(Severity, name="finding_severity"), default=Severity.info, nullable=False
    )
    summary: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    source_tool: Mapped[str | None] = mapped_column(String(120), index=True)
    target: Mapped[str | None] = mapped_column(String(500), index=True)

    phase: Mapped[FindingPhase] = mapped_column(
        Enum(FindingPhase, name="finding_phase"),
        default=FindingPhase.general,
        nullable=False,
        index=True,
    )
    status: Mapped[FindingStatus] = mapped_column(
        Enum(FindingStatus, name="finding_status"),
        default=FindingStatus.pending_validation,
        nullable=False,
        index=True,
    )
    validated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
