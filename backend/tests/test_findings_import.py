"""Findings bulk import — POST /engagements/{slug}/findings/import.

All imported findings land as pending_validation. Writes a findings.imported
audit log entry. Requires an authenticated user.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.main import app
from app.models import AuditLog, Engagement, EngagementStatus, Finding, FindingStatus


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="Import Test",
        slug=f"import-test-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.active,
    )
    db.add(eng)
    db.commit()
    db.refresh(eng)
    try:
        yield eng
    finally:
        db.execute(text("SELECT flush_engagement(:id)"), {"id": eng.id})
        db.commit()


_HDR = {"X-User-Id": "import-test@example.com"}


# ---------------------------------------------------------------------------
# Basic import
# ---------------------------------------------------------------------------


def test_import_single_finding(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    payload = [{"title": "TLS cert expiring soon", "severity": "medium", "phase": "osint"}]
    resp = client.post(
        f"/engagements/{engagement.slug}/findings/import",
        json=payload,
        headers=_HDR,
    )
    assert resp.status_code == 201, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["title"] == "TLS cert expiring soon"
    assert rows[0]["severity"] == "medium"
    assert rows[0]["phase"] == "osint"
    assert rows[0]["status"] == "pending_validation"


def test_import_multiple_findings(
    client: TestClient, engagement: Engagement
) -> None:
    payload = [
        {"title": "Finding A", "severity": "high"},
        {"title": "Finding B", "severity": "low"},
        {"title": "Finding C"},
    ]
    resp = client.post(
        f"/engagements/{engagement.slug}/findings/import",
        json=payload,
        headers=_HDR,
    )
    assert resp.status_code == 201, resp.text
    rows = resp.json()
    assert len(rows) == 3
    titles = {r["title"] for r in rows}
    assert titles == {"Finding A", "Finding B", "Finding C"}


def test_import_empty_list_returns_empty(
    client: TestClient, engagement: Engagement
) -> None:
    resp = client.post(
        f"/engagements/{engagement.slug}/findings/import",
        json=[],
        headers=_HDR,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_import_defaults_source_tool_to_import(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    resp = client.post(
        f"/engagements/{engagement.slug}/findings/import",
        json=[{"title": "No tool specified"}],
        headers=_HDR,
    )
    assert resp.status_code == 201
    finding_id = uuid.UUID(resp.json()[0]["id"])
    row = db.get(Finding, finding_id)
    assert row is not None
    assert row.source_tool == "import"


def test_import_respects_explicit_source_tool(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    resp = client.post(
        f"/engagements/{engagement.slug}/findings/import",
        json=[{"title": "Nessus finding", "source_tool": "nessus"}],
        headers=_HDR,
    )
    assert resp.status_code == 201
    finding_id = uuid.UUID(resp.json()[0]["id"])
    row = db.get(Finding, finding_id)
    assert row is not None
    assert row.source_tool == "nessus"


def test_import_all_land_as_pending_validation(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    resp = client.post(
        f"/engagements/{engagement.slug}/findings/import",
        json=[{"title": "Check me"}, {"title": "Check me too"}],
        headers=_HDR,
    )
    assert resp.status_code == 201
    for row in resp.json():
        assert row["status"] == "pending_validation"
        db_row = db.get(Finding, uuid.UUID(row["id"]))
        assert db_row is not None
        assert db_row.status is FindingStatus.pending_validation


def test_import_defaults_phase_to_general(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    resp = client.post(
        f"/engagements/{engagement.slug}/findings/import",
        json=[{"title": "No phase"}],
        headers=_HDR,
    )
    assert resp.status_code == 201
    assert resp.json()[0]["phase"] == "general"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_import_writes_audit_log(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    resp = client.post(
        f"/engagements/{engagement.slug}/findings/import",
        json=[{"title": "A"}, {"title": "B"}],
        headers=_HDR,
    )
    assert resp.status_code == 201

    entry = db.execute(
        select(AuditLog).where(
            AuditLog.engagement_id == engagement.id,
            AuditLog.event_type == "findings.imported",
        )
    ).scalar_one_or_none()
    assert entry is not None
    assert entry.payload["count"] == 2
    assert entry.payload["source"] == "bulk_import"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_import_404_for_unknown_engagement(client: TestClient) -> None:
    resp = client.post(
        f"/engagements/does-not-exist-{uuid.uuid4().hex[:6]}/findings/import",
        json=[{"title": "orphan"}],
        headers=_HDR,
    )
    assert resp.status_code == 404


def test_import_requires_auth(client: TestClient, engagement: Engagement) -> None:
    resp = client.post(
        f"/engagements/{engagement.slug}/findings/import",
        json=[{"title": "unauthed"}],
    )
    assert resp.status_code == 401


def test_import_409_for_flushed_engagement(
    client: TestClient, db: Session
) -> None:
    eng = Engagement(
        name="Flushed Import",
        slug=f"flushed-import-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.flushed,
    )
    db.add(eng)
    db.commit()
    db.refresh(eng)

    resp = client.post(
        f"/engagements/{eng.slug}/findings/import",
        json=[{"title": "should fail"}],
        headers=_HDR,
    )
    assert resp.status_code == 409

    db.delete(eng)
    db.commit()
