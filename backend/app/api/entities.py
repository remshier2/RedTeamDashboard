"""Stored entities — Phase 10 Maltego import + retrieval.

Two endpoints in v1:

    POST   /engagements/{slug}/entities/import/maltego  -> upload .mtgx
    GET    /engagements/{slug}/entities/stored          -> list stored

The existing ``GET /engagements/{slug}/entities`` (in
``engagements.py``) derives entities on the fly from ``Finding.target``
+ ``Finding.details``. This module owns the *stored* layer that
external imports (Maltego, future Dehashed) feed.

The Entities tab in the UI shows both: an "Imported" section sourced
from the stored table, and a "Derived from findings" section sourced
from the existing endpoint.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models import ActorType, AuditLog, Engagement, Entity
from app.services import entity_store
from app.services.maltego_import import parse_mtgx

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class StoredEntityRead(BaseModel):
    id: uuid.UUID
    type: str
    value: str
    properties: dict[str, Any] = Field(default_factory=dict)
    source_tool: str
    source_attribution: str | None = None
    created_at: Any
    updated_at: Any


class MaltegoImportResult(BaseModel):
    """Response shape for the .mtgx upload endpoint."""

    inserted: int
    merged: int
    skipped_empty: int
    skipped_unknown: int
    total_nodes: int
    entities: list[StoredEntityRead]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engagement_by_slug(session, slug: str) -> Engagement:
    eng = session.execute(
        select(Engagement).where(Engagement.slug == slug)
    ).scalar_one_or_none()
    if eng is None:
        raise HTTPException(
            status_code=404, detail=f"engagement '{slug}' not found"
        )
    return eng


def _entity_to_read(e: Entity) -> StoredEntityRead:
    return StoredEntityRead(
        id=e.id,
        type=e.type,
        value=e.value,
        properties=dict(e.properties or {}),
        source_tool=e.source_tool,
        source_attribution=e.source_attribution,
        created_at=e.created_at,
        updated_at=e.updated_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/engagements/{slug}/entities/import/maltego",
    response_model=MaltegoImportResult,
    status_code=status.HTTP_201_CREATED,
)
def import_maltego(
    slug: str,
    session: DbSession,
    user: CurrentUser,
    file: Annotated[UploadFile, File(..., description="Maltego .mtgx export.")],
) -> MaltegoImportResult:
    """Import a Maltego ``.mtgx`` graph export into the stored entities table.

    Each ``MaltegoEntity`` becomes an ``Entity`` with ``source_tool="maltego_import"``
    and ``source_attribution=<filename>``. Re-imports merge into existing
    rows via UPSERT on ``(engagement_id, type, value)`` — properties
    JSONB is concatenated so prior keys not in the new payload are
    preserved.
    """
    eng = _engagement_by_slug(session, slug)
    raw = file.file.read()
    attribution = file.filename or "maltego.mtgx"
    try:
        result = parse_mtgx(raw, source_attribution=attribution)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    inserted, merged = entity_store.persist_entities(
        session,
        engagement=eng,
        items=result.items,
        source_tool="maltego_import",
        source_attribution=attribution,
    )

    session.add(
        AuditLog(
            engagement_id=eng.id,
            actor_type=ActorType.user,
            actor_id=str(user.id),
            event_type="entities.imported",
            payload={
                "source": "maltego_import",
                "filename": attribution,
                "inserted": inserted,
                "merged": merged,
                "skipped_empty": result.skipped_empty,
                "skipped_unknown": result.skipped_unknown,
                "total_nodes": result.total_nodes,
            },
        )
    )
    session.commit()

    # Return the freshly-persisted rows so the UI can render immediately.
    fresh = entity_store.list_stored_entities(session, engagement=eng)
    return MaltegoImportResult(
        inserted=inserted,
        merged=merged,
        skipped_empty=result.skipped_empty,
        skipped_unknown=result.skipped_unknown,
        total_nodes=result.total_nodes,
        entities=[_entity_to_read(e) for e in fresh],
    )


@router.get(
    "/engagements/{slug}/entities/stored",
    response_model=list[StoredEntityRead],
)
def list_stored_entities_endpoint(
    slug: str,
    session: DbSession,
    type: Annotated[str | None, Query(description="Filter by type.")] = None,
    q: Annotated[
        str | None, Query(description="Substring match on the value.")
    ] = None,
) -> list[StoredEntityRead]:
    """Stored entities for the engagement (Maltego imports + future sources).

    Distinct from ``GET /engagements/{slug}/entities`` which derives
    entities from findings on the fly. The UI Entities tab shows both
    layers as separate sections.
    """
    eng = _engagement_by_slug(session, slug)
    rows = entity_store.list_stored_entities(
        session, engagement=eng, type_filter=type, query=q
    )
    return [_entity_to_read(e) for e in rows]
