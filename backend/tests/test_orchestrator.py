"""Phase 9 — Strategic + Tactical + orchestrator HTTP surface.

Covers:
- Strategic writes suggestions for proposed scan/enum tasks and silently
  drops any exploit proposal even if the LLM tries to suggest one.
- Strategic writes an AgentExecution row (model + status=completed) for each
  invocation.
- Tactical hard-refuses kind=exploit at the service layer.
- POST /findings/{id}/analyze returns the suggestions inline.
- POST /suggestions/{id}/accept mints a Task and dispatches when
  agent-eligible scan/enum; leaves the run on Redis for the worker.
- POST /suggestions/{id}/dismiss closes without minting a task.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents.strategic import StrategicAgent
from app.agents.tactical import TacticalAgent, TacticalRefusedExploit
from app.main import app
from app.models import (
    AgentName,
    AgentTrigger,
    Engagement,
    EngagementStatus,
    Finding,
    FindingPhase,
    FindingStatus,
    OwnerEligibility,
    ScopeItem,
    ScopeKind,
    Severity,
    Suggestion,
    SuggestionStatus,
    Task,
    TaskKind,
    TaskStatus,
)

HDR = {"X-User-Id": "phase9@example.com"}


# ── shared fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="Phase9 Orchestrator",
        slug=f"phase9-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.active,
        description="Strategic + Tactical wiring",
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
def finding(db: Session, engagement: Engagement) -> Finding:
    row = Finding(
        engagement_id=engagement.id,
        title="subfinder hit",
        severity=Severity.info,
        details={"hosts": ["a.acme.test", "b.acme.test"]},
        source_tool="subfinder",
        target="acme.test",
        phase=FindingPhase.osint,
        status=FindingStatus.pending_validation,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ── fake LLM that returns a fixed structured proposal ──────────────────────


class _FakeStructuredLLM:
    def __init__(self, tasks: list[dict[str, Any]], summary: str = "test summary") -> None:
        self._tasks = tasks
        self._summary = summary

    def invoke(self, _messages: Any) -> Any:
        # Return a Pydantic model matching the schema StrategicAgent expects.
        # We don't have the class here — the agent's structured-output wrapper
        # validates via model_validate when input isn't an instance, so a dict
        # is enough.
        return {"summary": self._summary, "tasks": self._tasks}


class _FakeChatLLM:
    """Stand-in for a langchain chat model. Captures messages then returns
    whatever ``_FakeStructuredLLM`` was configured with."""

    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self._tasks = tasks

    def with_structured_output(self, _schema: type[BaseModel]) -> _FakeStructuredLLM:
        return _FakeStructuredLLM(self._tasks)


# ── Strategic agent ────────────────────────────────────────────────────────


def test_strategic_writes_suggestions_and_execution(
    db: Session, engagement: Engagement, finding: Finding
) -> None:
    fake = _FakeChatLLM(
        tasks=[
            {
                "title": "Enumerate acme.test for more subdomains",
                "rationale": "subfinder only checked one source.",
                "kind": "enum",
                "owner_eligibility": "agent",
                "tool": "crt_sh",
                "target": "acme.test",
            },
            {
                "title": "Resolve discovered hosts",
                "rationale": "Map subdomains to IPs.",
                "kind": "enum",
                "owner_eligibility": "either",
                "tool": "dns_lookup",
                "target": "a.acme.test",
            },
        ]
    )
    agent = StrategicAgent(llm=fake, provider="test", model_name="fake-1")
    execution, suggestions = agent.analyze_finding(
        db, finding=finding, trigger=AgentTrigger.manual
    )
    db.commit()

    assert execution.agent == AgentName.strategic
    assert execution.status.value == "completed"
    assert execution.model_provider == "test"
    assert execution.model_name == "fake-1"
    assert len(suggestions) == 2
    for s in suggestions:
        assert s.created_by_agent == AgentName.strategic
        assert s.status == SuggestionStatus.open
        assert s.payload["tool"] in {"crt_sh", "dns_lookup"}


def test_strategic_drops_exploit_proposals(
    db: Session, engagement: Engagement, finding: Finding
) -> None:
    fake = _FakeChatLLM(
        tasks=[
            {
                "title": "RCE via outdated lib",
                "rationale": "exploit-as-test — must be dropped",
                "kind": "exploit",
                "owner_eligibility": "agent",
                "tool": "subfinder",
                "target": "acme.test",
            },
            {
                "title": "Probe with httpx",
                "rationale": "kept",
                "kind": "enum",
                "owner_eligibility": "agent",
                "tool": "httpx_probe",
                "target": "https://acme.test",
            },
        ]
    )
    agent = StrategicAgent(llm=fake, provider="test", model_name="fake-1")
    execution, suggestions = agent.analyze_finding(
        db, finding=finding, trigger=AgentTrigger.manual
    )
    db.commit()
    assert len(suggestions) == 1
    assert suggestions[0].payload["tool"] == "httpx_probe"
    assert execution.output["rejected_exploit_count"] == 1


# ── Tactical agent ─────────────────────────────────────────────────────────


class _FakeRedis:
    """Records xadd/hset calls so tests can assert on the dispatch envelope
    without standing up a real Redis."""

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


def test_tactical_refuses_exploit(db: Session, engagement: Engagement) -> None:
    task = Task(
        engagement_id=engagement.id,
        title="exploit attempt",
        kind=TaskKind.exploit,
        owner_eligibility=OwnerEligibility.either,
        status=TaskStatus.pending,
        payload={"tool": "portscan", "target": "1.2.3.4"},
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    tactical = TacticalAgent(_FakeRedis())
    with pytest.raises(TacticalRefusedExploit):
        tactical.dispatch(db, task=task)


def test_tactical_dispatches_scan_task(
    db: Session, engagement: Engagement
) -> None:
    task = Task(
        engagement_id=engagement.id,
        title="Subdomain enum",
        kind=TaskKind.enum,
        owner_eligibility=OwnerEligibility.agent,
        status=TaskStatus.pending,
        payload={"tool": "subfinder", "target": "acme.test"},
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    redis = _FakeRedis()
    thread_id = TacticalAgent(redis).dispatch(db, task=task)
    db.commit()

    assert task.status == TaskStatus.dispatched
    assert task.run_id == thread_id
    assert len(redis.xadd_calls) == 1
    stream, fields = redis.xadd_calls[0]
    assert stream.endswith(":in")
    import json

    envelope = json.loads(fields["data"])
    assert envelope["type"] == "run.start"
    assert "subfinder" in envelope["prompt"]
    assert "acme.test" in envelope["prompt"]


# ── HTTP surface ───────────────────────────────────────────────────────────


def test_analyze_endpoint_returns_suggestions(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    db: Session,
    finding: Finding,
) -> None:
    fake = _FakeChatLLM(
        tasks=[
            {
                "title": "DNS resolve",
                "rationale": "see what IPs are behind it",
                "kind": "enum",
                "owner_eligibility": "agent",
                "tool": "dns_lookup",
                "target": "a.acme.test",
            }
        ]
    )

    def _stub_make_chat_model(_provider: str, _name: str) -> Any:
        return fake

    monkeypatch.setattr(
        "app.agents.strategic._make_chat_model", _stub_make_chat_model
    )

    res = client.post(f"/findings/{finding.id}/analyze", headers=HDR)
    assert res.status_code == 200, res.text
    body = res.json()
    assert "execution_id" in body
    assert len(body["suggestions"]) == 1
    assert body["suggestions"][0]["status"] == "open"


def test_accept_dispatches_agent_eligible(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    db: Session,
    engagement: Engagement,
    finding: Finding,
) -> None:
    # Stub the Redis dependency so the worker doesn't actually get pinged.
    redis = _FakeRedis()
    from app.api import deps as deps_mod

    def _fake_redis_dep() -> Any:
        yield redis

    app.dependency_overrides[deps_mod.redis_client] = _fake_redis_dep
    try:
        suggestion = Suggestion(
            engagement_id=engagement.id,
            finding_id=finding.id,
            title="Probe",
            body="cheap recon",
            kind=__import__(
                "app.models", fromlist=["SuggestionKind"]
            ).SuggestionKind.task,
            payload={
                "tool": "dns_lookup",
                "target": "a.acme.test",
                "task_kind": "enum",
                "owner_eligibility": "agent",
            },
            status=SuggestionStatus.open,
            created_by_agent=AgentName.strategic,
        )
        db.add(suggestion)
        db.commit()
        db.refresh(suggestion)

        res = client.post(
            f"/suggestions/{suggestion.id}/accept", headers=HDR
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["suggestion"]["status"] == "accepted"
        assert body["task"] is not None
        assert body["dispatched"] is True
        assert len(redis.xadd_calls) == 1

        db.expire_all()
        task = (
            db.query(Task).filter(Task.id == uuid.UUID(body["task"]["id"])).one()
        )
        assert task.status == TaskStatus.dispatched
        assert task.kind == TaskKind.enum
    finally:
        app.dependency_overrides.pop(deps_mod.redis_client, None)


def test_dismiss_closes_suggestion(
    client: TestClient,
    db: Session,
    engagement: Engagement,
) -> None:
    from app.models import SuggestionKind

    suggestion = Suggestion(
        engagement_id=engagement.id,
        title="Not useful",
        kind=SuggestionKind.task,
        payload={
            "tool": "subfinder",
            "target": "acme.test",
            "task_kind": "enum",
            "owner_eligibility": "agent",
        },
        status=SuggestionStatus.open,
        created_by_agent=AgentName.strategic,
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)

    res = client.post(f"/suggestions/{suggestion.id}/dismiss", headers=HDR)
    assert res.status_code == 200
    assert res.json()["status"] == "dismissed"

    db.expire_all()
    s = db.query(Suggestion).filter(Suggestion.id == suggestion.id).one()
    assert s.status == SuggestionStatus.dismissed
