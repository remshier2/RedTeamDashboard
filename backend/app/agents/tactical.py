"""Tactical manager — dispatches agent-eligible Tasks to the worker.

This agent dispatches enumeration and scanning tasks during **authorized security
engagements**. It enforces the charter invariant that **agents scan, analysts validate**.

**Charter:** Only agent-eligible tasks (scan/enum) are dispatched. Validation and
proof-of-concept work (``TaskKind.exploit``) is **analyst-only** — refused at the
service boundary.

Slice 1 (Phase 9): deterministic dispatcher. Pulls (tool, target) from
``task.payload`` (set by Strategic when the suggestion was accepted) and
publishes a ``run.start`` envelope on the engagement's inbound stream. The
worker's existing graph + approval gate handles everything from there.

HARD INVARIANT: ``TaskKind.exploit`` is refused at the service boundary. The
CHARTER decided agents scan, analysts exploit. ``TacticalRefusedExploit``
is raised so the API layer can map it to a 4xx and the caller knows the
refusal is by design, not by misconfiguration.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentName,
    AgentTrigger,
    OwnerEligibility,
    Task,
    TaskKind,
    TaskStatus,
)
from app.orchestrator.llm import default_provider_model
from app.orchestrator.tools import get_tool
from app.runs.events import encode_command
from app.runs.streams import inbound_stream, store_run_model

logger = structlog.get_logger(__name__)


class TacticalRefusedExploit(Exception):
    """Tactical was asked to dispatch a kind=exploit task. Agents scan,
    analysts exploit (CHARTER invariant). The HTTP layer maps this to 400
    so the analyst sees a deliberate refusal, not a generic error."""


class TacticalAgent:
    """Dispatcher that turns an accepted Task into a worker run."""

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    def dispatch(
        self,
        session: Session,
        *,
        task: Task,
        trigger: AgentTrigger = AgentTrigger.manual,
    ) -> uuid.UUID:
        """Dispatch ``task`` as a worker run; return the new ``thread_id``.

        Caller commits the session. The function mutates ``task`` (status,
        dispatched_at, run_id) and adds an ``AgentExecution`` row to record
        the dispatch decision.
        """
        if task.kind == TaskKind.exploit:
            raise TacticalRefusedExploit(
                "tactical refuses exploit tasks — agents scan, analysts exploit"
            )
        if task.owner_eligibility == OwnerEligibility.analyst:
            raise ValueError(
                f"task {task.id} is analyst-only; tactical cannot dispatch"
            )
        if task.status != TaskStatus.pending:
            raise ValueError(
                f"task {task.id} is already {task.status.value}; refusing to redispatch"
            )

        tool_name = task.payload.get("tool")
        target = task.payload.get("target")
        if not (tool_name and target):
            raise ValueError(
                f"task {task.id} payload missing tool/target: {task.payload!r}"
            )

        spec = get_tool(tool_name)
        if spec is None:
            raise ValueError(f"task {task.id} references unknown tool {tool_name!r}")

        prompt = (
            f"Use the {tool_name} tool with {spec.target_arg}={target!r}. "
            "Report exactly what the tool returns; do not call any other tool."
        )

        provider, model_name = default_provider_model()
        thread_id = uuid.uuid4()

        # Stage 1 of per-task MCP composition: mint a lease for this dispatch
        # so the Execution Agent gets the curated tool/context/prompt surface
        # Strategic chose for this TaskKind. The lease id is the bearer token.
        from app.agents.strategic import StrategicAgent
        from app.core.config import settings

        lease = StrategicAgent().provision_lease(session, task=task)

        # Stage 2 routing: when Strategic marked the lease as needing an
        # isolated MCP host AND the deployment has provisioned a secondary
        # scale-to-zero MCP App, point the worker there. Otherwise use the
        # colocated MCP server in the backend container. The local-dev
        # default (``aca_mcp_app_enabled=False``) collapses both paths to
        # colocated so we don't fork the local stack for an Azure-only
        # feature.
        if (
            lease.requires_container
            and settings.aca_mcp_app_enabled
            and settings.aca_mcp_url
        ):
            mcp_url = f"{settings.aca_mcp_url.rstrip('/')}/mcp"
            mcp_host = "container"
        else:
            mcp_url = f"{settings.public_base_url.rstrip('/')}/mcp"
            mcp_host = "colocated"

        store_run_model(
            self._redis,
            thread_id,
            provider=provider,
            model_name=model_name,
        )
        self._redis.xadd(
            inbound_stream(task.engagement_id),
            encode_command(
                {
                    "type": "run.start",
                    "thread_id": str(thread_id),
                    "prompt": prompt,
                    "model": {"provider": provider, "name": model_name},
                    "mcp_url": mcp_url,
                    "lease_token": str(lease.id),
                }
            ),
        )

        now = datetime.now(tz=UTC)
        execution = AgentExecution(
            engagement_id=task.engagement_id,
            agent=AgentName.tactical,
            trigger=trigger,
            input={
                "task_id": str(task.id),
                "tool": tool_name,
                "target": target,
            },
            output={"thread_id": str(thread_id), "prompt": prompt},
            model_provider=provider,
            model_name=model_name,
            status=AgentExecutionStatus.completed,
            started_at=now,
            completed_at=now,
        )
        session.add(execution)

        task.status = TaskStatus.dispatched
        task.dispatched_at = now
        task.run_id = thread_id

        logger.info(
            "tactical.dispatched",
            task_id=str(task.id),
            tool=tool_name,
            target=target,
            thread_id=str(thread_id),
            mcp_host=mcp_host,
            lease_requires_container=lease.requires_container,
        )

        return thread_id
