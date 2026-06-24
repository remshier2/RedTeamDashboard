"""Direct-run lease wiring — Stage 3+1 (Stage 1.5 fallback rip).

The Stage 1.5 worker fallback (envelope without mcp_url/lease_token →
run against local registry) is gone. ``POST /engagements/{slug}/runs``
now mints a lease per direct run so every worker invocation carries an
MCP envelope. These tests verify the lease + envelope shape and the
strategic consumer's release path for direct runs.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.main import app
from app.models import (
    Engagement,
    EngagementStatus,
    MCPLease,
    MCPLeaseStatus,
    TaskKind,
)
from app.runs.streams import inbound_stream, outbound_stream
from app.services import mcp_lease
from tests.test_engagements_api import (
    _create,
    _headers,
    _seed_provider_key,
)

# Local fixtures duplicated from test_engagements_api so pytest doesn't
# trip on F811 when both files import each other's names. Small and
# stable enough that the duplication beats moving to conftest in this PR.


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def redis_client() -> Iterator[redis_lib.Redis]:
    r = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield r
    finally:
        r.close()


@pytest.fixture()
def cleanup_slugs(
    db: Session, redis_client: redis_lib.Redis
) -> Iterator[list[str]]:
    """Test appends engagement slugs it created; teardown flushes each."""
    slugs: list[str] = []
    yield slugs
    for slug in slugs:
        eng_id = db.execute(
            select(Engagement.id).where(Engagement.slug == slug)
        ).scalar_one_or_none()
        if eng_id is None:
            continue
        db.execute(
            text("DELETE FROM approvals WHERE engagement_id = :id"),
            {"id": eng_id},
        )
        db.commit()
        db.execute(text("SELECT flush_engagement(:id)"), {"id": eng_id})
        db.commit()
        redis_client.delete(inbound_stream(eng_id), outbound_stream(eng_id))

# ---------------------------------------------------------------------------
# Service-level: direct-run mint + thread lookup
# ---------------------------------------------------------------------------


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="direct-run-test",
        slug=f"direct-{uuid.uuid4().hex[:8]}",
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


def test_mint_for_engagement_persists_lease_with_null_task_id(
    db: Session, engagement: Engagement
) -> None:
    thread_id = uuid.uuid4()
    lease = mcp_lease.mint_for_engagement(
        db,
        engagement_id=engagement.id,
        thread_id=thread_id,
        allowed_tools=["subfinder", "crt_sh"],
        context={"engagement": {"slug": engagement.slug}, "direct_run": True},
        prompt_keys=[],
    )
    db.commit()

    db.refresh(lease)
    assert lease.task_id is None
    assert lease.engagement_id == engagement.id
    assert lease.allowed_tools == ["subfinder", "crt_sh"]
    assert lease.context["_thread_id"] == str(thread_id)
    assert lease.context["direct_run"] is True
    assert lease.status == MCPLeaseStatus.active.value
    assert lease.requires_container is False


def test_find_active_for_thread_resolves_direct_run_lease(
    db: Session, engagement: Engagement
) -> None:
    thread_id = uuid.uuid4()
    lease = mcp_lease.mint_for_engagement(
        db,
        engagement_id=engagement.id,
        thread_id=thread_id,
        allowed_tools=["subfinder"],
        context={},
        prompt_keys=[],
    )
    db.commit()

    found = mcp_lease.find_active_for_thread(db, thread_id)
    assert found is not None
    assert found.id == lease.id


def test_find_active_for_thread_returns_none_when_released(
    db: Session, engagement: Engagement
) -> None:
    thread_id = uuid.uuid4()
    lease = mcp_lease.mint_for_engagement(
        db,
        engagement_id=engagement.id,
        thread_id=thread_id,
        allowed_tools=[],
        context={},
        prompt_keys=[],
    )
    mcp_lease.release(db, lease_id=lease.id, reason="test")
    db.commit()

    assert mcp_lease.find_active_for_thread(db, thread_id) is None


def test_find_active_for_thread_returns_none_for_unknown_thread(
    db: Session,
) -> None:
    assert mcp_lease.find_active_for_thread(db, uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# API-level: POST /runs mints + stamps envelope
# ---------------------------------------------------------------------------


def test_post_runs_mints_direct_run_lease_and_stamps_envelope(
    client: TestClient,
    redis_client: redis_lib.Redis,
    cleanup_slugs: list[str],
    db: Session,
) -> None:
    """Stage 3+1 contract: every run.start envelope carries an MCP
    envelope. Direct runs mint a lease with task_id=NULL, the full
    non-exploit tool surface, and the colocated MCP URL."""
    _seed_provider_key(client)
    eng = _create(client, "Direct-run lease")
    cleanup_slugs.append(eng["slug"])

    response = client.post(
        f"/engagements/{eng['slug']}/runs",
        json={
            "prompt": "look at the engagement",
            # _seed_provider_key seeds Ollama by default; pin so we don't
            # trip the BYO-key precheck on the settings-default provider.
            "model": {"provider": "ollama", "name": "llama3.1:8b"},
        },
        headers=_headers(),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    thread_id = uuid.UUID(body["thread_id"])

    queued = redis_client.xrange(inbound_stream(uuid.UUID(eng["id"])))
    payload = json.loads(queued[-1][1]["data"])
    # Stage 3+1 envelope additions.
    assert payload["mcp_url"].endswith("/mcp")
    assert "lease_token" in payload

    # Lease persisted with task_id=NULL and is reachable by thread_id.
    lease = mcp_lease.validate_token(db, payload["lease_token"])
    assert lease is not None
    assert lease.task_id is None
    assert lease.engagement_id == uuid.UUID(eng["id"])
    assert lease.context["_thread_id"] == str(thread_id)
    assert lease.context["direct_run"] is True


def test_post_runs_lease_excludes_exploit_tools(
    client: TestClient,
    redis_client: redis_lib.Redis,
    cleanup_slugs: list[str],
    db: Session,
) -> None:
    """Charter invariant — direct-run lease never carries exploit-kind
    tools, even though it grants the otherwise-full agent surface."""
    from app.orchestrator.tools import all_tools

    _seed_provider_key(client)
    eng = _create(client, "No exploit in direct run")
    cleanup_slugs.append(eng["slug"])

    response = client.post(
        f"/engagements/{eng['slug']}/runs",
        json={
            "prompt": "go",
            "model": {"provider": "ollama", "name": "llama3.1:8b"},
        },
        headers=_headers(),
    )
    assert response.status_code == 202, response.text
    payload = json.loads(
        redis_client.xrange(inbound_stream(uuid.UUID(eng["id"])))[-1][1]["data"]
    )

    lease = mcp_lease.validate_token(db, payload["lease_token"])
    assert lease is not None
    exploit_tools = {
        spec.name for spec in all_tools() if spec.kind == TaskKind.exploit
    }
    assert not exploit_tools & set(lease.allowed_tools)


def test_post_runs_lease_uses_colocated_mcp_url(
    client: TestClient,
    redis_client: redis_lib.Redis,
    cleanup_slugs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-run leases default to colocated — no LLM is making the
    container decision, so the conservative path applies."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "public_base_url", "http://backend:8000")

    _seed_provider_key(client)
    eng = _create(client, "Colocated direct-run")
    cleanup_slugs.append(eng["slug"])

    response = client.post(
        f"/engagements/{eng['slug']}/runs",
        json={
            "prompt": "go",
            "model": {"provider": "ollama", "name": "llama3.1:8b"},
        },
        headers=_headers(),
    )
    assert response.status_code == 202, response.text
    payload = json.loads(
        redis_client.xrange(inbound_stream(uuid.UUID(eng["id"])))[-1][1]["data"]
    )
    assert payload["mcp_url"] == "http://backend:8000/mcp"


# ---------------------------------------------------------------------------
# Consumer release: thread-based lookup for direct runs
# ---------------------------------------------------------------------------


def test_strategic_consumer_releases_direct_run_lease_on_terminal_event(
    db: Session, engagement: Engagement
) -> None:
    """Without a Task, the consumer falls back to find_active_for_thread
    so direct-run leases get released on run.completed/run.errored.
    Idempotent: a redelivered terminal event is safe."""
    from app.worker.strategic_consumer import StrategicConsumer

    thread_id = uuid.uuid4()
    lease = mcp_lease.mint_for_engagement(
        db,
        engagement_id=engagement.id,
        thread_id=thread_id,
        allowed_tools=[],
        context={},
        prompt_keys=[],
    )
    db.commit()

    consumer = StrategicConsumer(
        agent=None,  # type: ignore[arg-type] — release path doesn't touch agent
        redis_client=None,  # type: ignore[arg-type]
        session_factory=lambda: db,
    )
    consumer._release_lease_for_run(thread_id, reason="run.completed")

    # Use a fresh session-scope read to bypass cached identity.
    fresh = db.execute(
        select(MCPLease).where(MCPLease.id == lease.id)
    ).scalar_one()
    assert fresh.status == MCPLeaseStatus.released.value
    assert fresh.released_at is not None
