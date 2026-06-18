from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid7


class Attachment(Base, TimestampMixin):
    """Binary file attached to a Finding (screenshots, evidence, etc.).

    Raw bytes stored in Postgres as BYTEA — suitable for screenshots up to ~10 MB.
    """

    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("findings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("engagements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
