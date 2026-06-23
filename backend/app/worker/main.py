"""Redis Streams consumer entrypoint.

Boots the compiled OSINT graph (ChatAnthropic-backed by default, swappable
via ``LLM_PROVIDER``), wires it into a ``RunRunner`` with a Postgres-backed
checkpointer so in-flight runs survive restarts, and spins the
``StreamConsumer`` poll loop until SIGTERM/SIGINT.
"""
from __future__ import annotations

import signal
import sys
import threading
from collections.abc import Mapping
from typing import Any

import redis as redis_lib
import structlog

from app.agents import StrategicAgent
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.orchestrator import build_graph
from app.orchestrator.llm import default_provider_model, make_llm
from app.worker.authz import make_db_authorizer
from app.worker.checkpoint import build_postgres_checkpointer
from app.worker.consumer import StreamConsumer
from app.worker.runner import RunRunner
from app.worker.strategic_consumer import StrategicConsumer

log = structlog.get_logger()


def main() -> None:
    configure_logging(settings.env)
    log.info("worker.start", env=settings.env, redis=settings.redis_url)

    redis_client = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
    checkpointer = build_postgres_checkpointer()
    authorizer = make_db_authorizer(SessionLocal)

    def graph_factory(
        model: Mapping[str, Any] | None,
        allowed_tools: list[str] | None = None,
        mcp_url: str | None = None,
        lease_token: str | None = None,
    ) -> object:
        """Build a fresh graph per run with the requested LLM and tool surface.

        Cheap — StateGraph compile is sub-millisecond. The LLM constructor
        is what costs (network handshake on first invoke), and we'd pay
        that anyway. Per-run rebuild lets each run pick its own provider.

        BYO-keys: ``api_key`` and ``endpoint`` arrive in ``model`` via the
        runner's per-envelope lookup (acting user's ``UserProviderKey``).
        When omitted (e.g. tests with raw envelopes), ``make_llm`` falls
        back to the SDK's env-var auto-detection.

        MCP leases: ``allowed_tools`` arrives from the runner's lease
        lookup. When present, we filter the global tool registry down to
        the lease's curated surface AND bind only those schemas onto the
        LLM (so the agent never proposes a tool outside the lease).

        Stage 1.5 — MCP execution: when ``mcp_url`` + ``lease_token`` are
        on the envelope AND a worker MCP API key is configured, build an
        MCP executor and pass it to the graph so tool calls run server-side
        over SSE. Without those (legacy / no lease / key not provisioned),
        the local IMPLEMENTATIONS registry runs the tools.
        """
        registry = None
        if allowed_tools is not None:
            from app.orchestrator.tools import all_tools

            registry = {
                spec.name: spec
                for spec in all_tools()
                if spec.name in allowed_tools
            }

        if model and model.get("provider") and model.get("name"):
            llm = make_llm(
                str(model["provider"]),
                str(model["name"]),
                api_key=model.get("api_key"),
                endpoint=model.get("endpoint"),
                registry=registry,
            )
        else:
            provider, model_name = default_provider_model()
            llm = make_llm(provider, model_name, registry=registry)

        mcp_executor = None
        if mcp_url and lease_token and settings.worker_mcp_api_key:
            from app.worker.mcp_executor import make_mcp_executor

            mcp_executor = make_mcp_executor(
                mcp_url,
                lease_token,
                api_key=settings.worker_mcp_api_key,
            )

        return build_graph(
            llm=llm,
            checkpointer=checkpointer,
            authorizer=authorizer,
            registry=registry,
            mcp_executor=mcp_executor,
        )

    runner = RunRunner(
        graph_factory=graph_factory,
        redis_client=redis_client,
        session_factory=SessionLocal,
    )
    consumer = StreamConsumer(
        runner=runner,
        redis_client=redis_client,
        session_factory=SessionLocal,
    )

    stop_event = threading.Event()

    def _shutdown(signum: int, _frame: object) -> None:
        log.info("worker.shutdown", signal=signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Phase 9: Strategic watcher subscribes to the outbound event stream and
    # runs on every finding.created. Lives in a sibling thread so the
    # existing run-command consumer in the main thread is untouched.
    strategic = StrategicConsumer(
        agent=StrategicAgent(),
        redis_client=redis_client,
        session_factory=SessionLocal,
    )
    strategic_thread = threading.Thread(
        target=strategic.run_forever,
        args=(stop_event,),
        name="strategic-watcher",
        daemon=True,
    )
    strategic_thread.start()

    consumer.run_forever(stop_event)
    strategic_thread.join(timeout=5.0)
    sys.exit(0)


if __name__ == "__main__":
    main()
