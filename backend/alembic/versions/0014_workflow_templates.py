"""workflow_templates — Phase 10 starter packs.

Reusable template rows the analyst applies to an engagement to mint N
Tasks at once. ``is_system=true`` rows are code-seeded idempotently on
startup; user-authored templates land with ``is_system=false`` (CRUD
deferred to a follow-on PR).

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-24
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_templates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_system",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("target_kind", sa.String(40), nullable=False),
        sa.Column(
            "steps",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
    )
    op.create_index(
        "ix_workflow_templates_is_system",
        "workflow_templates",
        ["is_system"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_templates_is_system", table_name="workflow_templates"
    )
    op.drop_table("workflow_templates")
