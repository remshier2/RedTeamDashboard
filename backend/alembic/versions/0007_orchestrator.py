"""orchestrator tables: tasks, suggestions, agent_executions

Phase 9 — Strategic watcher + Tactical manager. Tasks are the unit of work
the orchestrator emits (scan|enum|exploit, owned by agent|analyst|either).
Suggestions are Strategic's recommendations the analyst accepts/dismisses.
agent_executions records every Strategic/Tactical LLM call for the cost rollup.

Hard invariant from the charter: agents scan, analysts exploit. Tactical
refuses to dispatch ``exploit``-kind tasks at the service layer; this schema
permits the value so analyst-owned exploit tasks can still be tracked.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-17
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    task_kind = postgresql.ENUM("scan", "enum", "exploit", name="task_kind")
    task_kind.create(op.get_bind(), checkfirst=True)

    owner_eligibility = postgresql.ENUM(
        "agent", "analyst", "either", name="task_owner_eligibility"
    )
    owner_eligibility.create(op.get_bind(), checkfirst=True)

    task_status = postgresql.ENUM(
        "pending",
        "dispatched",
        "running",
        "completed",
        "failed",
        "deferred",
        "cancelled",
        name="task_status",
    )
    task_status.create(op.get_bind(), checkfirst=True)

    suggestion_kind = postgresql.ENUM(
        "task", "ephemeral", "note", name="suggestion_kind"
    )
    suggestion_kind.create(op.get_bind(), checkfirst=True)

    suggestion_status = postgresql.ENUM(
        "open", "accepted", "dismissed", name="suggestion_status"
    )
    suggestion_status.create(op.get_bind(), checkfirst=True)

    agent_name = postgresql.ENUM("strategic", "tactical", name="agent_name")
    agent_name.create(op.get_bind(), checkfirst=True)

    agent_trigger = postgresql.ENUM(
        "finding", "task", "manual", "tick", name="agent_trigger"
    )
    agent_trigger.create(op.get_bind(), checkfirst=True)

    agent_exec_status = postgresql.ENUM(
        "running", "completed", "failed", name="agent_execution_status"
    )
    agent_exec_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engagement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(name="task_kind", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "owner_eligibility",
            postgresql.ENUM(name="task_owner_eligibility", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="task_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["engagement_id"], ["engagements.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"], ["findings.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_engagement_id", "tasks", ["engagement_id"])
    op.create_index("ix_tasks_finding_id", "tasks", ["finding_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])

    op.create_table(
        "suggestions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engagement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "kind",
            postgresql.ENUM(name="suggestion_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "status",
            postgresql.ENUM(name="suggestion_status", create_type=False),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "created_by_agent",
            postgresql.ENUM(name="agent_name", create_type=False),
            nullable=False,
        ),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["engagement_id"], ["engagements.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"], ["findings.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_suggestions_engagement_id", "suggestions", ["engagement_id"]
    )
    op.create_index("ix_suggestions_finding_id", "suggestions", ["finding_id"])
    op.create_index("ix_suggestions_status", "suggestions", ["status"])

    op.create_table(
        "agent_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engagement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "agent",
            postgresql.ENUM(name="agent_name", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "trigger",
            postgresql.ENUM(name="agent_trigger", create_type=False),
            nullable=False,
        ),
        sa.Column("input", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("output", postgresql.JSONB, nullable=True),
        sa.Column("model_provider", sa.String(40), nullable=True),
        sa.Column("model_name", sa.String(120), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="agent_execution_status", create_type=False),
            nullable=False,
            server_default="running",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["engagement_id"], ["engagements.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_executions_engagement_id",
        "agent_executions",
        ["engagement_id"],
    )
    op.create_index(
        "ix_agent_executions_agent", "agent_executions", ["agent"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_executions_agent", table_name="agent_executions"
    )
    op.drop_index(
        "ix_agent_executions_engagement_id", table_name="agent_executions"
    )
    op.drop_table("agent_executions")

    op.drop_index("ix_suggestions_status", table_name="suggestions")
    op.drop_index("ix_suggestions_finding_id", table_name="suggestions")
    op.drop_index("ix_suggestions_engagement_id", table_name="suggestions")
    op.drop_table("suggestions")

    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_finding_id", table_name="tasks")
    op.drop_index("ix_tasks_engagement_id", table_name="tasks")
    op.drop_table("tasks")

    for enum_name in (
        "agent_execution_status",
        "agent_trigger",
        "agent_name",
        "suggestion_status",
        "suggestion_kind",
        "task_status",
        "task_owner_eligibility",
        "task_kind",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
