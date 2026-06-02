"""Engagements + nested scope + runs HTTP surface.

Engagements are addressed in URLs by their ``slug`` (human-set, non-sequential)
rather than the UUIDv7 primary key — the UUIDs still appear in JSON responses
(``id`` field) and as FKs internally, just never in paths.

Endpoints::

    POST   /engagements                                 -> create
    GET    /engagements                                 -> list (?status filter)
    GET    /engagements/{slug}                          -> read
    PATCH  /engagements/{slug}                          -> rename / archive / unarchive
    DELETE /engagements/{slug}                          -> soft archive
    POST   /engagements/{slug}/flush                    -> irreversible (calls flush_engagement)

    POST   /engagements/{slug}/scope                    -> create scope item
    GET    /engagements/{slug}/scope                    -> list scope items
    PATCH  /engagements/{slug}/scope/{scope_id}         -> update
    DELETE /engagements/{slug}/scope/{scope_id}         -> remove

    GET    /engagements/{slug}/findings                 -> list persisted findings

    POST   /engagements/{slug}/runs                     -> enqueue run.start

DELETE soft-archives the engagement (worker stops considering it for new runs
once status != active); /flush is the destructive operation, gated to a
separate endpoint so it can't fire from a stray HTTP verb.
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select, text

from app.api.deps import CurrentUser, DbSession, RedisClient
from app.core.config import settings
from app.models import ActorType, AuditLog, Engagement, EngagementStatus, Finding, ScopeItem
from app.orchestrator.llm import default_provider_model
from app.runs.events import encode_command
from app.runs.streams import inbound_stream, outbound_stream, store_run_model
from app.schemas.engagement import (
    EngagementCreate,
    EngagementRead,
    EngagementUpdate,
    RunModel,
    RunStart,
    RunStartResponse,
    ScopeItemCreate,
    ScopeItemRead,
    ScopeItemUpdate,
)
from app.schemas.finding import FindingRead

router = APIRouter()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    cleaned = _SLUG_RE.sub("-", name.lower()).strip("-")
    return cleaned or "engagement"


def _unique_slug(session: DbSession, base: str) -> str:
    candidate = base
    while session.execute(
        select(Engagement.id).where(Engagement.slug == candidate)
    ).first():
        candidate = f"{base}-{uuid.uuid4().hex[:6]}"
    return candidate


def _get_engagement_or_404(session: DbSession, slug: str) -> Engagement:
    eng = session.execute(
        select(Engagement).where(Engagement.slug == slug)
    ).scalar_one_or_none()
    if eng is None:
        raise HTTPException(status_code=404, detail="engagement not found")
    return eng


def _reject_flushed(eng: Engagement) -> None:
    if eng.status is EngagementStatus.flushed:
        raise HTTPException(
            status_code=409,
            detail="engagement has been flushed; the row will be gone shortly",
        )


def _finding_to_read(f: Finding) -> dict[str, Any]:
    """Unpack a persisted Finding into the same shape the SSE
    ``finding.created`` event carries.

    The worker stores ``details = {"thread_id": ..., "args": ..., **tool_data}``
    (see ``RunRunner._persist_finding``); we pop the envelope keys back out so
    the remainder is the raw tool data, letting the UI render hydrated and live
    findings through one code path.
    """
    details = dict(f.details or {})
    thread_id = details.pop("thread_id", None)
    args = details.pop("args", {})
    return {
        "id": f.id,
        "thread_id": str(thread_id) if thread_id is not None else None,
        "tool": f.source_tool,
        "target": f.target,
        "args": args if isinstance(args, dict) else {},
        "data": details,
        "severity": f.severity,
        "title": f.title,
        "created_at": f.created_at,
    }


# ---------------------------------------------------------------------------
# Engagement CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/engagements",
    response_model=EngagementRead,
    status_code=status.HTTP_201_CREATED,
)
def create_engagement(
    body: EngagementCreate,
    session: DbSession,
    user: CurrentUser,
) -> Engagement:
    base_slug = _slugify(body.slug) if body.slug else _slugify(body.name)
    slug = _unique_slug(session, base_slug)
    eng = Engagement(
        name=body.name,
        slug=slug,
        status=EngagementStatus.active,
        created_by=user.id,
    )
    session.add(eng)
    session.commit()
    session.refresh(eng)
    return eng


@router.get("/engagements", response_model=list[EngagementRead])
def list_engagements(
    session: DbSession,
    status_filter: Annotated[
        EngagementStatus | None,
        Query(alias="status", description="Filter by status."),
    ] = None,
) -> list[Engagement]:
    stmt = select(Engagement)
    if status_filter is not None:
        stmt = stmt.where(Engagement.status == status_filter)
    stmt = stmt.order_by(Engagement.created_at.desc())
    return list(session.execute(stmt).scalars())


@router.get("/engagements/{slug}", response_model=EngagementRead)
def get_engagement(slug: str, session: DbSession) -> Engagement:
    return _get_engagement_or_404(session, slug)


@router.patch("/engagements/{slug}", response_model=EngagementRead)
def update_engagement(
    slug: str,
    body: EngagementUpdate,
    session: DbSession,
) -> Engagement:
    eng = _get_engagement_or_404(session, slug)
    _reject_flushed(eng)

    if body.name is not None:
        eng.name = body.name

    if body.status is not None:
        if body.status is EngagementStatus.flushed:
            raise HTTPException(
                status_code=400,
                detail="use POST /engagements/{slug}/flush to flush",
            )
        if body.status is EngagementStatus.active and eng.status is EngagementStatus.archived:
            eng.archived_at = None
        elif (
            body.status is EngagementStatus.archived
            and eng.status is EngagementStatus.active
        ):
            eng.archived_at = datetime.now(tz=UTC)
        eng.status = body.status

    session.commit()
    session.refresh(eng)
    return eng


@router.delete("/engagements/{slug}", response_model=EngagementRead)
def archive_engagement(slug: str, session: DbSession) -> Engagement:
    eng = _get_engagement_or_404(session, slug)
    _reject_flushed(eng)
    if eng.status is not EngagementStatus.archived:
        eng.status = EngagementStatus.archived
        eng.archived_at = datetime.now(tz=UTC)
    session.commit()
    session.refresh(eng)
    return eng


@router.post(
    "/engagements/{slug}/flush",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def flush_engagement(
    slug: str,
    session: DbSession,
    redis_client: RedisClient,
) -> Response:
    eng = _get_engagement_or_404(session, slug)
    eid = eng.id

    # The DB-side flush_engagement() handles audit_log + engagements (with
    # cascades to scope_items, findings, approvals). Streams aren't FKs, so we
    # explicitly drop them here.
    session.execute(text("SELECT flush_engagement(:id)"), {"id": eid})
    session.commit()
    redis_client.delete(inbound_stream(eid), outbound_stream(eid))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Scope CRUD (nested under engagement)
# ---------------------------------------------------------------------------


@router.post(
    "/engagements/{slug}/scope",
    response_model=ScopeItemRead,
    status_code=status.HTTP_201_CREATED,
)
def create_scope_item(
    slug: str,
    body: ScopeItemCreate,
    session: DbSession,
) -> ScopeItem:
    eng = _get_engagement_or_404(session, slug)
    _reject_flushed(eng)
    item = ScopeItem(
        engagement_id=eng.id,
        kind=body.kind,
        value=body.value,
        is_exclusion=body.is_exclusion,
        note=body.note,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.get(
    "/engagements/{slug}/scope",
    response_model=list[ScopeItemRead],
)
def list_scope(slug: str, session: DbSession) -> list[ScopeItem]:
    eng = _get_engagement_or_404(session, slug)
    rows = session.execute(
        select(ScopeItem)
        .where(ScopeItem.engagement_id == eng.id)
        .order_by(ScopeItem.created_at)
    ).scalars()
    return list(rows)


@router.patch(
    "/engagements/{slug}/scope/{scope_id}",
    response_model=ScopeItemRead,
)
def update_scope_item(
    slug: str,
    scope_id: uuid.UUID,
    body: ScopeItemUpdate,
    session: DbSession,
) -> ScopeItem:
    eng = _get_engagement_or_404(session, slug)
    _reject_flushed(eng)
    item = session.get(ScopeItem, scope_id)
    if item is None or item.engagement_id != eng.id:
        raise HTTPException(status_code=404, detail="scope item not found")
    if body.value is not None:
        item.value = body.value
    if body.is_exclusion is not None:
        item.is_exclusion = body.is_exclusion
    if body.note is not None:
        item.note = body.note
    session.commit()
    session.refresh(item)
    return item


@router.delete(
    "/engagements/{slug}/scope/{scope_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_scope_item(
    slug: str,
    scope_id: uuid.UUID,
    session: DbSession,
) -> Response:
    eng = _get_engagement_or_404(session, slug)
    item = session.get(ScopeItem, scope_id)
    if item is None or item.engagement_id != eng.id:
        raise HTTPException(status_code=404, detail="scope item not found")
    session.delete(item)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Findings (read-only; written by the worker)
# ---------------------------------------------------------------------------


@router.get(
    "/engagements/{slug}/findings",
    response_model=list[FindingRead],
)
def list_findings(slug: str, session: DbSession) -> list[dict[str, Any]]:
    eng = _get_engagement_or_404(session, slug)
    rows = session.execute(
        select(Finding)
        .where(Finding.engagement_id == eng.id)
        .order_by(Finding.created_at.desc())
    ).scalars()
    return [_finding_to_read(f) for f in rows]


# ---------------------------------------------------------------------------
# Runs (enqueue run.start to the inbound stream)
# ---------------------------------------------------------------------------


def _check_provider_key_available(provider: str) -> None:
    """Raise 400 if the provider's credentials aren't set.

    Container Apps populates env vars from Key Vault refs; if the operator
    hasn't filled in the LLM key yet, the secret still reads as the
    ``PLACEHOLDER-set-after-deploy`` string. Treat that as missing too.
    """
    def _looks_placeholder(value: str) -> bool:
        return not value or value.startswith("PLACEHOLDER")

    if provider == "anthropic":
        if _looks_placeholder(settings.anthropic_api_key):
            raise HTTPException(
                status_code=400,
                detail="ANTHROPIC_API_KEY not configured for this deployment.",
            )
    elif provider == "openai":
        if _looks_placeholder(settings.openai_api_key):
            raise HTTPException(
                status_code=400,
                detail="OPENAI_API_KEY not configured for this deployment.",
            )
    elif provider == "azure" and (
        _looks_placeholder(settings.azure_openai_api_key)
        or _looks_placeholder(settings.azure_openai_endpoint)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT not configured "
                "for this deployment."
            ),
        )
    # ollama is local — no key precheck.


@router.post(
    "/engagements/{slug}/runs",
    response_model=RunStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_run(
    slug: str,
    body: RunStart,
    session: DbSession,
    redis_client: RedisClient,
    user: CurrentUser,
) -> RunStartResponse:
    eng = _get_engagement_or_404(session, slug)
    if eng.status is not EngagementStatus.active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"engagement is {eng.status.value}; only active engagements "
                "accept new runs"
            ),
        )

    # Resolve effective model: body wins, else fall back to env defaults.
    if body.model is not None:
        provider, model_name = body.model.provider, body.model.name
    else:
        provider, model_name = default_provider_model()
    _check_provider_key_available(provider)
    effective_model = RunModel(provider=provider, name=model_name)

    thread_id = uuid.uuid4()
    # Stash the (provider, model) so the approval endpoint can echo it on
    # the resume envelope without redoing the resolution dance.
    store_run_model(
        redis_client,
        thread_id,
        provider=effective_model.provider,
        model_name=effective_model.name,
    )

    redis_client.xadd(
        inbound_stream(eng.id),
        encode_command(
            {
                "type": "run.start",
                "thread_id": str(thread_id),
                "prompt": body.prompt,
                "model": {
                    "provider": effective_model.provider,
                    "name": effective_model.name,
                },
            }
        ),
    )

    session.add(
        AuditLog(
            engagement_id=eng.id,
            actor_type=ActorType.user,
            actor_id=str(user.id),
            event_type="run.requested",
            payload={
                "thread_id": str(thread_id),
                "prompt_len": len(body.prompt),
                "model": {
                    "provider": effective_model.provider,
                    "name": effective_model.name,
                },
            },
        )
    )
    session.commit()

    return RunStartResponse(
        engagement_id=eng.id,
        thread_id=thread_id,
        events_stream=outbound_stream(eng.id),
        model=effective_model,
    )
