"""Worker registry filtering by MCP lease.

When the run.start envelope carries a ``lease_token``, ``RunRunner`` looks
up the lease and threads its ``allowed_tools`` to the graph factory so
the agent's bound registry shrinks to that surface. No token → full
registry (legacy). Invalid/expired token → raises so the run errors
cleanly instead of silently widening.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import (
    Engagement,
    EngagementStatus,
    OwnerEligibility,
    Task,
    TaskKind,
    TaskStatus,
)
from app.services import mcp_lease
from app.worker.runner import RunRunner


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="lease-filter",
        slug=f"filter-{uuid.uuid4().hex[:8]}",
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


def _make_task(db: Session, engagement: Engagement) -> Task:
    t = Task(
        engagement_id=engagement.id,
        title="filter task",
        kind=TaskKind.enum,
        owner_eligibility=OwnerEligibility.agent,
        status=TaskStatus.pending,
        payload={},
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _capture_factory() -> tuple[list[tuple[Any, Any]], Any]:
    captured: list[tuple[Any, Any]] = []

    def factory(model: Any, allowed_tools: Any = None) -> Any:
        captured.append((model, allowed_tools))
        return object()

    return captured, factory


def test_no_lease_token_means_full_registry(db: Session) -> None:
    """Legacy path — envelope without lease_token gets allowed_tools=None."""
    captured, factory = _capture_factory()
    runner = RunRunner(
        graph_factory=factory,
        redis_client=None,  # type: ignore[arg-type]
        session_factory=SessionLocal,
    )
    runner._resolve_graph({"type": "run.start"})
    assert captured == [(None, None)]


def test_valid_lease_token_filters_registry(
    db: Session, engagement: Engagement
) -> None:
    task = _make_task(db, engagement)
    lease = mcp_lease.mint(
        db,
        task=task,
        allowed_tools=["subfinder"],
        context={},
        prompt_keys=[],
    )
    db.commit()

    captured, factory = _capture_factory()
    runner = RunRunner(
        graph_factory=factory,
        redis_client=None,  # type: ignore[arg-type]
        session_factory=SessionLocal,
    )
    runner._resolve_graph(
        {"type": "run.start", "lease_token": str(lease.id)}
    )
    _model, allowed = captured[0]
    assert allowed == ["subfinder"]


def test_invalid_lease_token_raises_so_run_errors(db: Session) -> None:
    captured, factory = _capture_factory()
    runner = RunRunner(
        graph_factory=factory,
        redis_client=None,  # type: ignore[arg-type]
        session_factory=SessionLocal,
    )
    with pytest.raises(ValueError, match="invalid, released, or expired"):
        runner._resolve_graph(
            {"type": "run.start", "lease_token": str(uuid.uuid4())}
        )
    assert captured == []


def test_expired_lease_token_raises(
    db: Session, engagement: Engagement
) -> None:
    task = _make_task(db, engagement)
    lease = mcp_lease.mint(
        db,
        task=task,
        allowed_tools=["subfinder"],
        context={},
        prompt_keys=[],
        ttl_seconds=10,
    )
    lease.expires_at = datetime.now(tz=UTC) - timedelta(seconds=5)
    db.commit()

    captured, factory = _capture_factory()
    runner = RunRunner(
        graph_factory=factory,
        redis_client=None,  # type: ignore[arg-type]
        session_factory=SessionLocal,
    )
    with pytest.raises(ValueError, match="invalid, released, or expired"):
        runner._resolve_graph(
            {"type": "run.start", "lease_token": str(lease.id)}
        )
