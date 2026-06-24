"""Workflow templates — Phase 10 starter packs.

A workflow template bundles N ordered ``steps`` (tool + kind + title +
rationale + owner_eligibility) that an analyst can apply to an
engagement with a single target. Apply creates one ``Task`` per step,
parametrized by the supplied target.

``is_system=true`` rows are seeded idempotently at startup from
``app.services.workflow_templates.STARTER_TEMPLATES`` (Network Recon,
OSINT Enum, Web App per CHARTER §16 RESOLVED). They are immutable —
the seed function refuses to mutate an existing system row, and the
API forbids edit/delete on ``is_system=true``. User-authored templates
(``is_system=false``) are deferred to a follow-on PR.

Step shape, validated by the API at write time::

    {
        "tool": "subfinder",            # name from the tool registry
        "kind": "enum",                  # TaskKind enum value
        "owner_eligibility": "agent",    # OwnerEligibility enum value
        "title": "Enumerate subdomains",
        "rationale": "Wide passive recon to map the attack surface."
    }
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid7


class WorkflowTemplate(Base, TimestampMixin):
    """Reusable workflow pack — ordered list of task specs keyed by a
    single typed target input."""

    __tablename__ = "workflow_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Code-seeded starter set vs analyst-authored. System rows are
    # immutable; the seed function and API both enforce this.
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Single typed target input for now — "domain" / "cidr" / "url" / "ip".
    # Multi-input parametrization (e.g. domain + scan_range) is intentionally
    # deferred until a real template needs it.
    target_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
