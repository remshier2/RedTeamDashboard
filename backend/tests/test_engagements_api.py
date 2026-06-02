"""Engagements + scope + runs HTTP API.

Tests use the live compose Postgres + Redis. Each test that creates
engagements via the API registers their slugs with a teardown fixture so the
``flush_engagement`` DB helper can clean them up afterwards.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.main import app
from app.models import Engagement, Finding, Severity
from app.runs.streams import inbound_stream, outbound_stream

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def cleanup_slugs(db: Session, redis_client: redis_lib.Redis) -> Iterator[list[str]]:
    """Tests append slugs they create; teardown flushes each engagement."""
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


def _headers() -> dict[str, str]:
    return {"X-User-Id": "engagement-test@example.com"}


def _create(client: TestClient, name: str, slug: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if slug is not None:
        body["slug"] = slug
    response = client.post("/engagements", json=body, headers=_headers())
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Engagement CRUD
# ---------------------------------------------------------------------------


def test_create_with_auto_generated_slug(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    # Use a unique name so a left-behind row from a previous run can't force
    # the unique-slug suffix path and trip the equality assertion.
    name = f"Auto Slug {uuid.uuid4().hex[:6]}"
    body = _create(client, name)
    cleanup_slugs.append(body["slug"])
    expected = name.lower().replace(" ", "-")
    assert body["slug"] == expected
    assert body["name"] == name
    assert body["status"] == "active"
    assert body["created_by"] is not None


def test_create_with_explicit_slug(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    slug = f"acme-explicit-{uuid.uuid4().hex[:6]}"
    body = _create(client, "Acme Explicit", slug=slug)
    cleanup_slugs.append(body["slug"])
    assert body["slug"] == slug


def test_create_with_conflicting_slug_appends_suffix(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    base = f"acme-collide-{uuid.uuid4().hex[:6]}"
    first = _create(client, "Acme", slug=base)
    second = _create(client, "Acme Two", slug=base)
    cleanup_slugs.extend([first["slug"], second["slug"]])
    assert first["slug"] == base
    assert second["slug"].startswith(base + "-")
    assert second["slug"] != base


def test_requires_x_user_id_header(client: TestClient) -> None:
    response = client.post("/engagements", json={"name": "no auth"})
    assert response.status_code == 401


def test_list_engagements_filters_by_status(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    active = _create(client, f"List active {uuid.uuid4().hex[:6]}")
    archived = _create(client, f"List archived {uuid.uuid4().hex[:6]}")
    cleanup_slugs.extend([active["slug"], archived["slug"]])

    client.delete(f"/engagements/{archived['slug']}", headers=_headers())

    response = client.get("/engagements", params={"status": "active"})
    assert response.status_code == 200
    slugs = {e["slug"] for e in response.json()}
    assert active["slug"] in slugs
    assert archived["slug"] not in slugs


def test_get_engagement_404_for_unknown_slug(client: TestClient) -> None:
    response = client.get(f"/engagements/does-not-exist-{uuid.uuid4().hex[:6]}")
    assert response.status_code == 404


def test_patch_renames_engagement(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, "Original")
    cleanup_slugs.append(eng["slug"])

    response = client.patch(
        f"/engagements/{eng['slug']}",
        json={"name": "Renamed"},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"


def test_patch_archive_then_unarchive_stamps_and_clears_archived_at(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, "Cycle")
    cleanup_slugs.append(eng["slug"])

    archived = client.patch(
        f"/engagements/{eng['slug']}",
        json={"status": "archived"},
        headers=_headers(),
    ).json()
    assert archived["status"] == "archived"
    assert archived["archived_at"] is not None

    unarchived = client.patch(
        f"/engagements/{eng['slug']}",
        json={"status": "active"},
        headers=_headers(),
    ).json()
    assert unarchived["status"] == "active"
    assert unarchived["archived_at"] is None


def test_patch_to_flushed_status_is_rejected(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, "No direct flush")
    cleanup_slugs.append(eng["slug"])

    response = client.patch(
        f"/engagements/{eng['slug']}",
        json={"status": "flushed"},
        headers=_headers(),
    )
    assert response.status_code == 400


def test_delete_soft_archives(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, "Soft archive me")
    cleanup_slugs.append(eng["slug"])

    response = client.delete(f"/engagements/{eng['slug']}", headers=_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "archived"
    assert body["archived_at"] is not None

    # Row still fetchable
    again = client.get(f"/engagements/{eng['slug']}")
    assert again.status_code == 200


def test_flush_removes_engagement_and_streams(
    client: TestClient,
    db: Session,
    redis_client: redis_lib.Redis,
) -> None:
    eng = _create(client, f"Flush me {uuid.uuid4().hex[:6]}")
    # Don't add to cleanup_slugs — we're flushing manually below.

    # Seed an inbound stream message so we can confirm the redis cleanup.
    redis_client.xadd(
        inbound_stream(uuid.UUID(eng["id"])),
        {"data": "{}"},
    )
    assert redis_client.exists(inbound_stream(uuid.UUID(eng["id"]))) == 1

    response = client.post(
        f"/engagements/{eng['slug']}/flush", headers=_headers()
    )
    assert response.status_code == 204

    # Engagement row is gone.
    gone = db.execute(
        select(Engagement.id).where(Engagement.slug == eng["slug"])
    ).scalar_one_or_none()
    assert gone is None

    # Stream is gone.
    assert redis_client.exists(inbound_stream(uuid.UUID(eng["id"]))) == 0


# ---------------------------------------------------------------------------
# Scope CRUD
# ---------------------------------------------------------------------------


def test_create_and_list_scope_items(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, "Scope holder")
    cleanup_slugs.append(eng["slug"])

    a = client.post(
        f"/engagements/{eng['slug']}/scope",
        json={"kind": "domain", "value": "acme.com"},
        headers=_headers(),
    )
    assert a.status_code == 201, a.text
    b = client.post(
        f"/engagements/{eng['slug']}/scope",
        json={
            "kind": "cidr",
            "value": "10.0.0.0/24",
            "is_exclusion": False,
            "note": "internal range",
        },
        headers=_headers(),
    )
    assert b.status_code == 201

    listing = client.get(f"/engagements/{eng['slug']}/scope")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 2
    values = {r["value"] for r in rows}
    assert values == {"acme.com", "10.0.0.0/24"}


def test_update_scope_item(
    client: TestClient, db: Session, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, "Scope edit")
    cleanup_slugs.append(eng["slug"])
    created = client.post(
        f"/engagements/{eng['slug']}/scope",
        json={"kind": "domain", "value": "acme.com"},
        headers=_headers(),
    ).json()

    response = client.patch(
        f"/engagements/{eng['slug']}/scope/{created['id']}",
        json={"value": "acme.org", "note": "renamed"},
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["value"] == "acme.org"
    assert body["note"] == "renamed"


def test_delete_scope_item(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, "Scope delete")
    cleanup_slugs.append(eng["slug"])
    created = client.post(
        f"/engagements/{eng['slug']}/scope",
        json={"kind": "domain", "value": "doomed.example.com"},
        headers=_headers(),
    ).json()

    response = client.delete(
        f"/engagements/{eng['slug']}/scope/{created['id']}",
        headers=_headers(),
    )
    assert response.status_code == 204

    listing = client.get(f"/engagements/{eng['slug']}/scope").json()
    assert listing == []


def test_scope_404_when_id_belongs_to_other_engagement(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    a = _create(client, f"A {uuid.uuid4().hex[:6]}")
    b = _create(client, f"B {uuid.uuid4().hex[:6]}")
    cleanup_slugs.extend([a["slug"], b["slug"]])

    item_a = client.post(
        f"/engagements/{a['slug']}/scope",
        json={"kind": "domain", "value": "a.com"},
        headers=_headers(),
    ).json()

    # Try to update under engagement b's slug — must 404.
    response = client.patch(
        f"/engagements/{b['slug']}/scope/{item_a['id']}",
        json={"value": "leaked.com"},
        headers=_headers(),
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def test_list_findings_unpacks_persisted_rows(
    client: TestClient, db: Session, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, f"Findings holder {uuid.uuid4().hex[:6]}")
    cleanup_slugs.append(eng["slug"])

    # Mirror what the worker's _persist_finding writes: tool data flattened into
    # details alongside the {thread_id, args} envelope.
    db.add(
        Finding(
            engagement_id=uuid.UUID(eng["id"]),
            title="dns_lookup → acme.com",
            severity=Severity.info,
            source_tool="dns_lookup",
            target="acme.com",
            details={
                "thread_id": "t-1",
                "args": {"domain": "acme.com"},
                "a": ["1.2.3.4"],
            },
        )
    )
    db.commit()

    response = client.get(f"/engagements/{eng['slug']}/findings")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "dns_lookup"
    assert row["target"] == "acme.com"
    assert row["thread_id"] == "t-1"
    assert row["args"] == {"domain": "acme.com"}
    # data is the details remainder after the envelope keys are popped.
    assert row["data"] == {"a": ["1.2.3.4"]}
    assert row["severity"] == "info"


def test_list_findings_empty_for_new_engagement(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, f"No findings {uuid.uuid4().hex[:6]}")
    cleanup_slugs.append(eng["slug"])
    response = client.get(f"/engagements/{eng['slug']}/findings")
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def test_run_endpoint_enqueues_run_start(
    client: TestClient,
    redis_client: redis_lib.Redis,
    cleanup_slugs: list[str],
) -> None:
    eng = _create(client, "Runnable")
    cleanup_slugs.append(eng["slug"])

    response = client.post(
        f"/engagements/{eng['slug']}/runs",
        json={"prompt": "enumerate acme.com"},
        headers=_headers(),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["engagement_id"] == eng["id"]
    assert body["events_stream"] == outbound_stream(uuid.UUID(eng["id"]))

    # Verify the envelope hit the inbound stream.
    queued = redis_client.xrange(inbound_stream(uuid.UUID(eng["id"])))
    assert len(queued) == 1
    payload = json.loads(queued[0][1]["data"])
    assert payload["type"] == "run.start"
    assert payload["thread_id"] == body["thread_id"]
    assert payload["prompt"] == "enumerate acme.com"


def test_run_endpoint_rejects_archived_engagement(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, "Archived no runs")
    cleanup_slugs.append(eng["slug"])
    client.delete(f"/engagements/{eng['slug']}", headers=_headers())

    response = client.post(
        f"/engagements/{eng['slug']}/runs",
        json={"prompt": "should be rejected"},
        headers=_headers(),
    )
    assert response.status_code == 409


def test_run_endpoint_404_for_unknown_engagement(client: TestClient) -> None:
    response = client.post(
        f"/engagements/does-not-exist-{uuid.uuid4().hex[:6]}/runs",
        json={"prompt": "..."},
        headers=_headers(),
    )
    assert response.status_code == 404


def test_run_endpoint_defaults_model_when_body_omits(
    client: TestClient,
    redis_client: redis_lib.Redis,
    cleanup_slugs: list[str],
) -> None:
    """Body without model => response + envelope echo the settings default."""
    eng = _create(client, "Default model")
    cleanup_slugs.append(eng["slug"])

    response = client.post(
        f"/engagements/{eng['slug']}/runs",
        json={"prompt": "go"},
        headers=_headers(),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["model"]["provider"] == settings.llm_provider
    assert body["model"]["name"]  # non-empty

    queued = redis_client.xrange(inbound_stream(uuid.UUID(eng["id"])))
    payload = json.loads(queued[-1][1]["data"])
    assert payload["model"] == body["model"]


def test_run_endpoint_passes_through_explicit_model(
    client: TestClient,
    redis_client: redis_lib.Redis,
    cleanup_slugs: list[str],
) -> None:
    """Body with model => envelope carries that exact model; redis cache populated."""
    eng = _create(client, "Explicit model")
    cleanup_slugs.append(eng["slug"])

    chosen = {"provider": "ollama", "name": "llama3.1:8b"}
    response = client.post(
        f"/engagements/{eng['slug']}/runs",
        json={"prompt": "go", "model": chosen},
        headers=_headers(),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["model"] == chosen

    payload = json.loads(
        redis_client.xrange(inbound_stream(uuid.UUID(eng["id"])))[-1][1]["data"]
    )
    assert payload["model"] == chosen

    cached = redis_client.hgetall(f"run:model:{body['thread_id']}")
    assert cached == chosen


def test_run_endpoint_rejects_when_provider_key_missing(
    client: TestClient,
    cleanup_slugs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic with no API key returns 400 before queueing."""
    from app.api import engagements as engagements_api

    # Patch the settings reference imported by the endpoint module so the
    # precheck sees an empty key without us having to mutate the real env.
    monkeypatch.setattr(
        engagements_api.settings, "anthropic_api_key", "", raising=False
    )

    eng = _create(client, "Missing key")
    cleanup_slugs.append(eng["slug"])

    response = client.post(
        f"/engagements/{eng['slug']}/runs",
        json={
            "prompt": "go",
            "model": {"provider": "anthropic", "name": "claude-opus-4-7"},
        },
        headers=_headers(),
    )
    assert response.status_code == 400
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_run_endpoint_rejects_when_provider_key_is_placeholder(
    client: TestClient,
    cleanup_slugs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bicep-installed PLACEHOLDER value is treated as missing."""
    from app.api import engagements as engagements_api

    monkeypatch.setattr(
        engagements_api.settings,
        "anthropic_api_key",
        "PLACEHOLDER-set-after-deploy",
        raising=False,
    )

    eng = _create(client, "Placeholder key")
    cleanup_slugs.append(eng["slug"])

    response = client.post(
        f"/engagements/{eng['slug']}/runs",
        json={
            "prompt": "go",
            "model": {"provider": "anthropic", "name": "claude-opus-4-7"},
        },
        headers=_headers(),
    )
    assert response.status_code == 400
