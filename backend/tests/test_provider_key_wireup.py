"""BYO-key wireup: resolver + worker lookup.

Covers:
- ``resolve_for_user`` decrypts the most-recently-updated row for a provider.
- ``resolve_for_user`` raises ``NoProviderKeyError`` when nothing matches.
- The ``RunRunner._resolve_graph`` lookup path threads the resolved api_key
  and endpoint into the model mapping handed to ``graph_factory`` (so
  ``make_llm`` can call the SDK with them).
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import ProviderKeyKind, User, UserProviderKey
from app.runs.streams import outbound_stream
from app.services.provider_key_resolver import (
    NoProviderKeyError,
    resolve_for_user,
)
from app.services.secret_box import encrypt, last4, reset_for_tests
from app.worker.runner import RunRunner


@pytest.fixture(autouse=True)
def _reset_secret_box_cache() -> None:
    reset_for_tests()


def _make_user(db: Session) -> User:
    u = User(
        email=f"wireup-{uuid.uuid4().hex[:6]}@example.com",
        display_name="wireup",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _add_key(
    db: Session,
    user: User,
    *,
    provider: str,
    api_key: str | None,
    is_local: bool = False,
    endpoint: str | None = None,
    name: str | None = None,
) -> UserProviderKey:
    row = UserProviderKey(
        user_id=user.id,
        kind=ProviderKeyKind.model_provider,
        name=name or f"{provider}-{uuid.uuid4().hex[:6]}",
        provider=provider,
        is_local=is_local,
        models=[],
        endpoint=endpoint,
        encrypted_key=encrypt(api_key) if api_key else None,
        key_last4=last4(api_key) if api_key else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ── resolver ─────────────────────────────────────────────────────────────


def test_resolver_decrypts_user_key(db: Session) -> None:
    user = _make_user(db)
    _add_key(db, user, provider="anthropic", api_key="sk-ant-resolved-1234")

    out = resolve_for_user(db, user_id=user.id, provider="anthropic")
    assert out.api_key == "sk-ant-resolved-1234"
    assert out.is_local is False


def test_resolver_returns_endpoint_for_local(db: Session) -> None:
    user = _make_user(db)
    _add_key(
        db,
        user,
        provider="ollama",
        api_key=None,
        is_local=True,
        endpoint="http://localhost:11434",
    )

    out = resolve_for_user(db, user_id=user.id, provider="ollama")
    assert out.api_key is None
    assert out.endpoint == "http://localhost:11434"
    assert out.is_local is True


def test_resolver_no_row_raises(db: Session) -> None:
    user = _make_user(db)
    with pytest.raises(NoProviderKeyError):
        resolve_for_user(db, user_id=user.id, provider="anthropic")


def test_resolver_picks_most_recent_when_multiple(db: Session) -> None:
    user = _make_user(db)
    _add_key(
        db, user, provider="anthropic", api_key="sk-ant-old", name="old"
    )
    newer = _add_key(
        db, user, provider="anthropic", api_key="sk-ant-new", name="new"
    )
    # Touch newer to make it more recently updated
    newer.endpoint = None
    db.commit()

    out = resolve_for_user(db, user_id=user.id, provider="anthropic")
    assert out.api_key == "sk-ant-new"


def test_resolver_ignores_mcp_kind_rows(db: Session) -> None:
    user = _make_user(db)
    db.add(
        UserProviderKey(
            user_id=user.id,
            kind=ProviderKeyKind.mcp_server,
            name="GitHub MCP",
            provider="anthropic",  # name collision is fine; kind differs
            is_local=False,
            models=[],
            endpoint="https://example.test",
            encrypted_key=encrypt("ghp-xxxx"),
            key_last4=last4("ghp-xxxx"),
        )
    )
    db.commit()
    with pytest.raises(NoProviderKeyError):
        resolve_for_user(db, user_id=user.id, provider="anthropic")


# ── runner lookup ────────────────────────────────────────────────────────


class _CapturingFactory:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any] | None] = []

    def __call__(
        self,
        model: Any,
        allowed_tools: Any = None,
        mcp_url: Any = None,
        lease_token: Any = None,
    ) -> object:
        # Phase mcp-leases / Stage 1.5: the runner passes allowed_tools,
        # mcp_url, and lease_token alongside the model dict. These tests
        # only assert on the model lookup, so the rest is signature-only.
        del allowed_tools, mcp_url, lease_token
        self.calls.append(dict(model) if model else None)
        return object()


def _make_session_factory(db: Session):
    # Bind to SessionLocal to spin a real session per call (RunRunner closes it)
    from app.db.session import SessionLocal

    return SessionLocal


def test_runner_threads_user_key_into_graph_factory(db: Session) -> None:
    user = _make_user(db)
    _add_key(db, user, provider="anthropic", api_key="sk-ant-runner-9999")

    factory = _CapturingFactory()
    runner = RunRunner(
        graph_factory=factory,
        redis_client=object(),
        session_factory=_make_session_factory(db),
    )

    runner._resolve_graph(
        {
            "type": "run.start",
            "thread_id": "t1",
            "model": {"provider": "anthropic", "name": "claude-opus-4-7"},
            "acting_user_id": str(user.id),
        }
    )
    assert factory.calls == [
        {
            "provider": "anthropic",
            "name": "claude-opus-4-7",
            "api_key": "sk-ant-runner-9999",
            "endpoint": None,
        }
    ]


def test_runner_envelope_without_acting_user_leaves_key_blank(
    db: Session,
) -> None:
    factory = _CapturingFactory()
    runner = RunRunner(
        graph_factory=factory,
        redis_client=object(),
        session_factory=_make_session_factory(db),
    )
    runner._resolve_graph(
        {
            "type": "run.start",
            "thread_id": "t1",
            "model": {"provider": "anthropic", "name": "claude-opus-4-7"},
        }
    )
    assert factory.calls == [
        {
            "provider": "anthropic",
            "name": "claude-opus-4-7",
            "api_key": None,
            "endpoint": None,
        }
    ]


def test_runner_propagates_no_key_error(db: Session) -> None:
    """Worker handle() catches the NoProviderKeyError raised by the runner
    lookup and surfaces a run.errored — verified here by re-raising past
    _resolve_graph."""
    user = _make_user(db)  # no anthropic key uploaded
    factory = _CapturingFactory()
    runner = RunRunner(
        graph_factory=factory,
        redis_client=object(),
        session_factory=_make_session_factory(db),
    )
    # Just verify _resolve_graph raises so .handle()'s try/except can map it.
    with pytest.raises(NoProviderKeyError):
        runner._resolve_graph(
            {
                "type": "run.start",
                "thread_id": "t1",
                "model": {"provider": "anthropic", "name": "claude-opus-4-7"},
                "acting_user_id": str(user.id),
            }
        )


# ── envelope plaintext guard ─────────────────────────────────────────────


def test_outbound_stream_helper_is_unused_in_envelope() -> None:
    """Sanity guard: ``acting_user_id`` belongs on the envelope; plaintext
    API keys must NEVER appear on the Redis stream. The check lives in
    ``test_engagements_api.test_run_endpoint_enqueues_run_start`` too;
    this is just a compile-time reminder that ``outbound_stream`` is the
    SSE feed name helper, not a place to encode secrets."""
    assert outbound_stream(uuid.uuid4()).startswith("runs:")
