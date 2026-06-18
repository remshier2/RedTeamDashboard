"""Bulk scope-import: parser unit tests + HTTP surface tests.

The parser is the load-bearing piece — most of the coverage here is per-line
classification (domain / cidr / ip / url) plus the `!` exclusion and `#`
comment markers. The endpoint tests cover dry-run preview, real commit,
dedupe, and the archived-engagement guard.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.main import app
from app.models import Engagement, EngagementStatus, ScopeItem, ScopeKind
from app.services.scope_import import (
    detect_kind,
    parse_scope_text,
)

HDR = {"X-User-Id": "scope-import@example.com"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="Scope Import",
        slug=f"scope-import-{uuid.uuid4().hex[:8]}",
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


# ── detect_kind ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("acme.test", ScopeKind.domain),
        ("a.b.acme.test", ScopeKind.domain),
        ("10.0.0.5", ScopeKind.ip),
        ("2001:db8::1", ScopeKind.ip),
        ("10.0.0.0/24", ScopeKind.cidr),
        ("2001:db8::/32", ScopeKind.cidr),
        ("https://acme.test/login", ScopeKind.url),
        ("http://10.0.0.5:8080", ScopeKind.url),
    ],
)
def test_detect_kind_happy(value: str, expected: ScopeKind) -> None:
    assert detect_kind(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "not a domain",
        "999.999.999.999",
        "acme",  # no TLD
        "",
        "   ",
        "10.0.0.0/99",  # invalid CIDR
    ],
)
def test_detect_kind_rejects_garbage(value: str) -> None:
    assert detect_kind(value) is None


# ── parser ─────────────────────────────────────────────────────────────────


def test_parser_handles_mixed_list() -> None:
    text = """
# acme q3 in-scope
acme.test
*.acme.test
10.0.0.0/24
192.168.1.1
https://portal.acme.test

# exclusions
!10.0.0.5
!evil.acme.test
"""
    rows, errors = parse_scope_text(text)
    # *.acme.test fails the domain regex — that's fine, it's reported and skipped.
    assert any(e.raw == "*.acme.test" for e in errors)

    kinds = {(r.value, r.kind, r.is_exclusion) for r in rows}
    assert ("acme.test", ScopeKind.domain, False) in kinds
    assert ("10.0.0.0/24", ScopeKind.cidr, False) in kinds
    assert ("192.168.1.1", ScopeKind.ip, False) in kinds
    assert ("https://portal.acme.test", ScopeKind.url, False) in kinds
    assert ("10.0.0.5", ScopeKind.ip, True) in kinds
    assert ("evil.acme.test", ScopeKind.domain, True) in kinds


def test_parser_splits_csv_lines() -> None:
    rows, _ = parse_scope_text("acme.test, 10.0.0.0/24, https://x.acme.test\n")
    assert len(rows) == 3
    assert {r.kind for r in rows} == {
        ScopeKind.domain,
        ScopeKind.cidr,
        ScopeKind.url,
    }


def test_parser_reports_bare_bang() -> None:
    _, errors = parse_scope_text("!\n!  \n")
    assert len(errors) >= 1
    assert all("exclusion marker" in e.reason for e in errors)


def test_parser_strips_bom_and_crlf() -> None:
    rows, errors = parse_scope_text("﻿acme.test\r\n10.0.0.5\r\n")
    assert errors == []
    assert {r.value for r in rows} == {"acme.test", "10.0.0.5"}


# ── HTTP surface ───────────────────────────────────────────────────────────


def test_endpoint_dry_run_returns_preview_without_writing(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    blob = "acme.test\n10.0.0.0/24\nbogus\n"
    res = client.post(
        f"/engagements/{engagement.slug}/scope/import?dry_run=true",
        json={"text": blob},
        headers=HDR,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["would_create"] == 2
    assert len(body["preview"]) == 2
    assert len(body["errors"]) == 1

    count = db.execute(
        select(ScopeItem).where(ScopeItem.engagement_id == engagement.id)
    ).all()
    assert count == []


def test_endpoint_commit_creates_and_dedupes(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    db.add(
        ScopeItem(
            engagement_id=engagement.id,
            kind=ScopeKind.domain,
            value="acme.test",
            is_exclusion=False,
        )
    )
    db.commit()

    blob = "acme.test\n10.0.0.0/24\n!10.0.0.5\n"
    res = client.post(
        f"/engagements/{engagement.slug}/scope/import",
        json={"text": blob},
        headers=HDR,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["created"]) == 2  # CIDR + exclusion IP
    assert len(body["duplicates"]) == 1  # acme.test already present
    assert body["duplicates"][0]["value"] == "acme.test"

    rows = list(
        db.execute(
            select(ScopeItem).where(ScopeItem.engagement_id == engagement.id)
        ).scalars()
    )
    assert {r.value for r in rows} == {"acme.test", "10.0.0.0/24", "10.0.0.5"}


def test_endpoint_rejects_flushed_engagement(
    client: TestClient, db: Session
) -> None:
    eng = Engagement(
        name="Flushed",
        slug=f"flushed-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.flushed,
    )
    db.add(eng)
    db.commit()
    res = client.post(
        f"/engagements/{eng.slug}/scope/import",
        json={"text": "acme.test\n"},
        headers=HDR,
    )
    # _reject_flushed returns 410.
    assert res.status_code in (400, 409, 410), res.text
