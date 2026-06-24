"""Tactical dispatch mints a lease + envelope carries it.

Strategic consumer releases the lease when the run terminates.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents.strategic import StrategicAgent
from app.agents.tactical import TacticalAgent
from app.models import (
    Engagement,
    EngagementStatus,
    MCPLeaseStatus,
    OwnerEligibility,
    ScopeItem,
    ScopeKind,
    Task,
    TaskKind,
    TaskStatus,
)
from app.services import mcp_lease


class _FakeRedis:
    """Captures xadd/hset/expire so we can inspect the envelope."""

    def __init__(self) -> None:
        self.xadd_calls: list[tuple[str, dict[str, Any]]] = []
        self.hset_calls: list[tuple[str, dict[str, Any]]] = []

    def hset(self, key: str, mapping: dict[str, Any]) -> int:
        self.hset_calls.append((key, mapping))
        return 1

    def expire(self, _key: str, _ttl: int) -> bool:
        return True

    def xadd(self, stream: str, fields: dict[str, Any]) -> str:
        self.xadd_calls.append((stream, fields))
        return "0-1"


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="wireup-test",
        slug=f"wireup-{uuid.uuid4().hex[:8]}",
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


def _make_enum_task(db: Session, engagement: Engagement) -> Task:
    t = Task(
        engagement_id=engagement.id,
        title="enum subdomains",
        kind=TaskKind.enum,
        owner_eligibility=OwnerEligibility.agent,
        status=TaskStatus.pending,
        payload={"tool": "subfinder", "target": "acme.test"},
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_strategic_provision_lease_uses_pack_defaults(
    db: Session, engagement: Engagement
) -> None:
    task = _make_enum_task(db, engagement)
    lease = StrategicAgent().provision_lease(db, task=task)
    db.commit()
    assert lease.status == MCPLeaseStatus.active.value
    assert "subfinder" in lease.allowed_tools
    assert "portscan" not in lease.allowed_tools
    assert lease.prompt_keys == ["passive_recon"]
    assert lease.context["engagement"]["slug"] == engagement.slug


def test_tactical_dispatch_mints_lease_and_stamps_envelope(
    db: Session, engagement: Engagement
) -> None:
    task = _make_enum_task(db, engagement)
    redis = _FakeRedis()
    TacticalAgent(redis).dispatch(db, task=task)
    db.commit()

    # Envelope carries mcp_url + lease_token.
    assert len(redis.xadd_calls) == 1
    _stream, fields = redis.xadd_calls[0]
    envelope = json.loads(fields["data"])
    assert envelope["type"] == "run.start"
    assert envelope["mcp_url"].endswith("/mcp")
    assert "lease_token" in envelope
    # Lease persisted with the token Tactical stamped.
    lease = mcp_lease.validate_token(db, envelope["lease_token"])
    assert lease is not None
    assert lease.task_id == task.id
    assert lease.engagement_id == engagement.id
    assert "subfinder" in lease.allowed_tools


def test_strategic_release_lease_is_idempotent(
    db: Session, engagement: Engagement
) -> None:
    task = _make_enum_task(db, engagement)
    lease = StrategicAgent().provision_lease(db, task=task)
    db.commit()

    agent = StrategicAgent()
    agent.release_lease(db, lease_id=lease.id, reason="run_completed")
    db.commit()
    db.refresh(lease)
    assert lease.status == MCPLeaseStatus.released.value
    first_released_at = lease.released_at

    # Second release is a no-op.
    agent.release_lease(db, lease_id=lease.id, reason="redelivery")
    db.commit()
    db.refresh(lease)
    assert lease.released_at == first_released_at


# ---------------------------------------------------------------------------
# Stage 2 — requires_container + Tactical routing
# ---------------------------------------------------------------------------


def test_strategic_default_policy_provisions_requires_container_false(
    db: Session, engagement: Engagement
) -> None:
    """Conservative default: leases mint with requires_container=False
    so every dispatch keeps the colocated path until Stage 3 LLM-driven
    policy ships."""
    task = _make_enum_task(db, engagement)
    lease = StrategicAgent().provision_lease(db, task=task)
    db.commit()
    assert lease.requires_container is False


def test_strategic_provision_lease_honours_explicit_requires_container(
    db: Session, engagement: Engagement
) -> None:
    """The kwarg override lets callers (and tests) flip the column without
    waiting for the LLM policy."""
    task = _make_enum_task(db, engagement)
    lease = StrategicAgent().provision_lease(
        db, task=task, requires_container=True
    )
    db.commit()
    assert lease.requires_container is True


def test_tactical_routes_to_colocated_when_aca_disabled(
    db: Session, engagement: Engagement, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with requires_container=True, ``aca_mcp_app_enabled=False``
    (the default — and the forced local-dev posture) collapses every
    lease to the colocated /mcp on the backend App."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "aca_mcp_app_enabled", False)
    monkeypatch.setattr(
        settings, "aca_mcp_url", "https://rtd-mcp.example.azurecontainerapps.io"
    )
    monkeypatch.setattr(settings, "public_base_url", "http://backend:8000")

    task = _make_enum_task(db, engagement)
    # Force the lease into container mode so we know it's the setting, not
    # the column, that's gating the route.
    monkeypatch.setattr(
        StrategicAgent, "_decide_requires_container", lambda self, t: True
    )

    redis = _FakeRedis()
    TacticalAgent(redis).dispatch(db, task=task)
    db.commit()

    envelope = json.loads(redis.xadd_calls[0][1]["data"])
    assert envelope["mcp_url"] == "http://backend:8000/mcp"


def test_tactical_routes_to_colocated_when_lease_does_not_require_container(
    db: Session, engagement: Engagement, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting on, lease off → still colocated. The lease column is the
    per-task switch; the setting is the deployment-level guard."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "aca_mcp_app_enabled", True)
    monkeypatch.setattr(
        settings, "aca_mcp_url", "https://rtd-mcp.example.azurecontainerapps.io"
    )
    monkeypatch.setattr(settings, "public_base_url", "http://backend:8000")

    task = _make_enum_task(db, engagement)
    # Default Strategic policy returns False — no override.

    redis = _FakeRedis()
    TacticalAgent(redis).dispatch(db, task=task)
    db.commit()

    envelope = json.loads(redis.xadd_calls[0][1]["data"])
    assert envelope["mcp_url"] == "http://backend:8000/mcp"


def test_tactical_routes_to_aca_mcp_when_lease_and_settings_agree(
    db: Session, engagement: Engagement, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both gates pass → Tactical stamps the secondary App's URL."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "aca_mcp_app_enabled", True)
    monkeypatch.setattr(
        settings, "aca_mcp_url", "https://rtd-mcp.example.azurecontainerapps.io"
    )
    monkeypatch.setattr(settings, "public_base_url", "http://backend:8000")
    monkeypatch.setattr(
        StrategicAgent, "_decide_requires_container", lambda self, t: True
    )

    task = _make_enum_task(db, engagement)
    redis = _FakeRedis()
    TacticalAgent(redis).dispatch(db, task=task)
    db.commit()

    envelope = json.loads(redis.xadd_calls[0][1]["data"])
    assert (
        envelope["mcp_url"]
        == "https://rtd-mcp.example.azurecontainerapps.io/mcp"
    )
    # Lease persisted with the container flag set.
    lease = mcp_lease.validate_token(db, envelope["lease_token"])
    assert lease is not None
    assert lease.requires_container is True


def test_tactical_routes_to_colocated_when_aca_url_blank(
    db: Session, engagement: Engagement, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting on but URL unset (mid-deploy state) → fall back rather than
    stamp a broken URL onto the envelope."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "aca_mcp_app_enabled", True)
    monkeypatch.setattr(settings, "aca_mcp_url", "")
    monkeypatch.setattr(settings, "public_base_url", "http://backend:8000")
    monkeypatch.setattr(
        StrategicAgent, "_decide_requires_container", lambda self, t: True
    )

    task = _make_enum_task(db, engagement)
    redis = _FakeRedis()
    TacticalAgent(redis).dispatch(db, task=task)
    db.commit()

    envelope = json.loads(redis.xadd_calls[0][1]["data"])
    assert envelope["mcp_url"] == "http://backend:8000/mcp"
