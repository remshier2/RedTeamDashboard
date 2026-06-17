"""Observations API — GET/POST /engagements/{slug}/observations, DELETE /observations/{id}.

Freeform analyst notes: created with content + optional phase, listed
chronologically, deletable. Cannot be added to flushed engagements.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.main import app
from app.models import Engagement, EngagementStatus, Observation


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="Obs Test",
        slug=f"obs-test-{uuid.uuid4().hex[:8]}",
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


_HDR = {"X-User-Id": "obs-test@example.com"}


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_observation(client: TestClient, engagement: Engagement) -> None:
    resp = client.post(
        f"/engagements/{engagement.slug}/observations",
        json={"content": "Login portal exposes version string", "phase": "osint"},
        headers=_HDR,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["content"] == "Login portal exposes version string"
    assert body["phase"] == "osint"
    assert body["id"] is not None
    assert body["created_at"] is not None


def test_create_observation_no_phase(client: TestClient, engagement: Engagement) -> None:
    resp = client.post(
        f"/engagements/{engagement.slug}/observations",
        json={"content": "Generic note without a phase"},
        headers=_HDR,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["phase"] is None


def test_create_observation_empty_content_rejected(
    client: TestClient, engagement: Engagement
) -> None:
    resp = client.post(
        f"/engagements/{engagement.slug}/observations",
        json={"content": ""},
        headers=_HDR,
    )
    assert resp.status_code == 422


def test_create_observation_404_for_unknown_slug(client: TestClient) -> None:
    resp = client.post(
        f"/engagements/does-not-exist-{uuid.uuid4().hex[:6]}/observations",
        json={"content": "some note"},
        headers=_HDR,
    )
    assert resp.status_code == 404


def test_create_observation_requires_auth(
    client: TestClient, engagement: Engagement
) -> None:
    resp = client.post(
        f"/engagements/{engagement.slug}/observations",
        json={"content": "note"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_observations_returns_chronological(
    client: TestClient, engagement: Engagement
) -> None:
    for i in range(3):
        client.post(
            f"/engagements/{engagement.slug}/observations",
            json={"content": f"note {i}"},
            headers=_HDR,
        )

    resp = client.get(
        f"/engagements/{engagement.slug}/observations", headers=_HDR
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) >= 3
    # Oldest first (created_at ASC)
    timestamps = [r["created_at"] for r in rows]
    assert timestamps == sorted(timestamps)


def test_list_observations_empty_for_new_engagement(
    client: TestClient, engagement: Engagement
) -> None:
    resp = client.get(
        f"/engagements/{engagement.slug}/observations", headers=_HDR
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_observation(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    create = client.post(
        f"/engagements/{engagement.slug}/observations",
        json={"content": "delete me"},
        headers=_HDR,
    )
    obs_id = create.json()["id"]

    resp = client.delete(f"/observations/{obs_id}", headers=_HDR)
    assert resp.status_code == 204

    gone = db.execute(
        select(Observation).where(Observation.id == uuid.UUID(obs_id))
    ).scalar_one_or_none()
    assert gone is None


def test_delete_observation_404_for_unknown(client: TestClient) -> None:
    resp = client.delete(f"/observations/{uuid.uuid4()}", headers=_HDR)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Flushed engagement guard
# ---------------------------------------------------------------------------


def test_cannot_add_observation_to_flushed_engagement(
    client: TestClient, db: Session
) -> None:
    eng = Engagement(
        name="Flushed",
        slug=f"flushed-obs-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.flushed,
    )
    db.add(eng)
    db.commit()
    db.refresh(eng)

    resp = client.post(
        f"/engagements/{eng.slug}/observations",
        json={"content": "should fail"},
        headers=_HDR,
    )
    assert resp.status_code == 409

    # Clean up the flushed row directly (flush_engagement won't work on flushed)
    db.delete(eng)
    db.commit()
