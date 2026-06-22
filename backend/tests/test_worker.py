"""End-to-end worker integration against the live compose Postgres + Redis.

Each test:
1. Inserts an Engagement (+ scope items) directly via SQLAlchemy.
2. Spins up a ``StreamConsumer`` in a thread, wired to a ``FakeLLM`` graph and
   a unique consumer-group name so it can't race with the compose worker.
3. Pushes a ``run.start`` (or ``run.resume``) envelope onto the inbound stream.
4. Reads the outbound stream until the expected terminal event arrives or the
   per-test deadline elapses.
5. Cleans up by calling the ``flush_engagement`` SECURITY DEFINER helper from
   migration 0001 and deleting the per-engagement Redis streams.
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
from langchain_core.messages import AIMessage
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import (
    Engagement,
    EngagementStatus,
    RiskLevel,
    ScopeItem,
    ScopeKind,
)
from app.orchestrator import ToolSpec, build_graph
from app.orchestrator.tools.runtime import ToolResult
from app.runs.events import encode_command
from app.runs.streams import inbound_stream, outbound_stream
from app.worker.consumer import StreamConsumer
from app.worker.runner import RunRunner
from tests._stub_tools import STUB_IMPLEMENTATIONS

# ---------------------------------------------------------------------------
# Fake LLM
# ---------------------------------------------------------------------------


class FakeLLM:
    def __init__(self, scripted: Iterable[AIMessage]) -> None:
        self._queue: list[AIMessage] = list(scripted)

    def invoke(self, _input: Any, _config: Any = None, **_kwargs: Any) -> AIMessage:
        if not self._queue:
            return AIMessage(content="(exhausted)")
        return self._queue.pop(0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def redis_client() -> Iterator[redis_lib.Redis]:
    client = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="worker-test",
        slug=f"worker-test-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.active,
    )
    db.add(eng)
    db.commit()
    db.refresh(eng)
    try:
        yield eng
    finally:
        # flush_engagement cascades scope items + bypasses the audit trigger.
        db.execute(text("SELECT flush_engagement(:id)"), {"id": eng.id})
        db.commit()


def _add_scope(db: Session, engagement_id: uuid.UUID, kind: ScopeKind, value: str) -> None:
    db.add(
        ScopeItem(
            engagement_id=engagement_id,
            kind=kind,
            value=value,
            is_exclusion=False,
        )
    )
    db.commit()


def _delete_streams(client: redis_lib.Redis, engagement_id: uuid.UUID) -> None:
    client.delete(inbound_stream(engagement_id), outbound_stream(engagement_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spin_worker(
    *,
    graph: Any,
    redis_client: redis_lib.Redis,
    engagement_id: uuid.UUID,
) -> tuple[StreamConsumer, threading.Thread, threading.Event]:
    """Start a StreamConsumer in a daemon thread with a unique consumer group,
    scoped to a single engagement so leftover rows from other tests can't
    feed messages into this thread's FakeLLM queue."""
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
    # Create consumer groups synchronously BEFORE the thread starts so the
    # test can xadd immediately without racing the worker's first refresh —
    # XGROUP CREATE ... ID $ skips messages added before group creation.
    consumer.refresh_streams()
    stop = threading.Event()
    thread = threading.Thread(target=consumer.run_forever, args=(stop,), daemon=True)
    thread.start()
    return consumer, thread, stop


def _collect_until(
    client: redis_lib.Redis,
    stream: str,
    terminal: set[str],
    *,
    deadline_s: float = 10.0,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    last_id = "0"
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        result = client.xread({stream: last_id}, block=250)
        if not result:
            continue
        for _stream_name, messages in result:
            for msg_id, fields in messages:
                last_id = msg_id
                payload = json.loads(fields["data"])
                events.append(payload)
        if any(e.get("type") in terminal for e in events):
            return events
    return events


def _stop(thread: threading.Thread, stop: threading.Event) -> None:
    stop.set()
    thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_passive_run_emits_lifecycle_events(
    db: Session, engagement: Engagement, redis_client: redis_lib.Redis
) -> None:
    _add_scope(db, engagement.id, ScopeKind.domain, "acme.com")

    llm = FakeLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "subfinder",
                        "args": {"domain": "acme.com"},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = build_graph(llm=llm, implementations=STUB_IMPLEMENTATIONS)
    _, thread, stop = _spin_worker(
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
                    "prompt": "enumerate acme.com",
                }
            ),
        )

        events = _collect_until(
            redis_client,
            outbound_stream(engagement.id),
            terminal={"run.completed", "run.errored"},
        )
    finally:
        _stop(thread, stop)
        _delete_streams(redis_client, engagement.id)

    types = [e["type"] for e in events]
    assert "run.started" in types
    assert "finding.created" in types
    assert "run.completed" in types
    finding = next(e for e in events if e["type"] == "finding.created")
    assert finding["tool"] == "subfinder"
    assert "www.acme.com" in finding["data"]["subdomains"]


def test_out_of_scope_call_emits_tool_denied(
    db: Session, engagement: Engagement, redis_client: redis_lib.Redis
) -> None:
    # No scope items — every tool call is out of scope.
    llm = FakeLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "subfinder",
                        "args": {"domain": "acme.com"},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(content="acknowledged denial"),
        ]
    )
    graph = build_graph(llm=llm, implementations=STUB_IMPLEMENTATIONS)
    _, thread, stop = _spin_worker(
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
                    "prompt": "enumerate",
                }
            ),
        )

        events = _collect_until(
            redis_client,
            outbound_stream(engagement.id),
            terminal={"run.completed", "run.errored"},
        )
    finally:
        _stop(thread, stop)
        _delete_streams(redis_client, engagement.id)

    types = [e["type"] for e in events]
    assert "tool.denied" in types
    denial = next(e for e in events if e["type"] == "tool.denied")
    assert denial["tool"] == "subfinder"
    assert "not in any scope item" in denial["reason"].lower()
    # And no findings were produced.
    assert all(e["type"] != "finding.created" for e in events)


def test_active_run_interrupts_then_resumes(
    db: Session, engagement: Engagement, redis_client: redis_lib.Redis
) -> None:
    _add_scope(db, engagement.id, ScopeKind.cidr, "10.0.0.0/24")

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

    llm = FakeLLM(
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
    _, thread, stop = _spin_worker(
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

        first = _collect_until(
            redis_client,
            outbound_stream(engagement.id),
            terminal={"approval.pending", "run.errored"},
        )
        assert any(e["type"] == "approval.pending" for e in first)
        pending = next(e for e in first if e["type"] == "approval.pending")
        assert pending["tool"] == "portscan"
        assert pending["risk"] == "active"

        # Approve and resume on the same thread.
        redis_client.xadd(
            inbound_stream(engagement.id),
            encode_command(
                {
                    "type": "run.resume",
                    "thread_id": thread_id,
                    "approved": True,
                }
            ),
        )

        all_events = _collect_until(
            redis_client,
            outbound_stream(engagement.id),
            terminal={"run.completed", "run.errored"},
            deadline_s=10.0,
        )
    finally:
        _stop(thread, stop)
        _delete_streams(redis_client, engagement.id)

    types = [e["type"] for e in all_events]
    assert "run.started" in types
    assert "approval.pending" in types
    assert "finding.created" in types
    assert "run.completed" in types
    finding = next(e for e in all_events if e["type"] == "finding.created")
    assert finding["data"]["open_ports"] == [22, 443]


# ---------------------------------------------------------------------------
# RunRunner factory mode (Phase 4: per-run model)
# ---------------------------------------------------------------------------


def test_run_runner_requires_exactly_one_of_graph_or_factory() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        RunRunner(
            redis_client=None,  # type: ignore[arg-type]
            session_factory=SessionLocal,
        )
    with pytest.raises(ValueError, match="exactly one"):
        RunRunner(
            graph=object(),
            graph_factory=lambda _m, _a=None: object(),
            redis_client=None,  # type: ignore[arg-type]
            session_factory=SessionLocal,
        )


def test_run_runner_calls_factory_with_envelope_model() -> None:
    received: list[tuple[Any, Any]] = []

    def factory(model: Any, allowed_tools: Any = None) -> Any:
        received.append((model, allowed_tools))
        return object()

    runner = RunRunner(
        graph_factory=factory,
        redis_client=None,  # type: ignore[arg-type]
        session_factory=SessionLocal,
    )
    runner._resolve_graph(
        {"type": "run.start", "model": {"provider": "anthropic", "name": "x"}}
    )
    runner._resolve_graph({"type": "run.start"})  # no model => None
    # Phase user-byo-keys-wireup: the runner now extends model with
    # api_key + endpoint fields populated from the acting user's
    # UserProviderKey lookup. Without acting_user_id on the envelope (this
    # test) both are None.
    # Phase mcp-leases: the runner now also resolves allowed_tools from
    # the envelope's lease_token. No token here → None (full registry).
    assert received == [
        (
            {
                "provider": "anthropic",
                "name": "x",
                "api_key": None,
                "endpoint": None,
            },
            None,
        ),
        (None, None),
    ]
