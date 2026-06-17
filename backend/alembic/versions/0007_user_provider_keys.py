"""user_provider_keys table — BYO model + MCP credentials per analyst

Each analyst owns a set of "provider keys" they uploaded (Anthropic, OpenAI,
Ollama, custom MCP servers, etc.). The encrypted key blob is the Fernet
ciphertext; ``key_last4`` is the last 4 plaintext chars for UI display so we
never have to round-trip through the master key just to render a list.

Encryption is the operator's responsibility — they ship ``PROVIDER_KEY_MASTER``
via env (dev) or KV secret ``provider-key-master`` (deployed). Losing the
master key effectively bricks all stored keys; that's intentional.

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
    provider_key_kind = postgresql.ENUM(
        "model_provider", "mcp_server", name="provider_key_kind"
    )
    provider_key_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "user_provider_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(name="provider_key_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(60), nullable=False),
        sa.Column(
            "is_local", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "models",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
        ),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("encrypted_key", sa.Text(), nullable=True),
        sa.Column("key_last4", sa.String(8), nullable=True),
        sa.Column(
            "extra",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_user_provider_keys_user_name"),
    )
    op.create_index(
        "ix_user_provider_keys_user_id", "user_provider_keys", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_provider_keys_user_id", table_name="user_provider_keys"
    )
    op.drop_table("user_provider_keys")
    op.execute("DROP TYPE IF EXISTS provider_key_kind")
