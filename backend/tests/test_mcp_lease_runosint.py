"""MCP server-side response shape under an active lease (Stage 1.5).

When ``_run_osint`` runs while ``get_current_lease()`` is set, the response
must:
  - skip server-side finding persistence (the worker writes them instead),
  - surface findings under ``_lease_findings`` so ``mcp_executor`` can
    forward them to the worker for emit + persist,
  - still write the ``mcp.tool.X`` audit row (single source of audit truth
    for executions — no duplication on the worker side).

Without a lease, the legacy behavior is unchanged: findings get stored
server-side and the response carries ``_findings_stored=N``.
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.mcp import auth as mcp_auth
from app.mcp.server import _run_osint
from app.models import (
    APIKey,
    AuditLog,
    Engagement,
    EngagementStatus,
    Finding,
    MCPLease,
    MCPLeaseStatus,
    OwnerEligibility,
    ScopeItem,
    ScopeKind,
    Task,
    TaskKind,
    TaskStatus,
    User,
)
from app.models.api_key import APIKeyScope
from app.orchestrator.tools.runtime import ToolResult


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="run-osint-test",
        slug=f"runosint-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.active,
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


@pytest.fixture()
def cli_user_and_key(db: Session) -> Iterator[tuple[User, APIKey]]:
    user = User(email=f"runner-{uuid.uuid4().hex[:8]}@example.com", display_name="r")
    db.add(user)
    db.commit()
    db.refresh(user)
    raw = uuid.uuid4().hex
    key = APIKey(
        name="runosint-test",
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        scope=APIKeyScope.cli,
        created_by=user.id,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    yield user, key
    db.delete(key)
    db.delete(user)
    db.commit()


@pytest.fixture()
def task(db: Session, engagement: Engagement) -> Task:
    t = Task(
        engagement_id=engagement.id,
        title="runosint task",
        kind=TaskKind.enum,
        owner_eligibility=OwnerEligibility.agent,
        status=TaskStatus.pending,
        payload={},
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _build_lease(task: Task) -> MCPLease:
    return MCPLease(
        id=uuid.uuid4(),
        task_id=task.id,
        engagement_id=task.engagement_id,
        allowed_tools=["dns_lookup"],
        context={},
        prompt_keys=[],
        status=MCPLeaseStatus.active.value,
        created_at=datetime.now(tz=UTC),
        expires_at=datetime.now(tz=UTC) + timedelta(seconds=600),
    )


def _set_caller_context(
    user: User, key: APIKey, lease: MCPLease | None
) -> list[object]:
    """Set the three ContextVars _run_osint reads. Returns reset tokens."""
    tokens = [
        mcp_auth._current_key.set(key),  # type: ignore[attr-defined]
        mcp_auth._current_user.set(user),  # type: ignore[attr-defined]
        mcp_auth.set_current_lease_for_tests(lease),
    ]
    return tokens


def _reset_caller_context(tokens: list[object]) -> None:
    mcp_auth._current_key.reset(tokens[0])  # type: ignore[attr-defined]
    mcp_auth._current_user.reset(tokens[1])  # type: ignore[attr-defined]
    mcp_auth.reset_current_lease_for_tests(tokens[2])


def _patched_run_tool(_name: str, _args):
    """Synthetic dns_lookup result with one finding so we can observe the
    leased vs non-leased branch."""
    return ToolResult(
        ok=True,
        data={"records": [{"a": "1.2.3.4"}]},
        findings=[
            {
                "target": "acme.test",
                "severity": "info",
                "title": "dns A record",
                "data": {"a": "1.2.3.4"},
            }
        ],
    )


def test_run_osint_leased_returns_lease_findings_and_skips_db_store(
    db: Session,
    engagement: Engagement,
    cli_user_and_key: tuple[User, APIKey],
    task: Task,
) -> None:
    user, key = cli_user_and_key
    lease = _build_lease(task)

    pre_findings = db.execute(
        select(Finding).where(Finding.engagement_id == engagement.id)
    ).scalars().all()
    assert pre_findings == []

    tokens = _set_caller_context(user, key, lease)
    try:
        with patch("app.mcp.server.run_tool", _patched_run_tool):
            result = _run_osint("dns_lookup", engagement.slug, {"domain": "acme.test"})
    finally:
        _reset_caller_context(tokens)

    assert "_lease_findings" in result
    assert "_findings_stored" not in result
    assert result["_lease_findings"][0]["target"] == "acme.test"
    assert result["records"] == [{"a": "1.2.3.4"}]

    # Worker is the writer on the leased path — server-side store skipped.
    post_findings = db.execute(
        select(Finding).where(Finding.engagement_id == engagement.id)
    ).scalars().all()
    assert post_findings == []

    # Audit row was still written (single source of truth for tool calls).
    audits = db.execute(
        select(AuditLog).where(
            AuditLog.engagement_id == engagement.id,
            AuditLog.event_type == "mcp.tool.dns_lookup",
        )
    ).scalars().all()
    assert len(audits) == 1
    assert audits[0].payload["via"] == "mcp.lease"


def test_run_osint_without_lease_stores_findings_server_side(
    db: Session,
    engagement: Engagement,
    cli_user_and_key: tuple[User, APIKey],
) -> None:
    """Analyst MCP path — no lease, server persists findings, response gives
    a count. Confirms Stage 1.5 didn't regress the legacy path."""
    user, key = cli_user_and_key
    tokens = _set_caller_context(user, key, lease=None)
    try:
        with patch("app.mcp.server.run_tool", _patched_run_tool):
            result = _run_osint("dns_lookup", engagement.slug, {"domain": "acme.test"})
    finally:
        _reset_caller_context(tokens)

    assert "_findings_stored" in result
    assert result["_findings_stored"] == 1
    assert "_lease_findings" not in result

    post_findings = db.execute(
        select(Finding).where(Finding.engagement_id == engagement.id)
    ).scalars().all()
    assert len(post_findings) == 1
    assert post_findings[0].target == "acme.test"

    audits = db.execute(
        select(AuditLog).where(
            AuditLog.engagement_id == engagement.id,
            AuditLog.event_type == "mcp.tool.dns_lookup",
        )
    ).scalars().all()
    assert len(audits) == 1
    assert audits[0].payload["via"] == "mcp.api"
