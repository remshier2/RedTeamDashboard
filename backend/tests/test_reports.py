"""PDF report endpoint.

Seeds an engagement with scope, a finding, an approval, and an audit-log
entry, then hits GET /engagements/{slug}/report and checks the response is
a non-trivial PDF.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.main import app
from app.models import (
    ActorType,
    Approval,
    ApprovalStatus,
    AuditLog,
    Engagement,
    EngagementStatus,
    Finding,
    FindingPhase,
    FindingStatus,
    Observation,
    RiskLevel,
    ScopeItem,
    ScopeKind,
    Severity,
)

# WeasyPrint needs GTK shared libraries (libgobject-2.0, pango, etc.) which
# aren't available on Windows dev machines. Skip PDF-rendering tests there;
# they run cleanly in CI on the Ubuntu runner where GTK is installed.
_weasyprint_ok: bool
try:
    import weasyprint  # noqa: F401

    _weasyprint_ok = True
except OSError:
    _weasyprint_ok = False

_needs_gtk = pytest.mark.skipif(
    not _weasyprint_ok, reason="WeasyPrint GTK libraries not available on this host"
)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="Acme Report Test",
        slug=f"report-test-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.active,
    )
    db.add(eng)
    db.commit()
    db.refresh(eng)
    try:
        yield eng
    finally:
        db.execute(
            text("DELETE FROM approvals WHERE engagement_id = :id"),
            {"id": eng.id},
        )
        db.commit()
        db.execute(text("SELECT flush_engagement(:id)"), {"id": eng.id})
        db.commit()


def _seed_data(db: Session, engagement_id: uuid.UUID) -> None:
    db.add(
        ScopeItem(
            engagement_id=engagement_id,
            kind=ScopeKind.domain,
            value="acme.com",
            is_exclusion=False,
        )
    )
    db.add(
        Finding(
            engagement_id=engagement_id,
            title="subfinder → acme.com",
            severity=Severity.info,
            details={"subdomains": ["www.acme.com", "mail.acme.com"]},
            source_tool="subfinder",
            target="acme.com",
            phase=FindingPhase.osint,
            # Report only includes validated findings (Phase 8 gate).
            status=FindingStatus.validated,
        )
    )
    db.add(
        Approval(
            engagement_id=engagement_id,
            thread_id=str(uuid.uuid4()),
            node="tool_dispatch",
            tool_name="portscan",
            tool_args={"ip": "10.0.0.5"},
            risk=RiskLevel.active,
            scope_check={"ok": True},
            status=ApprovalStatus.approved,
        )
    )
    db.add(
        AuditLog(
            engagement_id=engagement_id,
            actor_type=ActorType.agent,
            actor_id="worker",
            event_type="run.started",
            payload={"thread_id": "abc-123", "prompt": "enumerate acme.com"},
        )
    )
    db.commit()


@_needs_gtk
def test_report_renders_pdf(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    _seed_data(db, engagement.id)

    response = client.get(
        f"/engagements/{engagement.slug}/report",
        headers={"X-User-Id": "report-test@example.com"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment;")
    # PDF magic bytes
    assert response.content.startswith(b"%PDF-")
    # And it's at least a few KB — real content, not a stub.
    assert len(response.content) > 2_000


@_needs_gtk
def test_report_includes_observations(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    db.add(
        Observation(
            engagement_id=engagement.id,
            content="Certificate expires in 14 days",
            phase=FindingPhase.osint,
        )
    )
    db.commit()

    resp = client.get(
        f"/engagements/{engagement.slug}/report",
        headers={"X-User-Id": "report-test@example.com"},
    )
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")


def test_report_404_for_unknown_engagement(client: TestClient) -> None:
    response = client.get(
        f"/engagements/does-not-exist-{uuid.uuid4().hex[:6]}/report",
        headers={"X-User-Id": "report-test@example.com"},
    )
    assert response.status_code == 404


def test_report_requires_x_user_id(
    client: TestClient, engagement: Engagement
) -> None:
    response = client.get(f"/engagements/{engagement.slug}/report")
    assert response.status_code == 401
