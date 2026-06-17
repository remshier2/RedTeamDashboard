from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import Boolean, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid7


class ProviderKeyKind(enum.StrEnum):
    """What sort of remote the key authenticates against. ``model_provider``
    holds LLM API keys; ``mcp_server`` holds keys for third-party MCP servers
    the analyst connects to (GitHub, web search, etc.)."""

    model_provider = "model_provider"
    mcp_server = "mcp_server"


class UserProviderKey(Base, TimestampMixin):
    """One BYO credential entry uploaded by an analyst.

    The plaintext key never lives in this table — only the Fernet ciphertext
    (``encrypted_key``) and a 4-char tail (``key_last4``) we can show in the
    UI without decrypting. Local providers (Ollama on the analyst's box,
    self-hosted Hugging Face) carry no key, just an ``endpoint``.
    """

    __tablename__ = "user_provider_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_provider_keys_user_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[ProviderKeyKind] = mapped_column(
        Enum(ProviderKeyKind, name="provider_key_kind"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    is_local: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    models: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_last4: Mapped[str | None] = mapped_column(String(8), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
