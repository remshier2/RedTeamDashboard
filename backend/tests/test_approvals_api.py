"""Approvals HTTP API + worker round-trip.

The unit-shaped tests exercise list / GET / POST decision against a directly
seeded ``Approval`` row, with no worker in the loop. ``test_full_round_trip``
spins up the consumer in a thread and proves the complete dance:
``run.start`` → graph interrupt → Approval row → ``approval.pending`` event →
``POST /approvals/{id}/decision`` → ``run.resume`` → ``finding.created`` →
``run.completed``.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from typing import Any

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Approval,
    ApprovalStatus,
    Engagement,
    EngagementStatus,
    RiskLevel,
    ScopeItem,
    ScopeKind,
    User,
)
from app.orchestrator import ToolSpec, build_graph
from app.orchestrator.tools.runtime import ToolResult
from app.runs.events import encode_command
from app.runs.streams import inbound_stream, outbound_stream
from app.worker.consumer import StreamConsumer
from app.worker.runner import RunRunner

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
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="approvals-test",
        slug=f"approvals-test-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.active,
    )
    db.add(eng)
    db.commit()
    db.refresh(eng)
    try:
        yield eng
    finally:
        # Wipe any users we created during the test that referenced this
        # engagement — flush_engagement only handles audit_log and engagements.
        db.execute(
            text("DELETE FROM approvals WHERE engagement_id = :id"),
            {"id": eng.id},
        )
        db.commit()
        db.execute(text("SELECT flush_engagement(:id)"), {"id": eng.id})
        db.commit()


def _pending_approval(
    db: Session,
    engagement_id: uuid.UUID,
    *,
    thread_id: str | None = None,
) -> Approval:
    approval = Approval(
        engagement_id=engagement_id,
        thread_id=thread_id or str(uuid.uuid4()),
        node="tool_dispatch",
        tool_name="portscan",
        tool_args={"ip": "10.0.0.5"},
        risk=RiskLevel.active,
        scope_check={
            "ok": True,
            "target": "10.0.0.5",
            "reason": "in scope",
        },
        status=ApprovalStatus.pending,
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval


# ---------------------------------------------------------------------------
# List + GET
# ---------------------------------------------------------------------------


def test_list_pending_approvals_returns_only_pending(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    pending = _pending_approval(db, engagement.id)
    decided = _pending_approval(db, engagement.id)
    decided.status = ApprovalStatus.approved
    db.commit()

    response = client.get(
        f"/engagements/{engagement.id}/approvals",
        params={"status": "pending"},
    )
    assert response.status_code == 200
    rows = response.json()
    assert [r["id"] for r in rows] == [str(pending.id)]


def test_get_single_approval(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    approval = _pending_approval(db, engagement.id)
    response = client.get(f"/approvals/{approval.id}")
    assert response.status_code == 200
    assert response.json()["id"] == str(approval.id)


def test_get_missing_approval_is_404(client: TestClient) -> None:
    response = client.get(f"/approvals/{uuid.uuid4()}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def test_decision_approves_updates_row_and_pushes_resume(
    client: TestClient,
    db: Session,
    engagement: Engagement,
    redis_client: redis_lib.Redis,
) -> None:
    approval = _pending_approval(db, engagement.id)
    # Drain anything that may have been left on this engagement's input stream.
    redis_client.delete(inbound_stream(engagement.id))

    response = client.post(
        f"/approvals/{approval.id}/decision",
        headers={"X-User-Id": "analyst@example.com"},
        json={"approved": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["decided_by"] is not None
    assert body["decided_at"] is not None

    db.expire_all()
    db.refresh(approval)
    assert approval.status is ApprovalStatus.approved
    assert approval.decision_args == {"approved": True}

    # A run.resume envelope should now be sitting on the inbound stream.
    queued = redis_client.xrange(inbound_stream(engagement.id))
    assert len(queued) == 1
    payload = json.loads(queued[0][1]["data"])
    assert payload == {
        "type": "run.resume",
        "thread_id": approval.thread_id,
        "approved": True,
    }


def test_decision_with_edited_args_marks_status_edited(
    client: TestClient,
    db: Session,
    engagement: Engagement,
    redis_client: redis_lib.Redis,
) -> None:
    approval = _pending_approval(db, engagement.id)
    redis_client.delete(inbound_stream(engagement.id))

    response = client.post(
        f"/approvals/{approval.id}/decision",
        headers={"X-User-Id": "analyst@example.com"},
        json={"approved": True, "edited_args": {"ip": "10.0.0.6"}},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "edited"

    queued = redis_client.xrange(inbound_stream(engagement.id))
    payload = json.loads(queued[0][1]["data"])
    assert payload["edited_args"] == {"ip": "10.0.0.6"}


def test_decision_denies(
    client: TestClient,
    db: Session,
    engagement: Engagement,
    redis_client: redis_lib.Redis,
) -> None:
    approval = _pending_approval(db, engagement.id)
    redis_client.delete(inbound_stream(engagement.id))

    response = client.post(
        f"/approvals/{approval.id}/decision",
        headers={"X-User-Id": "analyst@example.com"},
        json={"approved": False, "reason": "out of window"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "denied"

    queued = redis_client.xrange(inbound_stream(engagement.id))
    payload = json.loads(queued[0][1]["data"])
    assert payload["approved"] is False
    assert payload["reason"] == "out of window"


def test_decision_resume_carries_cached_model_when_present(
    client: TestClient,
    db: Session,
    engagement: Engagement,
    redis_client: redis_lib.Redis,
) -> None:
    """Approvals reuse the run's original LLM choice on resume."""
    approval = _pending_approval(db, engagement.id)
    redis_client.delete(inbound_stream(engagement.id))
    redis_client.hset(
        f"run:model:{approval.thread_id}",
        mapping={"provider": "anthropic", "name": "claude-sonnet-4-6"},
    )

    response = client.post(
        f"/approvals/{approval.id}/decision",
        headers={"X-User-Id": "analyst@example.com"},
        json={"approved": True},
    )
    assert response.status_code == 200

    payload = json.loads(
        redis_client.xrange(inbound_stream(engagement.id))[-1][1]["data"]
    )
    assert payload["model"] == {"provider": "anthropic", "name": "claude-sonnet-4-6"}


def test_decision_on_already_decided_returns_409(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    approval = _pending_approval(db, engagement.id)
    approval.status = ApprovalStatus.approved
    db.commit()

    response = client.post(
        f"/approvals/{approval.id}/decision",
        headers={"X-User-Id": "analyst@example.com"},
        json={"approved": True},
    )
    assert response.status_code == 409


def test_decision_requires_x_user_id_header(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    approval = _pending_approval(db, engagement.id)
    response = client.post(
        f"/approvals/{approval.id}/decision",
        json={"approved": True},
    )
    assert response.status_code == 401


def test_x_user_id_upserts_user_by_email(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    approval = _pending_approval(db, engagement.id)
    email = f"new-analyst-{uuid.uuid4().hex[:6]}@example.com"

    response = client.post(
        f"/approvals/{approval.id}/decision",
        headers={"X-User-Id": email},
        json={"approved": True},
    )
    assert response.status_code == 200

    created = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    assert created is not None
    assert response.json()["decided_by"] == str(created.id)


def test_invalid_x_user_id_is_400(
    client: TestClient, db: Session, engagement: Engagement
) -> None:
    approval = _pending_approval(db, engagement.id)
    response = client.post(
        f"/approvals/{approval.id}/decision",
        headers={"X-User-Id": "not-a-uuid-or-email"},
        json={"approved": True},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Session authorizations (remember-for-session grants)
# ---------------------------------------------------------------------------


def test_decision_remember_for_session_creates_grant(
    client: TestClient,
    db: Session,
    engagement: Engagement,
    redis_client: redis_lib.Redis,
) -> None:
    approval = _pending_approval(db, engagement.id)
    redis_client.delete(inbound_stream(engagement.id))

    response = client.post(
        f"/approvals/{approval.id}/decision",
        headers={"X-User-Id": "analyst@example.com"},
        json={"approved": True, "remember_for_session": True},
    )
    assert response.status_code == 200
    assert response.json()["authorization_id"] is not None

    grants = client.get(
        f"/engagements/{engagement.id}/authorizations",
        params={"active": "true"},
    ).json()
    assert len(grants) == 1
    assert grants[0]["tool_name"] == "portscan"
    assert grants[0]["revoked_at"] is None
    assert grants[0]["id"] == response.json()["authorization_id"]


def test_decision_without_remember_creates_no_grant(
    client: TestClient,
    db: Session,
    engagement: Engagement,
    redis_client: redis_lib.Redis,
) -> None:
    approval = _pending_approval(db, engagement.id)
    redis_client.delete(inbound_stream(engagement.id))

    client.post(
        f"/approvals/{approval.id}/decision",
        headers={"X-User-Id": "analyst@example.com"},
        json={"approved": True},
    )
    grants = client.get(f"/engagements/{engagement.id}/authorizations").json()
    assert grants == []


def test_remember_for_session_reuses_existing_active_grant(
    client: TestClient,
    db: Session,
    engagement: Engagement,
    redis_client: redis_lib.Redis,
) -> None:
    redis_client.delete(inbound_stream(engagement.id))
    first = _pending_approval(db, engagement.id)
    second = _pending_approval(db, engagement.id)

    a = client.post(
        f"/approvals/{first.id}/decision",
        headers={"X-User-Id": "analyst@example.com"},
        json={"approved": True, "remember_for_session": True},
    ).json()
    b = client.post(
        f"/approvals/{second.id}/decision",
        headers={"X-User-Id": "analyst@example.com"},
        json={"approved": True, "remember_for_session": True},
    ).json()

    # Both approvals point at the SAME grant; no duplicate active row.
    assert a["authorization_id"] == b["authorization_id"]
    grants = client.get(
        f"/engagements/{engagement.id}/authorizations",
        params={"active": "true"},
    ).json()
    assert len(grants) == 1


def test_revoke_authorization(
    client: TestClient,
    db: Session,
    engagement: Engagement,
    redis_client: redis_lib.Redis,
) -> None:
    approval = _pending_approval(db, engagement.id)
    redis_client.delete(inbound_stream(engagement.id))
    client.post(
        f"/approvals/{approval.id}/decision",
        headers={"X-User-Id": "analyst@example.com"},
        json={"approved": True, "remember_for_session": True},
    )
    grant_id = client.get(
        f"/engagements/{engagement.id}/authorizations",
        params={"active": "true"},
    ).json()[0]["id"]

    revoked = client.post(
        f"/authorizations/{grant_id}/revoke",
        headers={"X-User-Id": "analyst@example.com"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None

    assert (
        client.get(
            f"/engagements/{engagement.id}/authorizations",
            params={"active": "true"},
        ).json()
        == []
    )


# ---------------------------------------------------------------------------
# End-to-end: worker + API + Redis
# ---------------------------------------------------------------------------


class _FakeLLM:
    def __init__(self, scripted: Iterable[AIMessage]) -> None:
        self._queue: list[AIMessage] = list(scripted)

    def invoke(self, _input: Any, _config: Any = None, **_kwargs: Any) -> AIMessage:
        if not self._queue:
            return AIMessage(content="(exhausted)")
        return self._queue.pop(0)


def _spin_worker(
    *, graph: Any, redis_client: redis_lib.Redis, engagement_id: uuid.UUID
) -> tuple[threading.Thread, threading.Event]:
    runner = RunRunner(
        graph=graph,
        redis_client=redis_client,
        session_factory=SessionLocal,
    )
    consumer = StreamConsumer(
        runner=runner,
        redis_client=redis_client,
        session_factory=SessionLocal,
        consumer_group=f"test-{uuid.uuid4().hex[:8]}",
        refresh_interval=0.5,
        engagement_ids=[engagement_id],
    )
    consumer.refresh_streams()
    stop = threading.Event()
    thread = threading.Thread(target=consumer.run_forever, args=(stop,), daemon=True)
    thread.start()
    return thread, stop


def _collect_until(
    client_: redis_lib.Redis,
    stream: str,
    terminal: set[str],
    *,
    start_id: str = "0",
    deadline_s: float = 10.0,
) -> tuple[list[dict[str, Any]], str]:
    events: list[dict[str, Any]] = []
    last_id = start_id
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        result = client_.xread({stream: last_id}, block=250)
        if not result:
            continue
        for _stream_name, messages in result:
            for msg_id, fields in messages:
                last_id = msg_id
                events.append(json.loads(fields["data"]))
        if any(e.get("type") in terminal for e in events):
            return events, last_id
    return events, last_id


def test_full_round_trip(
    client: TestClient,
    db: Session,
    engagement: Engagement,
    redis_client: redis_lib.Redis,
) -> None:
    db.add(
        ScopeItem(
            engagement_id=engagement.id,
            kind=ScopeKind.cidr,
            value="10.0.0.0/24",
            is_exclusion=False,
        )
    )
    db.commit()

    portscan = ToolSpec(
        name="portscan",
        risk=RiskLevel.active,
        target_arg="ip",
        kind=ScopeKind.ip,
        description="Aggressive TCP port scan.",
    )
    registry = {"portscan": portscan}
    impls = {
        "portscan": lambda args: ToolResult(
            ok=True, data={"ip": args["ip"], "open_ports": [22, 443]}
        ),
    }
    llm = _FakeLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "portscan",
                        "args": {"ip": "10.0.0.5"},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(content="scan complete"),
        ]
    )
    graph = build_graph(llm=llm, registry=registry, implementations=impls)

    thread, stop = _spin_worker(
        graph=graph,
        redis_client=redis_client,
        engagement_id=engagement.id,
    )
    try:
        thread_id = str(uuid.uuid4())
        redis_client.xadd(
            inbound_stream(engagement.id),
            encode_command(
                {
                    "type": "run.start",
                    "thread_id": thread_id,
                    "prompt": "scan 10.0.0.5",
                }
            ),
        )

        events, last_id = _collect_until(
            redis_client,
            outbound_stream(engagement.id),
            terminal={"approval.pending", "run.errored"},
        )
        pending = next(e for e in events if e["type"] == "approval.pending")
        approval_id = pending["approval_id"]
        assert pending["tool"] == "portscan"

        response = client.post(
            f"/approvals/{approval_id}/decision",
            headers={"X-User-Id": "analyst@example.com"},
            json={"approved": True},
        )
        assert response.status_code == 200, response.text

        final, _ = _collect_until(
            redis_client,
            outbound_stream(engagement.id),
            terminal={"run.completed", "run.errored"},
            start_id=last_id,
        )

        types = [e["type"] for e in final]
        assert "finding.created" in types
        assert "run.completed" in types
        finding = next(e for e in final if e["type"] == "finding.created")
        assert finding["data"]["open_ports"] == [22, 443]
    finally:
        stop.set()
        thread.join(timeout=5.0)
        redis_client.delete(
            inbound_stream(engagement.id), outbound_stream(engagement.id)
        )
