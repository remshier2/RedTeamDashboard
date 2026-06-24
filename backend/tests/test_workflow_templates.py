"""Workflow templates — Phase 10 starter packs.

Covers: idempotent seed, list ordering (system first), apply happy path
(creates pending Tasks with right shape), apply error cases (unknown
template, blank target, malformed step, exploit-kind step refused).
"""
from __future__ import annotations

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
    OwnerEligibility,
    Task,
    TaskKind,
    TaskStatus,
    WorkflowTemplate,
)
from app.runs.streams import inbound_stream, outbound_stream
from app.services import workflow_templates as wt_service
from tests.test_engagements_api import _create, _headers


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


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="tpl-test",
        slug=f"tpl-{uuid.uuid4().hex[:8]}",
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


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


def test_seed_inserts_starter_set_idempotently(db: Session) -> None:
    """First seed creates the starter rows. Second seed is a no-op
    even if the constants were the same (idempotency key = name)."""
    # Clean slate — drop any prior seeds.
    db.execute(text("DELETE FROM workflow_templates WHERE is_system = true"))
    db.commit()

    inserted = wt_service.seed_system_templates(db)
    db.commit()
    assert inserted == len(wt_service.STARTER_TEMPLATES)

    second_pass = wt_service.seed_system_templates(db)
    db.commit()
    assert second_pass == 0

    names = {
        row.name
        for row in db.execute(
            select(WorkflowTemplate).where(WorkflowTemplate.is_system.is_(True))
        ).scalars()
    }
    assert "OSINT Enum" in names
    assert "Web App" in names
    assert "Network Recon" in names


def test_seed_leaves_existing_system_rows_alone(db: Session) -> None:
    """Seeding does NOT update an existing row's steps even if the code
    constant changed — analyst-facing template shape stays stable."""
    db.execute(text("DELETE FROM workflow_templates WHERE is_system = true"))
    db.commit()
    wt_service.seed_system_templates(db)
    db.commit()

    osint = db.execute(
        select(WorkflowTemplate).where(WorkflowTemplate.name == "OSINT Enum")
    ).scalar_one()
    # Simulate a manual change to the row's description (could be analyst
    # editing or a Phase 10b CRUD path).
    osint.description = "edited"
    db.commit()

    wt_service.seed_system_templates(db)
    db.commit()
    db.refresh(osint)
    assert osint.description == "edited"  # unchanged by seed


def test_list_returns_system_first(db: Session, engagement: Engagement) -> None:
    db.execute(text("DELETE FROM workflow_templates"))
    db.commit()
    wt_service.seed_system_templates(db)
    # Inject a user template so we can verify the ordering.
    db.add(
        WorkflowTemplate(
            name="User Custom",
            description="user-authored",
            is_system=False,
            target_kind="domain",
            steps=[
                {
                    "tool": "dns_lookup",
                    "kind": "enum",
                    "owner_eligibility": "agent",
                    "title": "Custom lookup",
                    "rationale": "custom",
                }
            ],
        )
    )
    db.commit()

    rows = wt_service.list_templates(db)
    # System rows first.
    assert all(rows[i].is_system for i in range(len(wt_service.STARTER_TEMPLATES)))
    # User row last.
    assert rows[-1].name == "User Custom"


def test_apply_creates_pending_tasks_with_right_shape(
    db: Session, engagement: Engagement
) -> None:
    db.execute(text("DELETE FROM workflow_templates WHERE is_system = true"))
    db.commit()
    wt_service.seed_system_templates(db)
    db.commit()

    osint = db.execute(
        select(WorkflowTemplate).where(WorkflowTemplate.name == "OSINT Enum")
    ).scalar_one()
    tasks = wt_service.apply_template(
        db, template=osint, engagement=engagement, target="acme.test"
    )
    db.commit()

    assert len(tasks) == len(osint.steps)
    for t in tasks:
        assert t.engagement_id == engagement.id
        assert t.status is TaskStatus.pending
        assert t.payload["target"] == "acme.test"
        assert t.payload["tool"] in {
            step["tool"] for step in osint.steps
        }
        assert isinstance(t.kind, TaskKind)
        assert isinstance(t.owner_eligibility, OwnerEligibility)


def test_apply_rejects_blank_target(
    db: Session, engagement: Engagement
) -> None:
    db.execute(text("DELETE FROM workflow_templates WHERE is_system = true"))
    db.commit()
    wt_service.seed_system_templates(db)
    db.commit()
    osint = db.execute(
        select(WorkflowTemplate).where(WorkflowTemplate.name == "OSINT Enum")
    ).scalar_one()

    with pytest.raises(wt_service.WorkflowTemplateApplyError, match="blank"):
        wt_service.apply_template(
            db, template=osint, engagement=engagement, target="   "
        )


def test_apply_refuses_exploit_kind_step(
    db: Session, engagement: Engagement
) -> None:
    """Charter defense-in-depth — even if a user template tried to embed
    an exploit-kind step, apply refuses. Seed templates never include
    exploit-kind, but a future CRUD path could try."""
    bad = WorkflowTemplate(
        name="bad-exploit",
        is_system=False,
        target_kind="domain",
        steps=[
            {
                "tool": "any",
                "kind": "exploit",
                "owner_eligibility": "analyst",
                "title": "should be refused",
                "rationale": "charter test",
            }
        ],
    )
    db.add(bad)
    db.commit()

    with pytest.raises(
        wt_service.WorkflowTemplateApplyError, match="exploit"
    ):
        wt_service.apply_template(
            db, template=bad, engagement=engagement, target="acme.test"
        )


def test_apply_rejects_malformed_step(
    db: Session, engagement: Engagement
) -> None:
    bad = WorkflowTemplate(
        name="bad-shape",
        is_system=False,
        target_kind="domain",
        steps=[{"tool": "dns_lookup"}],  # missing kind/owner/title
    )
    db.add(bad)
    db.commit()

    with pytest.raises(
        wt_service.WorkflowTemplateApplyError, match="malformed"
    ):
        wt_service.apply_template(
            db, template=bad, engagement=engagement, target="acme.test"
        )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


def test_list_endpoint_returns_seeded_starter_set(
    client: TestClient,
    db: Session,
) -> None:
    """Startup lifespan seeded the rows; the endpoint surfaces them."""
    res = client.get("/workflow-templates", headers=_headers())
    assert res.status_code == 200, res.text
    body = res.json()
    names = {t["name"] for t in body}
    assert "OSINT Enum" in names
    assert "Web App" in names
    assert "Network Recon" in names
    # System rows are flagged correctly.
    for tpl in body:
        if tpl["name"] in {"OSINT Enum", "Web App", "Network Recon"}:
            assert tpl["is_system"] is True


def test_apply_endpoint_creates_tasks(
    client: TestClient,
    db: Session,
    cleanup_slugs: list[str],
) -> None:
    eng = _create(client, "Template apply")
    cleanup_slugs.append(eng["slug"])

    # Pick the seeded OSINT Enum template via the list endpoint.
    list_res = client.get("/workflow-templates", headers=_headers())
    osint = next(t for t in list_res.json() if t["name"] == "OSINT Enum")

    apply_res = client.post(
        f"/engagements/{eng['slug']}/templates/{osint['id']}/apply",
        json={"target": "acme.test"},
        headers=_headers(),
    )
    assert apply_res.status_code == 201, apply_res.text
    body = apply_res.json()
    assert body["template_name"] == "OSINT Enum"
    assert body["target"] == "acme.test"
    assert len(body["tasks"]) == len(osint["steps"])
    for t in body["tasks"]:
        assert t["status"] == TaskStatus.pending.value
        assert t["payload"]["target"] == "acme.test"

    # And the Task rows were really persisted.
    eng_id = uuid.UUID(eng["id"])
    persisted = list(
        db.execute(select(Task).where(Task.engagement_id == eng_id)).scalars()
    )
    assert len(persisted) == len(osint["steps"])


def test_apply_endpoint_404_on_unknown_template(
    client: TestClient,
    cleanup_slugs: list[str],
) -> None:
    eng = _create(client, "Template 404")
    cleanup_slugs.append(eng["slug"])

    res = client.post(
        f"/engagements/{eng['slug']}/templates/{uuid.uuid4()}/apply",
        json={"target": "acme.test"},
        headers=_headers(),
    )
    assert res.status_code == 404


def test_apply_endpoint_404_on_unknown_engagement(
    client: TestClient, db: Session
) -> None:
    list_res = client.get("/workflow-templates", headers=_headers())
    osint = next(t for t in list_res.json() if t["name"] == "OSINT Enum")

    res = client.post(
        f"/engagements/does-not-exist-{uuid.uuid4().hex[:6]}/templates/{osint['id']}/apply",
        json={"target": "acme.test"},
        headers=_headers(),
    )
    assert res.status_code == 404


def test_apply_endpoint_validates_target_required(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, "Template empty target")
    cleanup_slugs.append(eng["slug"])

    list_res = client.get("/workflow-templates", headers=_headers())
    osint = next(t for t in list_res.json() if t["name"] == "OSINT Enum")

    # Empty string trips Pydantic min_length=1.
    res = client.post(
        f"/engagements/{eng['slug']}/templates/{osint['id']}/apply",
        json={"target": ""},
        headers=_headers(),
    )
    assert res.status_code == 422
