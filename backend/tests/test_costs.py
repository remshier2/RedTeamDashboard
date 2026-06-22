"""Phase 11 — Costs tab roll-up (GET /engagements/{slug}/costs).

Seeds agent_executions directly and asserts the per-engagement roll-up:
totals, by-agent and by-model breakdowns, USD derived from app.core.pricing,
unpriced-model flagging, and free (local) providers counted at $0 without being
flagged. Plus 404 / auth.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.main import app
from app.models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentName,
    AgentTrigger,
    Engagement,
    EngagementStatus,
)

HDR = {"X-User-Id": "phase11@example.com"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="Phase11 Costs",
        slug=f"phase11-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.active,
        description="Costs roll-up",
    )
    db.add(eng)
    db.commit()
    db.refresh(eng)
    try:
        yield eng
    finally:
        db.execute(text("SELECT flush_engagement(:id)"), {"id": eng.id})
        db.commit()


def _exec(
    db: Session,
    eng: Engagement,
    *,
    agent: AgentName,
    provider: str | None,
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
) -> None:
    db.add(
        AgentExecution(
            engagement_id=eng.id,
            agent=agent,
            trigger=AgentTrigger.finding,
            input={},
            model_provider=provider,
            model_name=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            status=AgentExecutionStatus.completed,
            started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC),
        )
    )


def test_costs_empty(client: TestClient, engagement: Engagement) -> None:
    r = client.get(f"/engagements/{engagement.slug}/costs", headers=HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["engagement_slug"] == engagement.slug
    assert body["total"] == {
        "executions": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
    }
    assert body["by_agent"] == []
    assert body["by_model"] == []
    assert body["unpriced_models"] == []


def test_costs_rollup_by_agent_and_model(
    client: TestClient, engagement: Engagement, db: Session
) -> None:
    # A: strategic sonnet 1M+1M = $3 + $15 = 18.00
    _exec(db, engagement, agent=AgentName.strategic, provider="anthropic",
          model="claude-sonnet-4-6", tokens_in=1_000_000, tokens_out=1_000_000)
    # B: tactical haiku-3.5 1M+1M = $0.80 + $4 = 4.80
    _exec(db, engagement, agent=AgentName.tactical, provider="anthropic",
          model="claude-3-5-haiku-20241022", tokens_in=1_000_000, tokens_out=1_000_000)
    # C: strategic sonnet 2M in only = $6.00
    _exec(db, engagement, agent=AgentName.strategic, provider="anthropic",
          model="claude-sonnet-4-6", tokens_in=2_000_000, tokens_out=0)
    db.commit()

    body = client.get(f"/engagements/{engagement.slug}/costs", headers=HDR).json()

    assert body["total"]["executions"] == 3
    assert body["total"]["tokens_in"] == 4_000_000
    assert body["total"]["tokens_out"] == 2_000_000
    assert body["total"]["cost_usd"] == pytest.approx(28.80)

    # by_agent (sorted by cost desc): strategic 24.00, tactical 4.80
    agents = {a["agent"]: a for a in body["by_agent"]}
    assert agents["strategic"]["executions"] == 2
    assert agents["strategic"]["cost_usd"] == pytest.approx(24.0)
    assert agents["tactical"]["cost_usd"] == pytest.approx(4.80)
    assert body["by_agent"][0]["agent"] == "strategic"  # higher cost first

    # by_model: sonnet aggregated across A+C
    models = {m["model"]: m for m in body["by_model"]}
    assert models["claude-sonnet-4-6"]["executions"] == 2
    assert models["claude-sonnet-4-6"]["tokens_in"] == 3_000_000
    assert models["claude-sonnet-4-6"]["cost_usd"] == pytest.approx(24.0)
    assert models["claude-sonnet-4-6"]["priced"] is True
    assert body["unpriced_models"] == []


def test_costs_unpriced_model_flagged(
    client: TestClient, engagement: Engagement, db: Session
) -> None:
    _exec(db, engagement, agent=AgentName.strategic, provider="mystery",
          model="mystery-model-x", tokens_in=1000, tokens_out=1000)
    db.commit()

    body = client.get(f"/engagements/{engagement.slug}/costs", headers=HDR).json()
    assert body["total"]["tokens_in"] == 1000
    assert body["total"]["cost_usd"] == 0.0  # unpriced contributes nothing
    assert body["unpriced_models"] == ["mystery-model-x"]
    model = body["by_model"][0]
    assert model["model"] == "mystery-model-x"
    assert model["priced"] is False
    assert model["executions"] == 1


def test_costs_free_provider_not_flagged(
    client: TestClient, engagement: Engagement, db: Session
) -> None:
    _exec(db, engagement, agent=AgentName.tactical, provider="ollama",
          model="llama3.1", tokens_in=5000, tokens_out=5000)
    db.commit()

    body = client.get(f"/engagements/{engagement.slug}/costs", headers=HDR).json()
    assert body["total"]["cost_usd"] == 0.0
    assert body["total"]["tokens_in"] == 5000
    assert body["unpriced_models"] == []  # free != unpriced
    assert body["by_model"][0]["priced"] is True


def test_costs_404_unknown_engagement(client: TestClient) -> None:
    r = client.get("/engagements/does-not-exist/costs", headers=HDR)
    assert r.status_code == 404


def test_costs_requires_auth(client: TestClient, engagement: Engagement) -> None:
    r = client.get(f"/engagements/{engagement.slug}/costs")
    assert r.status_code == 401
