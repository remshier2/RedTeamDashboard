"""Drive the compiled OSINT graph for one inbound envelope.

``RunRunner`` owns the graph instance and the Redis publisher. Each call to
``handle()`` processes a single ``run.start`` or ``run.resume`` envelope:

1. Build the LangGraph input (initial state, or ``Command(resume=...)``).
2. Iterate ``graph.stream(...)`` and emit a lifecycle event for each finding
   and each interrupt that surfaces in the chunked state diffs.
3. After the stream finishes, emit ``run.completed`` (if the graph reached
   END) or rely on the in-stream ``approval.pending`` (if it interrupted).
4. Any uncaught exception becomes ``run.errored`` — the caller still acks the
   inbound message so we don't redeliver poison.

The graph and the session factory are injected so tests can wire a fake LLM
and a DB session that points at the compose Postgres.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from typing import Any

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ActorType,
    Approval,
    ApprovalStatus,
    AuditLog,
    Finding,
    RiskLevel,
    ScopeItem,
    Severity,
)
from app.orchestrator.scope import ScopeSnapshot
from app.runs.events import encode_event
from app.runs.streams import outbound_stream

logger = structlog.get_logger(__name__)


def _build_system_prompt(snapshots: list[ScopeSnapshot]) -> str:
    """Initial context the agent sees: who it is, what scope it operates in,
    and how to interpret bulk-scan phrasing.

    Listing the scope explicitly is what lets phrases like "enumerate all"
    actually fan out — the agent has no other way to discover what targets
    exist (the gate only enforces, it doesn't advertise)."""
    if snapshots:
        scope_lines = "\n".join(
            f"  - {item.kind.value} {item.value}"
            + (" (exclude)" if item.is_exclusion else "")
            for item in snapshots
        )
    else:
        scope_lines = "  (no scope items yet)"

    return (
        "You are a recon agent for an authorized red team engagement.\n"
        "\n"
        "Use the available tools to investigate targets within the engagement "
        "scope. Each tool call's target argument MUST match a scope item "
        "(exact value, or a subdomain of a domain-kind item, or an IP inside "
        "a CIDR-kind item).\n"
        "\n"
        "Engagement scope:\n"
        f"{scope_lines}\n"
        "\n"
        "When the operator's prompt says 'all', 'every', 'all targets', "
        "'whole scope', or names multiple targets, call the relevant tools "
        "once per include item — skip items marked (exclude).\n"
        "\n"
        "Per-call target argument: ALWAYS a single string. The tool schema "
        "declares the target arg as a string; passing a list (e.g. "
        '`["acme.com", "foo.com"]`) works as a courtesy fan-out for passive '
        "tools but is not the contract — prefer one call per target so each "
        "result is its own message in the conversation.\n"
        "\n"
        "Most tools are passive and run automatically. Some are ACTIVE: when "
        "you call one, the run pauses for a human operator to approve or deny "
        "it before it executes — this is expected, not an error. Active tools:\n"
        "  - portscan: a single host as `target` (an IP or a hostname, which "
        "is resolved to an IP) plus an optional `ports` string.\n"
        "  - subnet_sweep: an entire CIDR (up to a /24) as `cidr` plus an "
        "optional `ports` string — use this instead of calling portscan once "
        "per host when the operator wants a whole subnet swept.\n"
        "  - service_detect: identify the service/version on a host as "
        "`target` (IP or hostname) plus a `ports` string — pass the OPEN ports "
        "a prior portscan/subnet_sweep found to fingerprint what's running.\n"
        "\n"
        "Out-of-scope tool calls are automatically denied by the gate. Do "
        "not retry the same out-of-scope target.\n"
        "\n"
        "When you have gathered findings for every target the operator asked "
        "about, reply with a short summary (no tool_calls) to end the run."
    )

SessionFactory = Callable[[], Session]


GraphFactory = Callable[[Mapping[str, str] | None], Any]


class RunRunner:
    def __init__(
        self,
        *,
        graph: Any | None = None,
        graph_factory: GraphFactory | None = None,
        redis_client: Any,
        session_factory: SessionFactory,
    ) -> None:
        """One of ``graph`` or ``graph_factory`` is required.

        - ``graph``: a pre-compiled graph (tests). Used for every run
          regardless of the envelope's ``model`` field.
        - ``graph_factory``: builds a graph per run from the envelope's
          ``model``. Production wiring — lets each run pick its own LLM.
        """
        if (graph is None) == (graph_factory is None):
            raise ValueError("RunRunner requires exactly one of graph or graph_factory")
        self._graph = graph
        self._graph_factory = graph_factory
        self._redis = redis_client
        self._session_factory = session_factory

    def _resolve_graph(self, envelope: Mapping[str, Any]) -> Any:
        """Static graph for tests; factory-built per-run graph in prod."""
        if self._graph is not None:
            return self._graph
        model_raw = envelope.get("model")
        model: Mapping[str, str] | None = None
        if isinstance(model_raw, Mapping):
            model = {
                "provider": str(model_raw.get("provider", "")),
                "name": str(model_raw.get("name", "")),
            }
        assert self._graph_factory is not None  # noqa: S101 — invariant of __init__
        return self._graph_factory(model)

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def handle(self, engagement_id: uuid.UUID, envelope: Mapping[str, Any]) -> None:
        kind = envelope.get("type")
        thread_id = str(envelope.get("thread_id") or "")
        if not thread_id:
            raise ValueError("envelope missing thread_id")

        try:
            graph = self._resolve_graph(envelope)
            if kind == "run.start":
                self._start(engagement_id, thread_id, envelope, graph)
            elif kind == "run.resume":
                self._resume(engagement_id, thread_id, envelope, graph)
            else:
                raise ValueError(f"unknown envelope type: {kind!r}")
        except Exception as exc:
            logger.exception(
                "worker.handle_failed",
                engagement_id=str(engagement_id),
                thread_id=thread_id,
                error=str(exc),
            )
            self._audit(
                engagement_id,
                "run.errored",
                {"thread_id": thread_id, "error": str(exc)},
            )
            self._emit(
                engagement_id,
                {"type": "run.errored", "thread_id": thread_id, "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _start(
        self,
        engagement_id: uuid.UUID,
        thread_id: str,
        envelope: Mapping[str, Any],
        graph: Any,
    ) -> None:
        prompt = str(envelope.get("prompt") or "")
        snapshots = self._load_scope(engagement_id)
        model = envelope.get("model") if isinstance(envelope.get("model"), Mapping) else None
        logger.info(
            "worker.run_starting",
            engagement_id=str(engagement_id),
            thread_id=thread_id,
            prompt_len=len(prompt),
            scope_items=len(snapshots),
            model=model,
        )
        initial_state: dict[str, Any] = {
            "engagement_id": engagement_id,
            "messages": [
                SystemMessage(content=_build_system_prompt(snapshots)),
                HumanMessage(content=prompt),
            ],
            "scope_items": snapshots,
        }
        config = self._config(thread_id)

        started_payload: dict[str, Any] = {"thread_id": thread_id, "prompt": prompt}
        if model is not None:
            started_payload["model"] = dict(model)
        self._audit(engagement_id, "run.started", started_payload)
        self._emit(
            engagement_id,
            {"type": "run.started", **started_payload},
        )
        self._drive(engagement_id, thread_id, initial_state, config, graph)

    def _resume(
        self,
        engagement_id: uuid.UUID,
        thread_id: str,
        envelope: Mapping[str, Any],
        graph: Any,
    ) -> None:
        resume_value: dict[str, Any] = {"approved": bool(envelope.get("approved"))}
        if "edited_args" in envelope and isinstance(envelope["edited_args"], dict):
            resume_value["edited_args"] = envelope["edited_args"]
        if "reason" in envelope and envelope["reason"]:
            resume_value["reason"] = envelope["reason"]

        logger.info(
            "worker.run_resuming",
            engagement_id=str(engagement_id),
            thread_id=thread_id,
            approved=resume_value["approved"],
        )
        config = self._config(thread_id)
        self._drive(engagement_id, thread_id, Command(resume=resume_value), config, graph)

    # ------------------------------------------------------------------
    # Graph driving + event emission
    # ------------------------------------------------------------------

    def _drive(
        self,
        engagement_id: uuid.UUID,
        thread_id: str,
        input_: Any,
        config: dict[str, Any],
        graph: Any,
    ) -> None:
        for chunk in graph.stream(input_, config=config):
            for node in chunk:
                if node == "__interrupt__":
                    continue
                logger.info(
                    "worker.graph_step",
                    engagement_id=str(engagement_id),
                    thread_id=thread_id,
                    node=node,
                )
            self._emit_from_chunk(engagement_id, thread_id, chunk)

        snapshot = graph.get_state(config)
        if not snapshot.next:
            logger.info(
                "worker.run_completed",
                engagement_id=str(engagement_id),
                thread_id=thread_id,
            )
            self._audit(
                engagement_id,
                "run.completed",
                {"thread_id": thread_id},
            )
            self._emit(
                engagement_id,
                {"type": "run.completed", "thread_id": thread_id},
            )
        else:
            logger.info(
                "worker.run_interrupted",
                engagement_id=str(engagement_id),
                thread_id=thread_id,
                next_nodes=list(snapshot.next),
            )

    def _emit_from_chunk(
        self,
        engagement_id: uuid.UUID,
        thread_id: str,
        chunk: Mapping[str, Any],
    ) -> None:
        # langgraph yields a special ``__interrupt__`` chunk when interrupt()
        # fires. The payload is a tuple of Interrupt(value=...) objects.
        interrupts = chunk.get("__interrupt__")
        if interrupts:
            for interrupt_obj in interrupts:
                payload = getattr(interrupt_obj, "value", None)
                if not isinstance(payload, Mapping):
                    continue
                approval_id = self._persist_pending_approval(
                    engagement_id, thread_id, payload
                )
                self._emit(
                    engagement_id,
                    {
                        "type": "approval.pending",
                        "thread_id": thread_id,
                        "approval_id": str(approval_id),
                        "tool": payload.get("tool"),
                        "args": payload.get("args"),
                        "risk": payload.get("risk"),
                        "scope": payload.get("scope"),
                        "tool_call_id": payload.get("tool_call_id"),
                    },
                )
            return

        for _node_name, update in chunk.items():
            if not isinstance(update, Mapping):
                continue
            for finding in update.get("findings") or []:
                row = self._persist_finding(engagement_id, thread_id, finding)
                self._emit(
                    engagement_id,
                    {
                        "type": "finding.created",
                        "thread_id": thread_id,
                        "tool": finding.get("tool"),
                        "args": finding.get("args"),
                        "data": finding.get("data"),
                        "target": row.target,
                        "severity": row.severity.value,
                        "title": row.title,
                        "finding_id": str(row.id),
                    },
                )
            for denial in update.get("denials") or []:
                self._audit(
                    engagement_id,
                    "tool.denied",
                    {
                        "thread_id": thread_id,
                        "tool": denial.get("tool"),
                        "args": denial.get("args"),
                        "reason": denial.get("reason"),
                    },
                )
                self._emit(
                    engagement_id,
                    {
                        "type": "tool.denied",
                        "thread_id": thread_id,
                        "tool": denial.get("tool"),
                        "args": denial.get("args"),
                        "reason": denial.get("reason"),
                        "scope": denial.get("scope"),
                    },
                )
            for auto in update.get("auto_approvals") or []:
                # An active call auto-approved via a session grant must still be
                # logged with the covering authorization id (authz hard rule).
                self._audit(
                    engagement_id,
                    "tool.auto_approved",
                    {
                        "thread_id": thread_id,
                        "tool": auto.get("tool"),
                        "args": auto.get("args"),
                        "risk": auto.get("risk"),
                        "authorization_id": auto.get("authorization_id"),
                    },
                )
                self._emit(
                    engagement_id,
                    {
                        "type": "tool.auto_approved",
                        "thread_id": thread_id,
                        "tool": auto.get("tool"),
                        "args": auto.get("args"),
                        "risk": auto.get("risk"),
                        "authorization_id": auto.get("authorization_id"),
                    },
                )

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _emit(self, engagement_id: uuid.UUID, payload: dict[str, Any]) -> None:
        self._redis.xadd(outbound_stream(engagement_id), encode_event(payload))

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    def _persist_finding(
        self,
        engagement_id: uuid.UUID,
        thread_id: str,
        finding: Mapping[str, Any],
    ) -> Finding:
        tool = str(finding.get("tool") or "unknown")
        args = dict(finding.get("args") or {})
        data = dict(finding.get("data") or {})

        # Per-row target (e.g. "10.0.0.5:3389" for a portscan finding) wins; fall
        # back to the first string arg so passive tools that don't set a target
        # still get something searchable.
        target = finding.get("target") or next(
            (str(v) for v in args.values() if isinstance(v, (str, int))),
            None,
        )

        severity_raw = finding.get("severity") or "info"
        try:
            severity = Severity(severity_raw)
        except ValueError:
            severity = Severity.info

        title = finding.get("title") or (
            f"{tool} → {target}" if target else tool
        )

        with self._session_scope() as session:
            row = Finding(
                engagement_id=engagement_id,
                title=title,
                severity=severity,
                summary=None,
                details={"thread_id": thread_id, "args": args, **data},
                source_tool=tool,
                target=target,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def _audit(
        self,
        engagement_id: uuid.UUID | None,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        try:
            with self._session_scope() as session:
                session.add(
                    AuditLog(
                        engagement_id=engagement_id,
                        actor_type=ActorType.agent,
                        actor_id="worker",
                        event_type=event_type,
                        payload=dict(payload),
                    )
                )
                session.commit()
        except Exception:  # noqa: BLE001 — audit failures shouldn't kill a run
            logger.exception(
                "worker.audit_failed",
                engagement_id=str(engagement_id) if engagement_id else None,
                event_type=event_type,
            )

    def _persist_pending_approval(
        self,
        engagement_id: uuid.UUID,
        thread_id: str,
        payload: Mapping[str, Any],
    ) -> uuid.UUID:
        risk_raw = payload.get("risk")
        risk = RiskLevel(risk_raw) if risk_raw else RiskLevel.active
        with self._session_scope() as session:
            approval = Approval(
                engagement_id=engagement_id,
                thread_id=thread_id,
                node="tool_dispatch",
                tool_name=str(payload.get("tool") or ""),
                tool_args=dict(payload.get("args") or {}),
                risk=risk,
                scope_check=dict(payload.get("scope") or {}),
                status=ApprovalStatus.pending,
            )
            session.add(approval)
            session.commit()
            session.refresh(approval)
            logger.info(
                "worker.approval_persisted",
                engagement_id=str(engagement_id),
                thread_id=thread_id,
                approval_id=str(approval.id),
                tool=approval.tool_name,
                risk=risk.value,
            )
            return approval.id

    def _load_scope(self, engagement_id: uuid.UUID) -> list[ScopeSnapshot]:
        with self._session_scope() as session:
            rows: Iterable[ScopeItem] = session.execute(
                select(ScopeItem).where(ScopeItem.engagement_id == engagement_id)
            ).scalars()
            return [ScopeSnapshot.from_scope_item(item) for item in rows]

    @contextmanager
    def _session_scope(self):
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()
