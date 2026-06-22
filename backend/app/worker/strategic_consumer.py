"""Strategic watcher consumer loop.

A second Redis Streams consumer that lives alongside the existing
``StreamConsumer``. Instead of reading the *inbound* per-engagement command
streams, this one reads the *outbound* event streams (``runs:{eid}:events``)
under a NEW consumer group (``strategic-watcher``) so it doesn't compete with
the SSE endpoint or with other workers' delivery of inbound commands.

For every ``finding.created`` envelope it sees, it loads the persisted
``Finding`` row and asks ``StrategicAgent`` to propose next-step suggestions.
Strategic writes ``Suggestion`` rows the analyst will see in the findings
slide-over. Nothing dispatches until the analyst accepts — pure watcher.

Failure handling: a poison envelope is logged + acked. An LLM-call failure
inside Strategic is caught upstream in ``StrategicAgent``; the execution row
gets ``status=failed`` and we move on.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from redis.exceptions import ResponseError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import StrategicAgent
from app.models import AgentTrigger, Engagement, EngagementStatus, Finding
from app.runs.streams import outbound_stream

logger = structlog.get_logger(__name__)

SessionFactory = Callable[[], Session]

STRATEGIC_GROUP = "strategic-watcher"


class StrategicConsumer:
    def __init__(
        self,
        *,
        agent: StrategicAgent,
        redis_client: Any,
        session_factory: SessionFactory,
        consumer_group: str = STRATEGIC_GROUP,
        consumer_name: str | None = None,
        refresh_interval: float = 5.0,
    ) -> None:
        self._agent = agent
        self._redis = redis_client
        self._session_factory = session_factory
        self._group = consumer_group
        self._consumer = consumer_name or f"strategic-{uuid.uuid4().hex[:8]}"
        self._refresh_interval = refresh_interval
        self._known_streams: set[str] = set()
        self._last_refresh = 0.0

    def _active_engagement_ids(self) -> list[uuid.UUID]:
        session = self._session_factory()
        try:
            return list(
                session.execute(
                    select(Engagement.id).where(
                        Engagement.status == EngagementStatus.active
                    )
                ).scalars()
            )
        finally:
            session.close()

    def _ensure_group(self, stream: str) -> None:
        try:
            # MKSTREAM so we don't race the producer; id="$" means we only see
            # NEW events — historical findings shouldn't trigger Strategic.
            self._redis.xgroup_create(stream, self._group, id="$", mkstream=True)
            logger.info(
                "strategic.group_created", stream=stream, group=self._group
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def refresh_streams(self) -> set[str]:
        streams = {outbound_stream(eid) for eid in self._active_engagement_ids()}
        for s in streams - self._known_streams:
            self._ensure_group(s)
        self._known_streams = streams
        self._last_refresh = time.time()
        return streams

    def run_once(self, *, block_ms: int = 1000) -> int:
        if time.time() - self._last_refresh > self._refresh_interval:
            self.refresh_streams()

        if not self._known_streams:
            time.sleep(min(block_ms / 1000.0, 0.5))
            return 0

        try:
            response = self._redis.xreadgroup(
                self._group,
                self._consumer,
                {s: ">" for s in self._known_streams},
                count=10,
                block=block_ms,
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                # The outbound stream was deleted (engagement flushed). Forget
                # everything and let the next refresh recreate as needed.
                logger.warning("strategic.nogroup_recovering", error=str(exc))
                self._known_streams = set()
                self._last_refresh = 0.0
                return 0
            raise

        processed = 0
        for stream_name, messages in response or []:
            for msg_id, fields in messages:
                self._process_one(stream_name, msg_id, fields)
                processed += 1
        return processed

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.run_once(block_ms=1000)
            except Exception:
                logger.exception("strategic.iteration_failed")
                time.sleep(1.0)

    def _process_one(
        self,
        stream_name: str,
        msg_id: str,
        fields: dict[str, Any],
    ) -> None:
        try:
            raw = fields.get("data") or fields.get(b"data")
            if raw is None:
                logger.warning(
                    "strategic.envelope_missing_data",
                    stream=stream_name,
                    msg_id=msg_id,
                )
                return
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            envelope = json.loads(raw)
            if envelope.get("type") != "finding.created":
                # Other event types (run.started, approval.pending, etc.) are
                # not interesting to Strategic. Skip silently.
                return
            finding_id_raw = envelope.get("finding_id")
            if not finding_id_raw:
                logger.warning(
                    "strategic.finding_event_missing_id",
                    stream=stream_name,
                    msg_id=msg_id,
                )
                return
            self._analyze(uuid.UUID(finding_id_raw))
        except Exception:
            logger.exception(
                "strategic.message_failed",
                stream=stream_name,
                msg_id=msg_id,
            )
        finally:
            try:
                self._redis.xack(stream_name, self._group, msg_id)
            except Exception:
                logger.exception(
                    "strategic.ack_failed",
                    stream=stream_name,
                    msg_id=msg_id,
                )

    def _analyze(self, finding_id: uuid.UUID) -> None:
        session = self._session_factory()
        try:
            finding = session.get(Finding, finding_id)
            if finding is None:
                logger.warning("strategic.finding_not_found", finding_id=str(finding_id))
                return
            execution, suggestions = self._agent.analyze_finding(
                session, finding=finding, trigger=AgentTrigger.finding
            )
            session.commit()
            logger.info(
                "strategic.analyzed",
                finding_id=str(finding_id),
                execution_id=str(execution.id),
                suggestion_count=len(suggestions),
            )
        except Exception:
            session.rollback()
            logger.exception(
                "strategic.analyze_failed", finding_id=str(finding_id)
            )
        finally:
            session.close()
