"""Stored-entities persistence — Phase 10.

UPSERT helper for the ``entities`` table. Duck-typed against any item
shape that exposes ``type`` + ``value`` + ``properties``, so the
Maltego parser (``maltego_import.ParsedEntity``) and any future
importer (Dehashed JSON, etc.) feed the same persistence path.

Why UPSERT instead of bulk INSERT: analysts re-export Maltego graphs
as they add transforms. The natural identity is ``(engagement_id,
type, value)``; on a re-import we want to merge new property data into
the existing row rather than create duplicates. Postgres ``ON CONFLICT
... DO UPDATE`` with a JSONB concatenation (``properties || EXCLUDED``)
gives us merge semantics in one statement per row.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Engagement, Entity

logger = structlog.get_logger(__name__)


def persist_entities(
    session: Session,
    *,
    engagement: Engagement,
    items: list[Any],
    source_tool: str,
    source_attribution: str | None = None,
) -> tuple[int, int]:
    """UPSERT a list of parsed entities, merging properties on conflict.

    Items are duck-typed: each must expose ``type``, ``value``,
    ``properties``. Returns ``(inserted_count, merged_count)``. Caller
    commits.

    Merge semantics: on ``(engagement_id, type, value)`` conflict we
    concatenate the JSONB properties (``existing || incoming``), so
    later imports override matching keys but preserve prior ones. The
    ``updated_at`` column gets bumped too.
    """
    if not items:
        return 0, 0

    inserted = 0
    merged = 0
    now = datetime.now(tz=UTC)

    for item in items:
        type_value = str(item.type)
        value_str = str(item.value)
        props = dict(getattr(item, "properties", {}) or {})

        existing_id = session.execute(
            select(Entity.id).where(
                Entity.engagement_id == engagement.id,
                Entity.type == type_value,
                Entity.value == value_str,
            )
        ).scalar_one_or_none()

        stmt = pg_insert(Entity).values(
            engagement_id=engagement.id,
            type=type_value,
            value=value_str,
            properties=props,
            source_tool=source_tool,
            source_attribution=source_attribution,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_entities_engagement_type_value",
            set_={
                # JSONB concat — incoming keys override existing on collision,
                # but prior keys not in the new payload are preserved.
                "properties": Entity.properties.op("||")(stmt.excluded.properties),
                "source_tool": stmt.excluded.source_tool,
                "source_attribution": stmt.excluded.source_attribution,
                "updated_at": now,
            },
        )
        session.execute(stmt)

        if existing_id is None:
            inserted += 1
        else:
            merged += 1

    session.flush()
    logger.info(
        "entity_store.persisted",
        engagement_id=str(engagement.id),
        source_tool=source_tool,
        inserted=inserted,
        merged=merged,
    )
    return inserted, merged


def list_stored_entities(
    session: Session,
    *,
    engagement: Engagement,
    type_filter: str | None = None,
    query: str | None = None,
) -> list[Entity]:
    """Read-side query — stored entities for the engagement, optionally
    filtered by ``type`` (exact) and ``query`` (case-insensitive substring
    on ``value``). Ordered newest first."""
    stmt = select(Entity).where(Entity.engagement_id == engagement.id)
    if type_filter:
        stmt = stmt.where(Entity.type == type_filter)
    if query:
        stmt = stmt.where(Entity.value.ilike(f"%{query}%"))
    stmt = stmt.order_by(Entity.created_at.desc())
    return list(session.execute(stmt).scalars())
