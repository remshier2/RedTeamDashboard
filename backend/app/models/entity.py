"""Stored entities — Phase 10.

An ``Entity`` is an OSINT-shaped data point (domain, email, IP, person,
ASN, phone, etc.) that landed in the engagement via an external import.
This complements the existing on-the-fly *derived* entity view
(``app/services/entities.py``), which extracts entity values out of
``Finding.target`` + ``Finding.details``. Imported entities are
persistent first-class records; derived ones stay derived.

Type is a free-form string on purpose: Maltego defines a long list of
``maltego.*`` types that don't neatly compress into a Postgres enum,
and the analyst's library will grow over time. The free-form choice
matches what the existing derived view already does (``EntityType =
str`` in ``services/entities.py``).

Uniqueness on ``(engagement_id, type, value)``: re-importing the same
Maltego graph merges into existing rows rather than duplicating them.
Properties JSONB on conflict is patched in (new values win) by the
``entity_store.persist_entities`` UPSERT.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid7


class Entity(Base, TimestampMixin):
    """One persisted OSINT data point tied to an engagement."""

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint(
            "engagement_id",
            "type",
            "value",
            name="uq_entities_engagement_type_value",
        ),
        Index("ix_entities_engagement_id", "engagement_id"),
        Index("ix_entities_type", "type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    engagement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("engagements.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Free-form: "domain", "email", "ip", "person", "asn", "phone",
    # "maltego.Hash", etc. Matches the existing derived view's vocabulary
    # and extends it for Maltego-specific types.
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    # Extra fields from the source (Maltego properties, etc.). Stored
    # verbatim; renderer is responsible for choosing what to display.
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    # Where the entity came from. Examples:
    # source_tool="maltego_import", source_attribution="scan-2026-01-15.mtgx".
    source_tool: Mapped[str] = mapped_column(String(80), nullable=False)
    source_attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
