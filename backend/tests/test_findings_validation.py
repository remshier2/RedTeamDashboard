"""Phase 8: finding phase tagging + validation gate.

Covers the tool→phase mapping, the ?phase / ?status list filters, the
POST /findings/{id}/validate transition, and that only validated findings
reach the report.
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
    Engagement,
    EngagementStatus,
    Finding,
    FindingPhase,
    FindingStatus,
    Severity,
)
from app.orchestrator.tools import phase_for_tool


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="Phase8 Validation",
        slug=f"phase8-{uuid.uuid4().hex[:8]}",
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


def _seed(
    db: Session,
    engagement_id: uuid.UUID,
    *,
    tool: str,
    phase: FindingPhase,
    status: FindingStatus,
) -> Finding:
    row = Finding(
        engagement_id=engagement_id,
        title=f"{tool} finding",
        severity=Severity.info,
        details={},
        source_tool=tool,
        target="acme.com",
        phase=phase,
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ── tool → phase mapping ─────────────────────────────────────────────────


def test_phase_for_tool_maps_recon_and_scan() -> None:
    assert phase_for_tool("subfinder") == "osint"
    assert phase_for_tool("crt_sh") == "osint"
    assert phase_for_tool("portscan") == "vuln_scan"
    assert phase_for_tool("service_detect") == "vuln_scan"
    assert phase_for_tool("something_unknown") == "general"
    assert phase_for_tool(None) == "general"


# ── list filters ─────────────────────────────────────────────────────────


def test_findings_filter_by_phase_and_status(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    _seed(
        db, engagement.id, tool="subfinder",
        phase=FindingPhase.osint, status=FindingStatus.pending_validation,
    )
    _seed(
        db, engagement.id, tool="portscan",
        phase=FindingPhase.vuln_scan, status=FindingStatus.validated,
    )
    hdr = {"X-User-Id": "p8@example.com"}

    all_rows = client.get(
        f"/engagements/{engagement.slug}/findings", headers=hdr
    ).json()
    assert len(all_rows) == 2
    assert {r["phase"] for r in all_rows} == {"osint", "vuln_scan"}

    osint = client.get(
        f"/engagements/{engagement.slug}/findings?phase=osint", headers=hdr
    ).json()
    assert len(osint) == 1 and osint[0]["phase"] == "osint"

    pending = client.get(
        f"/engagements/{engagement.slug}/findings?status=pending_validation",
        headers=hdr,
    ).json()
    assert len(pending) == 1 and pending[0]["status"] == "pending_validation"


# ── validate transition ──────────────────────────────────────────────────


def test_validate_promotes_finding(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    f = _seed(
        db, engagement.id, tool="subfinder",
        phase=FindingPhase.osint, status=FindingStatus.pending_validation,
    )
    resp = client.post(
        f"/findings/{f.id}/validate",
        json={"decision": "validated"},
        headers={"X-User-Id": "validator@example.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "validated"
    assert body["validated_at"] is not None

    db.refresh(f)
    assert f.status is FindingStatus.validated
    assert f.validated_by is not None


def test_reject_clears_validation_stamp(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    f = _seed(
        db, engagement.id, tool="portscan",
        phase=FindingPhase.vuln_scan, status=FindingStatus.validated,
    )
    resp = client.post(
        f"/findings/{f.id}/validate",
        json={"decision": "false_positive", "reason": "stale banner"},
        headers={"X-User-Id": "validator@example.com"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "false_positive"
    assert resp.json()["validated_at"] is None


def test_validate_unknown_finding_404(client: TestClient) -> None:
    resp = client.post(
        f"/findings/{uuid.uuid4()}/validate",
        json={"decision": "validated"},
        headers={"X-User-Id": "validator@example.com"},
    )
    assert resp.status_code == 404


# ── report only includes validated ───────────────────────────────────────


def test_report_excludes_unvalidated(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    # Only a pending finding exists → it must not count as validated, and the
    # report still renders cleanly (just without that finding).
    _seed(
        db, engagement.id, tool="subfinder",
        phase=FindingPhase.osint, status=FindingStatus.pending_validation,
    )
    hdr = {"X-User-Id": "p8@example.com"}

    validated = client.get(
        f"/engagements/{engagement.slug}/findings?status=validated", headers=hdr
    ).json()
    assert validated == []

    report = client.get(f"/engagements/{engagement.slug}/report", headers=hdr)
    assert report.status_code == 200
    assert report.content.startswith(b"%PDF-")
