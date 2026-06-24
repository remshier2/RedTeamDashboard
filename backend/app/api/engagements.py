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

    GET    /engagements/{slug}/observations              -> list observations
    POST   /engagements/{slug}/observations              -> create observation
    DELETE /observations/{observation_id}                -> delete observation

    POST   /engagements/{slug}/findings/import           -> bulk import findings (JSON/CSV)
    POST   /engagements/{slug}/findings/import/nessus    -> import Nessus .nessus v2 XML
    PATCH  /findings/{finding_id}                        -> update title/summary/severity/phase
    GET    /engagements/{slug}/export                    -> full JSON snapshot

    POST   /findings/{finding_id}/attachments            -> upload screenshot/evidence file
    GET    /findings/{finding_id}/attachments            -> list attachment metadata
    GET    /attachments/{attachment_id}                  -> serve raw bytes
    DELETE /attachments/{attachment_id}                  -> delete

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

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select, text

from app.api.deps import CurrentUser, DbSession, RedisClient, RequireScope
from app.core.blob import upload_engagement_export
from app.models import (
    ActorType,
    Attachment,
    AuditLog,
    Engagement,
    EngagementStatus,
    Finding,
    FindingPhase,
    FindingStatus,
    Observation,
    ScopeItem,
    Severity,
    TaskKind,
)
from app.models.api_key import APIKeyScope
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
    ScopeImportPreview,
    ScopeImportRequest,
    ScopeImportResult,
    ScopeItemCreate,
    ScopeItemRead,
    ScopeItemUpdate,
)
from app.schemas.finding import (
    AttachmentRead,
    EntityRead,
    FindingRead,
    FindingUpdate,
    FindingValidate,
)
from app.schemas.observation import ObservationCreate, ObservationRead
from app.services.entities import extract_entities
from app.services.scope_import import parse_scope_text

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


def _build_export_payload(session: DbSession, eng: Engagement) -> dict[str, Any]:
    """Assemble a complete engagement snapshot suitable for blob archiving."""
    scope_items = list(
        session.execute(select(ScopeItem).where(ScopeItem.engagement_id == eng.id)).scalars()
    )
    findings = list(
        session.execute(select(Finding).where(Finding.engagement_id == eng.id)).scalars()
    )
    audit_rows = list(
        session.execute(
            select(AuditLog)
            .where(AuditLog.engagement_id == eng.id)
            .order_by(AuditLog.created_at)
        ).scalars()
    )
    audit_summary: dict[str, Any] = {"count": len(audit_rows)}
    if audit_rows:
        audit_summary["first"] = str(audit_rows[0].created_at)
        audit_summary["last"] = str(audit_rows[-1].created_at)

    observations = list(
        session.execute(
            select(Observation)
            .where(Observation.engagement_id == eng.id)
            .order_by(Observation.created_at)
        ).scalars()
    )

    return {
        "version": "1",
        "exported_at": str(datetime.now(tz=UTC)),
        "engagement": {
            "id": str(eng.id),
            "slug": eng.slug,
            "name": eng.name,
            "status": eng.status,
            "description": eng.description,
            "created_at": str(eng.created_at),
            "archived_at": str(eng.archived_at) if eng.archived_at else None,
        },
        "scope": [
            {"kind": s.kind, "value": s.value, "is_exclusion": s.is_exclusion, "note": s.note}
            for s in scope_items
        ],
        "findings": [
            {
                "id": str(f.id),
                "title": f.title,
                "severity": f.severity,
                "status": f.status,
                "target": f.target,
                "source_tool": f.source_tool,
                "phase": f.phase,
                "summary": f.summary,
                "details": f.details,
                "created_at": str(f.created_at),
            }
            for f in findings
        ],
        "observations": [
            {
                "content": o.content,
                "phase": o.phase,
                "created_at": str(o.created_at),
            }
            for o in observations
        ],
        "audit_summary": audit_summary,
    }


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
        "summary": f.summary,
        "phase": f.phase,
        "status": f.status,
        "validated_at": f.validated_at,
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
        description=body.description,
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


@router.post("/engagements/{slug}/export", dependencies=[Depends(RequireScope(APIKeyScope.admin))])
def export_engagement(slug: str, session: DbSession) -> dict[str, Any]:
    """Export all engagement data (findings, scope, audit summary) to blob storage.

    Returns the blob URL if storage is configured, or the full payload inline
    if AZURE_STORAGE_ACCOUNT_NAME is unset (useful for local dev / manual backup).
    Requires admin scope.
    """
    eng = _get_engagement_or_404(session, slug)
    payload = _build_export_payload(session, eng)
    blob_url = upload_engagement_export(slug, payload)
    if blob_url:
        return {"slug": slug, "blob_url": blob_url}
    return {"slug": slug, "blob_url": None, "payload": payload}


@router.delete(
    "/engagements/{slug}",
    response_model=EngagementRead,
)
def archive_engagement(slug: str, session: DbSession, _user: CurrentUser) -> Engagement:
    eng = _get_engagement_or_404(session, slug)
    _reject_flushed(eng)
    if eng.status is not EngagementStatus.archived:
        eng.status = EngagementStatus.archived
        eng.archived_at = datetime.now(tz=UTC)
        session.commit()
        session.refresh(eng)
        # Export to blob; failure doesn't block the archive.
        upload_engagement_export(slug, _build_export_payload(session, eng))
    else:
        session.commit()
        session.refresh(eng)
    return eng


@router.post("/engagements/{slug}/flush", status_code=204)
def flush_engagement(
    slug: str,
    session: DbSession,
    redis_client: RedisClient,
    _user: CurrentUser,
) -> Response:
    """Permanently delete all engagement data. Export to blob first, then purge."""
    eng = _get_engagement_or_404(session, slug)
    eid = eng.id
    slug_val = eng.slug

    # Export before destroying — failure is logged but doesn't block the flush.
    payload = _build_export_payload(session, eng)
    upload_engagement_export(slug_val, payload)

    # The DB-side flush_engagement() handles audit_log + engagements (with
    # cascades to scope_items, findings, approvals). Streams aren't FKs, so we
    # explicitly drop them here.
    session.execute(text("SELECT flush_engagement(:id)"), {"id": eid})
    session.commit()
    redis_client.delete(inbound_stream(eid), outbound_stream(eid))
    return Response(status_code=204)


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


@router.post(
    "/scope/parse",
    response_model=ScopeImportPreview,
)
def parse_scope_blob(
    body: ScopeImportRequest, _user: CurrentUser
) -> ScopeImportPreview:
    """Pure parser — no engagement, no DB writes.

    Lets the /new wizard preview an import before the engagement exists.
    Same parser the /scope/import endpoint uses; results are interchangeable.
    """
    rows, errors = parse_scope_text(body.text)
    return ScopeImportPreview(
        preview=[
            {
                "line": r.line,
                "value": r.value,
                "kind": r.kind,
                "is_exclusion": r.is_exclusion,
            }
            for r in rows
        ],
        errors=[
            {"line": e.line, "raw": e.raw, "reason": e.reason} for e in errors
        ],
        would_create=len(rows),
    )


@router.post(
    "/engagements/{slug}/scope/import",
    response_model=ScopeImportPreview | ScopeImportResult,
)
def import_scope(
    slug: str,
    body: ScopeImportRequest,
    session: DbSession,
    user: CurrentUser,
    dry_run: bool = False,
) -> ScopeImportPreview | ScopeImportResult:
    """Bulk-import scope items from a free-form text blob.

    Same parser whether the analyst uploaded a file (client read it as text)
    or pasted into a textarea. ``?dry_run=true`` returns the preview without
    persisting; the UI calls it on each debounced keystroke. The real commit
    de-dupes against the engagement's existing (kind, value, is_exclusion)
    tuples so re-running an import is safe.
    """
    eng = _get_engagement_or_404(session, slug)
    _reject_flushed(eng)
    rows, errors = parse_scope_text(body.text)

    error_rows = [
        {"line": e.line, "raw": e.raw, "reason": e.reason} for e in errors
    ]

    if dry_run:
        return ScopeImportPreview(
            preview=[
                {
                    "line": r.line,
                    "value": r.value,
                    "kind": r.kind,
                    "is_exclusion": r.is_exclusion,
                }
                for r in rows
            ],
            errors=error_rows,
            would_create=len(rows),
        )

    existing = list(
        session.execute(
            select(ScopeItem).where(ScopeItem.engagement_id == eng.id)
        ).scalars()
    )
    seen = {(s.kind, s.value, s.is_exclusion) for s in existing}

    created: list[ScopeItem] = []
    duplicates: list[dict[str, Any]] = []
    for r in rows:
        key = (r.kind, r.value, r.is_exclusion)
        if key in seen:
            duplicates.append(
                {
                    "line": r.line,
                    "value": r.value,
                    "kind": r.kind,
                    "is_exclusion": r.is_exclusion,
                }
            )
            continue
        item = ScopeItem(
            engagement_id=eng.id,
            kind=r.kind,
            value=r.value,
            is_exclusion=r.is_exclusion,
        )
        session.add(item)
        seen.add(key)
        created.append(item)

    session.flush()
    if created:
        session.add(
            AuditLog(
                engagement_id=eng.id,
                actor_type=ActorType.user,
                actor_id=str(user.id),
                event_type="scope.imported",
                payload={
                    "created_count": len(created),
                    "error_count": len(errors),
                    "duplicate_count": len(duplicates),
                },
            )
        )
    session.commit()
    for c in created:
        session.refresh(c)

    return ScopeImportResult(
        created=[ScopeItemRead.model_validate(c) for c in created],
        errors=error_rows,
        duplicates=duplicates,
    )


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
def list_findings(
    slug: str,
    session: DbSession,
    phase: Annotated[FindingPhase | None, Query(description="Filter by phase.")] = None,
    status: Annotated[
        FindingStatus | None, Query(description="Filter by validation status.")
    ] = None,
) -> list[dict[str, Any]]:
    eng = _get_engagement_or_404(session, slug)
    stmt = select(Finding).where(Finding.engagement_id == eng.id)
    if phase is not None:
        stmt = stmt.where(Finding.phase == phase)
    if status is not None:
        stmt = stmt.where(Finding.status == status)
    rows = session.execute(stmt.order_by(Finding.created_at.desc())).scalars()
    return [_finding_to_read(f) for f in rows]


@router.get(
    "/engagements/{slug}/entities",
    response_model=list[EntityRead],
)
def list_entities(
    slug: str,
    session: DbSession,
    type: Annotated[str | None, Query(description="Filter by entity type.")] = None,
    q: Annotated[str | None, Query(description="Substring match on the value.")] = None,
) -> list[dict[str, Any]]:
    """Entities correlated across this engagement's findings (CHARTER Idea 4)."""
    eng = _get_engagement_or_404(session, slug)
    findings = list(
        session.execute(
            select(Finding)
            .where(Finding.engagement_id == eng.id)
            .order_by(Finding.created_at)
        ).scalars()
    )
    return extract_entities(findings, type_filter=type, query=q)


@router.post(
    "/findings/{finding_id}/validate",
    response_model=FindingRead,
)
def validate_finding(
    finding_id: uuid.UUID,
    body: FindingValidate,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """Promote/reject a pending finding. ``validated`` makes it report-eligible;
    ``rejected`` / ``false_positive`` keep it for audit but exclude it."""
    finding = session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")

    finding.status = body.decision
    if body.decision is FindingStatus.validated:
        finding.validated_by = user.id
        finding.validated_at = datetime.now(tz=UTC)
    else:
        # Re-deciding away from validated clears the validation stamp.
        finding.validated_by = None
        finding.validated_at = None

    session.add(
        AuditLog(
            engagement_id=finding.engagement_id,
            actor_type=ActorType.user,
            actor_id=str(user.id),
            event_type="finding.validated",
            payload={
                "finding_id": str(finding.id),
                "decision": body.decision.value,
                "reason": body.reason,
            },
        )
    )
    session.commit()
    session.refresh(finding)
    return _finding_to_read(finding)


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


@router.get("/engagements/{slug}/observations", response_model=list[ObservationRead])
def list_observations(slug: str, session: DbSession) -> list[Observation]:
    eng = _get_engagement_or_404(session, slug)
    return list(
        session.execute(
            select(Observation)
            .where(Observation.engagement_id == eng.id)
            .order_by(Observation.created_at)
        ).scalars()
    )


@router.post(
    "/engagements/{slug}/observations",
    response_model=ObservationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_observation(
    slug: str,
    body: ObservationCreate,
    session: DbSession,
    user: CurrentUser,
) -> Observation:
    eng = _get_engagement_or_404(session, slug)
    _reject_flushed(eng)
    obs = Observation(
        engagement_id=eng.id,
        content=body.content,
        phase=body.phase,
        created_by=user.id,
    )
    session.add(obs)
    session.commit()
    session.refresh(obs)
    return obs


@router.delete("/observations/{observation_id}", status_code=204)
def delete_observation(
    observation_id: uuid.UUID,
    session: DbSession,
    _user: CurrentUser,
) -> Response:
    obs = session.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status_code=404, detail="observation not found")
    session.delete(obs)
    session.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Findings import
# ---------------------------------------------------------------------------


class FindingImport(BaseModel):
    """Single finding in a bulk import payload."""

    title: str
    severity: Severity = Severity.info
    phase: FindingPhase = FindingPhase.general
    summary: str | None = None
    target: str | None = None
    source_tool: str | None = "import"
    details: dict[str, Any] = {}


class NessusImportResult(BaseModel):
    """Response shape for the .nessus XML importer.

    ``total_items`` is every ReportItem the parser saw; ``imported`` is
    the subset that survived the Info filter + scope filter and now has
    a Finding row. The ``skipped_*`` counters let the analyst sanity-
    check the filters dropped what they expected.
    """

    imported: list[FindingRead]
    skipped_info: int
    skipped_out_of_scope: int
    total_items: int


def _create_findings_from_imports(
    session: Any,
    eng: Engagement,
    items: list[Any],
    user: Any,
    *,
    source: str,
) -> list[Finding]:
    """Persist a list of import-shaped items as Findings + write the audit row.

    ``items`` is duck-typed: each must expose ``title``, ``severity``,
    ``phase``, ``summary``, ``target``, ``source_tool``, ``details``.
    Both ``FindingImport`` (Phase 11 JSON/CSV importer) and
    ``nessus_import.ParsedItem`` (Phase 10 .nessus parser) satisfy this.

    All imports land ``status=pending_validation`` per the Phase 8
    validation gate — analyst must approve before they're report-eligible.
    Caller commits the session.
    """
    created: list[Finding] = []
    for item in items:
        f = Finding(
            engagement_id=eng.id,
            title=item.title,
            severity=item.severity,
            phase=item.phase,
            summary=item.summary,
            target=item.target,
            source_tool=item.source_tool or "import",
            details=item.details,
            status=FindingStatus.pending_validation,
        )
        session.add(f)
        created.append(f)
    if created:
        session.add(
            AuditLog(
                engagement_id=eng.id,
                actor_type=ActorType.user,
                actor_id=str(user.id),
                event_type="findings.imported",
                payload={"count": len(created), "source": source},
            )
        )
    return created


@router.post(
    "/engagements/{slug}/findings/import",
    response_model=list[FindingRead],
    status_code=status.HTTP_201_CREATED,
)
def import_findings(
    slug: str,
    body: list[FindingImport],
    session: DbSession,
    user: CurrentUser,
) -> list[dict[str, Any]]:
    """Bulk-import findings from an external source (scanner output, prior report, etc.).

    All imported findings land as ``pending_validation`` so the analyst can
    review before they become report-eligible. ``source_tool`` defaults to
    ``'import'`` if omitted.
    """
    if not body:
        return []

    eng = _get_engagement_or_404(session, slug)
    _reject_flushed(eng)

    created = _create_findings_from_imports(
        session, eng, body, user, source="bulk_import"
    )
    session.commit()
    for f in created:
        session.refresh(f)
    return [_finding_to_read(f) for f in created]


@router.post(
    "/engagements/{slug}/findings/import/nessus",
    response_model=NessusImportResult,
    status_code=status.HTTP_201_CREATED,
)
def import_nessus(
    slug: str,
    session: DbSession,
    user: CurrentUser,
    file: Annotated[UploadFile, File(..., description="Nessus .nessus v2 XML export.")],
    include_info: Annotated[
        bool,
        Query(description="Import Severity=Info findings. Default False."),
    ] = False,
) -> dict[str, Any]:
    """Import a Tenable Nessus .nessus v2 XML export.

    Each ReportItem becomes a Finding with ``phase=vuln_scan`` and
    ``status=pending_validation`` (analyst must approve before report).
    ``include_info=true`` opts in to Severity=Info rows; default off.
    Out-of-scope hosts are dropped silently and counted on the response.
    """
    from app.services.nessus_import import parse_nessus_xml

    eng = _get_engagement_or_404(session, slug)
    _reject_flushed(eng)

    raw = file.file.read()
    scope_items = list(
        session.execute(
            select(ScopeItem).where(ScopeItem.engagement_id == eng.id)
        ).scalars()
    )
    try:
        result = parse_nessus_xml(
            raw, include_info=include_info, scope_items=scope_items
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    created = _create_findings_from_imports(
        session, eng, result.items, user, source="nessus_import"
    )
    session.commit()
    for f in created:
        session.refresh(f)

    return {
        "imported": [_finding_to_read(f) for f in created],
        "skipped_info": result.skipped_info,
        "skipped_out_of_scope": result.skipped_out_of_scope,
        "total_items": result.total_items,
    }


# ---------------------------------------------------------------------------
# Finding update (title / summary / severity / phase)
# ---------------------------------------------------------------------------


@router.patch(
    "/findings/{finding_id}",
    response_model=FindingRead,
)
def update_finding(
    finding_id: uuid.UUID,
    body: FindingUpdate,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """Edit analyst-controlled fields on a finding. Only provided fields change;
    omitted fields are left as-is. ``summary`` accepts ``null`` to clear it."""
    finding = session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")

    changed: dict[str, Any] = {}
    if "title" in body.model_fields_set and body.title is not None:
        finding.title = body.title
        changed["title"] = body.title
    if "summary" in body.model_fields_set:
        finding.summary = body.summary
        changed["summary"] = body.summary
    if "severity" in body.model_fields_set and body.severity is not None:
        finding.severity = body.severity
        changed["severity"] = body.severity.value
    if "phase" in body.model_fields_set and body.phase is not None:
        finding.phase = body.phase
        changed["phase"] = body.phase.value

    if changed:
        session.add(
            AuditLog(
                engagement_id=finding.engagement_id,
                actor_type=ActorType.user,
                actor_id=str(user.id),
                event_type="finding.updated",
                payload={"finding_id": str(finding.id), "changes": changed},
            )
        )
        session.commit()
        session.refresh(finding)

    return _finding_to_read(finding)


# ---------------------------------------------------------------------------
# Engagement JSON export
# ---------------------------------------------------------------------------


@router.get("/engagements/{slug}/export")
def get_engagement_export(
    slug: str,
    session: DbSession,
    _user: CurrentUser,
) -> dict[str, Any]:
    """Full engagement snapshot as structured JSON — findings, scope, observations,
    and audit summary. Suitable for archiving or importing into another instance."""
    eng = _get_engagement_or_404(session, slug)
    return _build_export_payload(session, eng)


# ---------------------------------------------------------------------------
# Finding attachments (screenshots / evidence files)
# ---------------------------------------------------------------------------

_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post(
    "/findings/{finding_id}/attachments",
    response_model=AttachmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    finding_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    session: DbSession,
    user: CurrentUser,
) -> Attachment:
    """Upload a screenshot or evidence file and attach it to the finding."""
    finding = session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")

    data = await file.read()
    if len(data) > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large — max {_MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB",
        )

    attachment = Attachment(
        finding_id=finding_id,
        engagement_id=finding.engagement_id,
        filename=file.filename or "attachment",
        content_type=file.content_type or "application/octet-stream",
        size_bytes=len(data),
        data=data,
        created_by=str(user.id),
    )
    session.add(attachment)
    session.add(
        AuditLog(
            engagement_id=finding.engagement_id,
            actor_type=ActorType.user,
            actor_id=str(user.id),
            event_type="attachment.uploaded",
            payload={
                "finding_id": str(finding_id),
                "filename": attachment.filename,
                "size_bytes": attachment.size_bytes,
            },
        )
    )
    session.commit()
    session.refresh(attachment)
    return attachment


@router.get(
    "/findings/{finding_id}/attachments",
    response_model=list[AttachmentRead],
)
def list_attachments(
    finding_id: uuid.UUID,
    session: DbSession,
    _user: CurrentUser,
) -> list[Attachment]:
    """List attachment metadata for a finding (no raw bytes — fetch individually)."""
    finding = session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")
    return list(
        session.execute(
            select(Attachment)
            .where(Attachment.finding_id == finding_id)
            .order_by(Attachment.created_at)
        ).scalars()
    )


@router.get("/attachments/{attachment_id}")
def serve_attachment(
    attachment_id: uuid.UUID,
    session: DbSession,
    _user: CurrentUser,
) -> Response:
    """Serve the raw bytes of an attachment with its original content-type."""
    attachment = session.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    return Response(
        content=attachment.data,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{attachment.filename}"',
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.delete("/attachments/{attachment_id}")
def delete_attachment(
    attachment_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> Response:
    """Delete an attachment."""
    attachment = session.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    session.add(
        AuditLog(
            engagement_id=attachment.engagement_id,
            actor_type=ActorType.user,
            actor_id=str(user.id),
            event_type="attachment.deleted",
            payload={
                "attachment_id": str(attachment_id),
                "filename": attachment.filename,
            },
        )
    )
    session.delete(attachment)
    session.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Runs (enqueue run.start to the inbound stream)
# ---------------------------------------------------------------------------


def _require_user_provider_key(
    session: DbSession, *, user_id: uuid.UUID, provider: str
) -> None:
    """Raise 400 if the acting user has no ``UserProviderKey`` for ``provider``.

    Pre-Phase-9 the system fell back to ``settings.{provider}_api_key`` if the
    user had nothing on file; the BYO charter explicitly drops that fallback
    so the analyst stays in control. The hint in the error message points at
    the Settings page so a fresh login knows where to go.
    """
    from app.services.provider_key_resolver import (
        NoProviderKeyError,
        resolve_for_user,
    )

    try:
        resolve_for_user(session, user_id=user_id, provider=provider)
    except NoProviderKeyError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"no provider key configured for '{provider}'. "
                "Upload one at /settings/keys before kicking off a run."
            ),
        ) from exc


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
    _require_user_provider_key(session, user_id=user.id, provider=provider)
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

    # Stage 3+1: every worker run carries an MCP lease — the Stage 1.5
    # local-execution fallback is gone. Direct-run prompts don't have a
    # Task wrapping them, so we mint a "direct-run" lease keyed on the
    # engagement + thread_id with the full non-exploit tool surface.
    # The Strategic policy LLM isn't called here (no task to narrow); the
    # analyst typed a freeform prompt so they get the full agent surface.
    from app.core.config import settings
    from app.orchestrator.tools import all_tools
    from app.services import mcp_lease

    allowed_tools = [
        spec.name for spec in all_tools() if spec.kind != TaskKind.exploit
    ]
    scope_items_for_lease = list(
        session.execute(
            select(ScopeItem).where(ScopeItem.engagement_id == eng.id)
        ).scalars()
    )
    lease = mcp_lease.mint_for_engagement(
        session,
        engagement_id=eng.id,
        thread_id=thread_id,
        allowed_tools=allowed_tools,
        context={
            "engagement": {
                "slug": eng.slug,
                "name": eng.name,
                "description": eng.description,
            },
            "scope": [
                {
                    "kind": item.kind.value,
                    "value": item.value,
                    "is_exclusion": item.is_exclusion,
                }
                for item in scope_items_for_lease
            ],
            "direct_run": True,
        },
        prompt_keys=[],
    )
    mcp_url = f"{settings.public_base_url.rstrip('/')}/mcp"

    # NB: we put ``acting_user_id`` on the envelope, NOT the decrypted key.
    # The worker re-resolves at run-time via UserProviderKey lookup. This
    # keeps plaintext API keys out of the Redis stream.
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
                "acting_user_id": str(user.id),
                "mcp_url": mcp_url,
                "lease_token": str(lease.id),
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
