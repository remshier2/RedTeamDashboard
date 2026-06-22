"""Phase 9g — MCP-side Strategic surface.

Covers the tools an external agent (Claude Code, Cursor, etc.) uses when
acting as Strategic with the analyst's own API key:

- ``get_finding_context`` returns engagement + finding + scope + tools.
- ``propose_strategic_suggestion`` writes a Suggestion + AgentExecution row
  tagged ``model_provider='mcp:external'`` so the Costs tab can distinguish
  analyst-brought agents.
- ``propose_strategic_suggestion`` HARD-REFUSES ``task_kind='exploit'`` —
  CHARTER invariant (agents scan, analysts exploit). Server-side guard,
  independent of the in-process Strategic's defense-in-depth filter.
- ``propose_strategic_suggestion`` refuses unknown tool names.
- ``list_open_suggestions`` returns only open rows for the engagement.

Auth context (the ContextVars MCPAuthMiddleware sets) is wired by hand here
so tests don't need to spin up the full ASGI stack.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import hash_api_key
from app.mcp import auth as mcp_auth
from app.mcp.server import (
    get_finding_context,
    list_open_suggestions,
    propose_strategic_suggestion,
    strategic_planning,
)
from app.models import (
    AgentExecution,
    AgentName,
    APIKey,
    APIKeyScope,
    Engagement,
    EngagementStatus,
    Finding,
    FindingPhase,
    FindingStatus,
    ScopeItem,
    ScopeKind,
    Severity,
    Suggestion,
    SuggestionStatus,
    User,
)


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="MCP Strategic",
        slug=f"mcp-strategic-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.active,
        description="external agent plays Strategic",
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
        title="crt.sh discovery",
        severity=Severity.info,
        details={"certs": 12},
        source_tool="crt_sh",
        target="acme.test",
        phase=FindingPhase.osint,
        status=FindingStatus.pending_validation,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture()
def cli_key_user(db: Session) -> tuple[APIKey, User]:
    """Create (and tear down) a cli-scoped API key + matching User."""
    user = User(email=f"mcp-{uuid.uuid4().hex[:6]}@example.com", display_name="mcp")
    db.add(user)
    db.commit()
    db.refresh(user)
    raw = f"rtd_mcp_{uuid.uuid4().hex}"
    key = APIKey(
        name="mcp-strategic-test",
        scope=APIKeyScope.cli,
        key_hash=hash_api_key(raw),
        created_by=user.id,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key, user


@pytest.fixture()
def with_mcp_auth(cli_key_user: tuple[APIKey, User]) -> Iterator[None]:
    """Install the ContextVars MCPAuthMiddleware would normally set."""
    key, user = cli_key_user
    tk = mcp_auth._current_key.set(key)
    tu = mcp_auth._current_user.set(user)
    try:
        yield
    finally:
        mcp_auth._current_key.reset(tk)
        mcp_auth._current_user.reset(tu)


# ── get_finding_context ─────────────────────────────────────────────────


def test_get_finding_context_returns_full_picture(
    engagement: Engagement, finding: Finding
) -> None:
    ctx = get_finding_context(engagement.slug, str(finding.id))
    assert "error" not in ctx
    assert ctx["engagement"]["slug"] == engagement.slug
    assert ctx["finding"]["id"] == str(finding.id)
    assert ctx["finding"]["source_tool"] == "crt_sh"
    assert any(i["value"] == "acme.test" for i in ctx["scope"]["include"])
    assert any(t["name"] == "subfinder" for t in ctx["tools"])
    assert any("never propose exploit" in r.lower() for r in ctx["charter_rules"])


def test_get_finding_context_404s_unknown_finding(engagement: Engagement) -> None:
    ctx = get_finding_context(engagement.slug, str(uuid.uuid4()))
    assert "error" in ctx


# ── propose_strategic_suggestion ────────────────────────────────────────


def test_propose_writes_suggestion_and_execution(
    db: Session,
    with_mcp_auth: None,
    engagement: Engagement,
    finding: Finding,
) -> None:
    out = propose_strategic_suggestion(
        engagement.slug,
        str(finding.id),
        title="Resolve discovered hosts",
        body="Map subdomains to IPs.",
        task_kind="enum",
        tool="dns_lookup",
        target="a.acme.test",
        owner_eligibility="agent",
    )
    assert "error" not in out, out
    assert out["status"] == "open"
    assert out["task_kind"] == "enum"

    db.expire_all()
    suggestion = db.get(Suggestion, uuid.UUID(out["suggestion_id"]))
    assert suggestion is not None
    assert suggestion.created_by_agent == AgentName.strategic
    assert suggestion.payload["tool"] == "dns_lookup"

    execution = db.get(AgentExecution, uuid.UUID(out["execution_id"]))
    assert execution is not None
    assert execution.model_provider == "mcp:external"
    assert execution.model_name is None
    assert execution.input["via"] == "mcp"


def test_propose_refuses_exploit_task_kind(
    with_mcp_auth: None,
    engagement: Engagement,
    finding: Finding,
) -> None:
    out = propose_strategic_suggestion(
        engagement.slug,
        str(finding.id),
        title="RCE",
        body="bad",
        task_kind="exploit",
        tool="dns_lookup",
        target="acme.test",
    )
    assert "error" in out
    assert "agents scan" in out["error"].lower()


def test_propose_refuses_unknown_tool(
    with_mcp_auth: None,
    engagement: Engagement,
    finding: Finding,
) -> None:
    out = propose_strategic_suggestion(
        engagement.slug,
        str(finding.id),
        title="bogus",
        body="",
        task_kind="enum",
        tool="metasploit",
        target="acme.test",
    )
    assert "error" in out
    assert "unknown tool" in out["error"].lower()


# ── list_open_suggestions ───────────────────────────────────────────────


def test_list_open_only_returns_open_rows(
    db: Session,
    with_mcp_auth: None,
    engagement: Engagement,
    finding: Finding,
) -> None:
    out = propose_strategic_suggestion(
        engagement.slug,
        str(finding.id),
        title="Probe HTTPS",
        body="",
        task_kind="enum",
        tool="httpx_probe",
        target="https://acme.test",
        owner_eligibility="agent",
    )
    assert "error" not in out, out
    sid = uuid.UUID(out["suggestion_id"])

    listed = list_open_suggestions(engagement.slug)
    assert any(s["id"] == str(sid) for s in listed)

    # Dismiss it directly in DB; list_open should no longer return it.
    suggestion = db.get(Suggestion, sid)
    assert suggestion is not None
    suggestion.status = SuggestionStatus.dismissed
    db.commit()

    listed = list_open_suggestions(engagement.slug)
    assert not any(s["id"] == str(sid) for s in listed)


# ── prompt parity ──────────────────────────────────────────────────────


def test_strategic_planning_prompt_references_charter() -> None:
    prompt = strategic_planning("acme-q3", "00000000-0000-0000-0000-000000000000")
    assert "agents scan, analysts exploit" in prompt.lower()
    assert "propose_strategic_suggestion" in prompt
    assert "exploit" in prompt.lower()
