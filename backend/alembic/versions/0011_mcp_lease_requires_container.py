"""mcp_leases.requires_container — opt into ephemeral MCP host per task.

Stage 2 of per-task MCP composition. When True, Tactical provisions an
Azure Container Apps Job (the standalone MCP) for that run; when False
(the default), the colocated MCP server handles the lease. Strategic
decides at mint time.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-24
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_leases",
        sa.Column(
            "requires_container",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("mcp_leases", "requires_container")
