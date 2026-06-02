"""Redis Streams name helpers.

Per-engagement streams give us natural isolation:
- inbound  ``runs:{engagement_id}:in``      — run.start / run.resume commands
- outbound ``runs:{engagement_id}:events``  — lifecycle events for the SSE feed

The consumer group name is shared across worker replicas so message delivery
fans out instead of duplicating.
"""
from __future__ import annotations

import uuid

CONSUMER_GROUP = "osint-workers"

_INBOUND_PREFIX = "runs:"
_INBOUND_SUFFIX = ":in"
_OUTBOUND_SUFFIX = ":events"


def inbound_stream(engagement_id: uuid.UUID | str) -> str:
    return f"{_INBOUND_PREFIX}{engagement_id}{_INBOUND_SUFFIX}"


def outbound_stream(engagement_id: uuid.UUID | str) -> str:
    return f"{_INBOUND_PREFIX}{engagement_id}{_OUTBOUND_SUFFIX}"


def engagement_id_from_inbound(stream_name: str) -> uuid.UUID:
    if not stream_name.startswith(_INBOUND_PREFIX) or not stream_name.endswith(
        _INBOUND_SUFFIX
    ):
        raise ValueError(f"not an inbound stream name: {stream_name!r}")
    raw = stream_name[len(_INBOUND_PREFIX) : -len(_INBOUND_SUFFIX)]
    return uuid.UUID(raw)


# Per-thread LLM choice cache. ``start_run`` writes the chosen
# (provider, model) here so the approval endpoint can include it in
# ``run.resume`` envelopes without re-deriving it. TTL is generous —
# runs are short-lived (minutes) and the key gets overwritten on each
# new run for the same thread anyway.
_RUN_MODEL_KEY = "run:model:{thread_id}"
_RUN_MODEL_TTL_SECONDS = 6 * 60 * 60  # 6h


def run_model_key(thread_id: uuid.UUID | str) -> str:
    return _RUN_MODEL_KEY.format(thread_id=thread_id)


def store_run_model(
    redis_client: object,
    thread_id: uuid.UUID | str,
    *,
    provider: str,
    model_name: str,
) -> None:
    """HSET the (provider, name) for a thread; TTL'd so abandoned runs expire."""
    client: object = redis_client
    key = run_model_key(thread_id)
    client.hset(key, mapping={"provider": provider, "name": model_name})  # type: ignore[attr-defined]
    client.expire(key, _RUN_MODEL_TTL_SECONDS)  # type: ignore[attr-defined]


def load_run_model(
    redis_client: object,
    thread_id: uuid.UUID | str,
) -> dict[str, str] | None:
    """HGETALL — returns ``None`` if the thread has no recorded model."""
    raw = redis_client.hgetall(run_model_key(thread_id))  # type: ignore[attr-defined]
    if not raw:
        return None
    return {"provider": raw["provider"], "name": raw["name"]}
