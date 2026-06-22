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

    def graph_factory(model: Mapping[str, str] | None) -> object:
        """Build a fresh graph per run with the requested LLM.

        Cheap — StateGraph compile is sub-millisecond. The LLM constructor
        is what costs (network handshake on first invoke), and we'd pay
        that anyway. Per-run rebuild lets each run pick its own provider.
        """
        if model and model.get("provider") and model.get("name"):
            llm = make_llm(model["provider"], model["name"])
        else:
            provider, model_name = default_provider_model()
            llm = make_llm(provider, model_name)
        return build_graph(
            llm=llm,
            checkpointer=checkpointer,
            authorizer=authorizer,
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
