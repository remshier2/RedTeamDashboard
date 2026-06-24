"""entities — persistent OSINT data points (Phase 10 Maltego import target).

Complements the existing derived-on-the-fly entity view
(``app/services/entities.py``). Imported entities are stored here with
a uniqueness constraint on ``(engagement_id, type, value)`` so
re-importing the same Maltego graph merges into existing rows.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-24
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "engagement_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("engagements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(80), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "properties",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("source_tool", sa.String(80), nullable=False),
        sa.Column("source_attribution", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "engagement_id",
            "type",
            "value",
            name="uq_entities_engagement_type_value",
        ),
    )
    op.create_index(
        "ix_entities_engagement_id", "entities", ["engagement_id"]
    )
    op.create_index("ix_entities_type", "entities", ["type"])


def downgrade() -> None:
    op.drop_index("ix_entities_type", table_name="entities")
    op.drop_index("ix_entities_engagement_id", table_name="entities")
    op.drop_table("entities")
