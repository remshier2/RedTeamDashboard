"""Tool pack selection + context curation for MCP leases.

Stage 1 packs are static-by-TaskKind. These tests pin the actual bundles
so a future edit doesn't silently change what an Execution Agent gets.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import (
    Engagement,
    EngagementStatus,
    Finding,
    FindingPhase,
    FindingStatus,
    OwnerEligibility,
    ScopeItem,
    ScopeKind,
    Severity,
    Task,
    TaskKind,
    TaskStatus,
)
from app.services import tool_packs


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="packs-test",
        slug=f"packs-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.active,
        description="tool pack tests",
    )
    db.add(eng)
    db.commit()
    db.refresh(eng)
    db.add(
        ScopeItem(
            engagement_id=eng.id,
            kind=ScopeKind.domain,
            value="acme.test",
            is_exclusion=False,
        )
    )
    db.commit()
    try:
        yield eng
    finally:
        db.execute(text("SELECT flush_engagement(:id)"), {"id": eng.id})
        db.commit()


def _make_task(
    db: Session,
    engagement: Engagement,
    *,
    kind: TaskKind,
    finding_id: uuid.UUID | None = None,
) -> Task:
    t = Task(
        engagement_id=engagement.id,
        finding_id=finding_id,
        title=f"{kind.value} task",
        kind=kind,
        owner_eligibility=OwnerEligibility.agent,
        status=TaskStatus.pending,
        payload={"tool": "x", "target": "acme.test"},
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_enum_pack_is_passive_osint_tools(
    db: Session, engagement: Engagement
) -> None:
    task = _make_task(db, engagement, kind=TaskKind.enum)
    tools = tool_packs.tools_for_task(task)
    assert "subfinder" in tools
    assert "crt_sh" in tools
    assert "dns_lookup" in tools
    # No active probes in the enum pack.
    assert "portscan" not in tools


def test_scan_pack_is_active_tools(
    db: Session, engagement: Engagement
) -> None:
    task = _make_task(db, engagement, kind=TaskKind.scan)
    tools = tool_packs.tools_for_task(task)
    assert "portscan" in tools
    assert "service_detect" in tools
    # subfinder is enum-only.
    assert "subfinder" not in tools


def test_exploit_pack_is_empty_by_charter(
    db: Session, engagement: Engagement
) -> None:
    task = _make_task(db, engagement, kind=TaskKind.exploit)
    assert tool_packs.tools_for_task(task) == []
    assert tool_packs.prompts_for_task(task) == []


def test_prompts_for_task_pick_phase_prompt(
    db: Session, engagement: Engagement
) -> None:
    enum_task = _make_task(db, engagement, kind=TaskKind.enum)
    scan_task = _make_task(db, engagement, kind=TaskKind.scan)
    assert tool_packs.prompts_for_task(enum_task) == ["passive_recon"]
    assert tool_packs.prompts_for_task(scan_task) == ["active_enum"]


def test_context_includes_engagement_scope_and_task(
    db: Session, engagement: Engagement
) -> None:
    task = _make_task(db, engagement, kind=TaskKind.enum)
    ctx = tool_packs.context_for_task(db, task)
    assert ctx["engagement"]["slug"] == engagement.slug
    assert ctx["engagement"]["name"] == engagement.name
    assert ctx["engagement"]["description"] == engagement.description
    assert len(ctx["scope"]) == 1
    assert ctx["scope"][0]["kind"] == "domain"
    assert ctx["scope"][0]["value"] == "acme.test"
    assert ctx["task"]["id"] == str(task.id)
    assert ctx["task"]["kind"] == "enum"
    # No finding linked → not included.
    assert "finding" not in ctx


def test_context_includes_finding_when_linked(
    db: Session, engagement: Engagement
) -> None:
    finding = Finding(
        engagement_id=engagement.id,
        title="cert oddity",
        severity=Severity.medium,
        details={"note": "wildcard"},
        source_tool="manual",
        target="acme.test",
        phase=FindingPhase.osint,
        status=FindingStatus.pending_validation,
    )
    db.add(finding)
    db.commit()
    db.refresh(finding)

    task = _make_task(db, engagement, kind=TaskKind.enum, finding_id=finding.id)
    ctx = tool_packs.context_for_task(db, task)
    assert "finding" in ctx
    assert ctx["finding"]["id"] == str(finding.id)
    assert ctx["finding"]["title"] == "cert oddity"
    assert ctx["finding"]["target"] == "acme.test"
    assert ctx["finding"]["severity"] == "medium"


def test_tools_list_is_a_fresh_list_per_call(
    db: Session, engagement: Engagement
) -> None:
    """Caller shouldn't be able to corrupt the registry by mutating
    the return value."""
    task = _make_task(db, engagement, kind=TaskKind.enum)
    a = tool_packs.tools_for_task(task)
    a.append("not-a-real-tool")
    b = tool_packs.tools_for_task(task)
    assert "not-a-real-tool" not in b
