"""findings: phase + validation status

Phase 8 makes findings tab-aware and gates them behind analyst validation:
- ``phase``  — which engagement phase tab a finding belongs to.
- ``status`` — agent/tool findings start ``pending_validation``; an analyst
  promotes them to ``validated`` (report-eligible) or rejects them.
- ``validated_by`` / ``validated_at`` — who validated and when.

Existing rows predate the gate, so they are backfilled to ``validated`` with a
``phase`` derived from their ``source_tool``.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-16
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PHASE = sa.Enum(
    "osint", "vuln_scan", "exploit", "phishing", "general", name="finding_phase"
)
_STATUS = sa.Enum(
    "pending_validation",
    "validated",
    "rejected",
    "false_positive",
    name="finding_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    _PHASE.create(bind, checkfirst=True)
    _STATUS.create(bind, checkfirst=True)

    op.add_column(
        "findings",
        sa.Column("phase", _PHASE, nullable=False, server_default="general"),
    )
    op.add_column(
        "findings",
        sa.Column(
            "status", _STATUS, nullable=False, server_default="pending_validation"
        ),
    )
    op.add_column(
        "findings",
        sa.Column("validated_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "findings",
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_findings_validated_by_users",
        "findings",
        "users",
        ["validated_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_findings_phase", "findings", ["phase"])
    op.create_index("ix_findings_status", "findings", ["status"])

    # Backfill: existing findings predate the gate → mark validated, and derive
    # phase from the tool that produced them.
    op.execute(
        """
        UPDATE findings SET
          status = 'validated'::finding_status,
          phase = (CASE
            WHEN source_tool IN
              ('subfinder','crt_sh','dns_lookup','whois_lookup','httpx_probe','reverse_dns')
              THEN 'osint'
            WHEN source_tool IN ('portscan','subnet_sweep','service_detect')
              THEN 'vuln_scan'
            ELSE 'general'
          END)::finding_phase
        """
    )


def downgrade() -> None:
    op.drop_index("ix_findings_status", table_name="findings")
    op.drop_index("ix_findings_phase", table_name="findings")
    op.drop_constraint("fk_findings_validated_by_users", "findings", type_="foreignkey")
    op.drop_column("findings", "validated_at")
    op.drop_column("findings", "validated_by")
    op.drop_column("findings", "status")
    op.drop_column("findings", "phase")
    op.execute("DROP TYPE IF EXISTS finding_status")
    op.execute("DROP TYPE IF EXISTS finding_phase")
