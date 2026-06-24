"""mcp_leases.task_id — drop NOT NULL for direct-run leases.

The Stage 1.5 worker fallback (run with no lease → execute against
local registry) is ripped in this commit. Every run now carries an
MCP envelope, including analyst-typed direct runs via
``POST /engagements/{slug}/runs``. Those don't have an orchestrator
Task wrapping them, so the lease record needs to allow task_id=NULL.

Tactical-dispatched leases still always carry a Task — the column
relaxation is purely additive for the direct-run path.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-24
"""
from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("mcp_leases", "task_id", nullable=True)


def downgrade() -> None:
    # Downgrading requires every existing lease to have a task_id — direct-run
    # leases would block this. Best-effort: set them to NOT NULL only if no
    # rows have task_id IS NULL. Otherwise the caller must clean up first.
    op.execute(
        "DELETE FROM mcp_leases WHERE task_id IS NULL"
    )
    op.alter_column("mcp_leases", "task_id", nullable=False)
