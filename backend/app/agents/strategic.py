"""Strategic watcher — the Phase 9 planner.

This agent assists analysts during **authorized security engagements** by analyzing
findings and suggesting follow-up enumeration and scanning tasks.

**Charter:** Agents perform **enumeration and scanning only**. This agent is a pure
observer — it never executes tools, never dispatches. The analyst reviews suggestions
and explicitly accepts them to create Tasks. Validation/proof-of-concept work
(``TaskKind.exploit``) is **analyst-only** — filtered out even if the model proposes it.

Given a finding, it asks the LLM "what passive scan/enum tasks would dig into
this?" and writes the answers as ``Suggestion`` rows the analyst reviews from
the findings slide-over. The analyst's accept-click is what creates a Task
(and only then does ``TacticalAgent`` consider dispatching).

The LLM is asked for structured JSON via ``with_structured_output``; we don't
trust freeform text here.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentName,
    AgentTrigger,
    Engagement,
    Finding,
    OwnerEligibility,
    ScopeItem,
    Suggestion,
    SuggestionKind,
    SuggestionStatus,
    TaskKind,
)
from app.orchestrator.llm import default_provider_model
from app.orchestrator.tools import all_tools

logger = structlog.get_logger(__name__)


# Strategic produces scan + enum tasks only. exploit slipping through the
# structured-output schema is still filtered out post-LLM as a defense.
_AGENT_TASK_KINDS = (TaskKind.scan, TaskKind.enum)


class _ProposedTask(BaseModel):
    """LLM-side row shape for a single proposed next step."""

    title: str = Field(..., description="One-line task title shown to the analyst.")
    rationale: str = Field(
        ..., description="Why this is the right next step given the finding."
    )
    kind: TaskKind = Field(
        ...,
        description=(
            "scan = active probing (portscan, subnet_sweep, service_detect). "
            "enum = passive enumeration (subfinder, crt_sh, dns_lookup, "
            "whois_lookup, httpx_probe, reverse_dns). "
            "exploit = forbidden; agents never exploit."
        ),
    )
    owner_eligibility: OwnerEligibility = Field(
        OwnerEligibility.either,
        description=(
            "agent = safe for the worker to run autonomously after analyst "
            "accept. analyst = manual-only. either = analyst chooses."
        ),
    )
    tool: str = Field(
        ...,
        description="OSINT tool name (must be one of the listed registered tools).",
    )
    target: str = Field(
        ...,
        description="Concrete target the tool runs against (domain/ip/cidr/url).",
    )


class _StrategicProposal(BaseModel):
    """Structured-output envelope from the Strategic LLM call."""

    summary: str = Field(
        ...,
        description="2-3 sentence read on the finding from a red-team perspective.",
    )
    tasks: list[_ProposedTask] = Field(
        default_factory=list,
        description="Concrete next-step tasks. Empty list = nothing to add right now.",
    )


STRATEGIC_SYSTEM_PROMPT = (
    """You are the Strategic watcher in a red-team orchestrator. \
Your job is to read one finding and propose what passive enumeration or \
active scan tasks would develop it further.

HARD RULES (never break):
- Agents scan, analysts exploit. NEVER propose exploit-kind tasks. Only \
scan or enum.
- Only propose tools from the provided registry. Inventing a tool name is a \
failure.
- Targets MUST be inside the engagement's scope. If the finding's target sits \
outside scope, return an empty task list.
- Each proposed task must be one concrete next step (one tool + one target). \
Do not stack steps.
- If the finding doesn't suggest a useful next step right now, return tasks=[]. \
Empty is fine.

You are a pure observer. Your output is a recommendation; nothing runs until \
the analyst accepts.
"""
)


def _scope_summary(scope_items: Iterable[ScopeItem]) -> str:
    lines = []
    for item in scope_items:
        marker = "EXCLUDE" if item.is_exclusion else "INCLUDE"
        lines.append(f"  {marker} {item.kind.value}: {item.value}")
    return "\n".join(lines) if lines else "  (no scope items defined)"


def _tools_summary() -> str:
    lines = []
    for spec in all_tools():
        lines.append(
            f"  - {spec.name} (risk={spec.risk.value}, "
            f"target={spec.target_arg}/{spec.kind.value}): {spec.description}"
        )
    return "\n".join(lines)


def _build_user_prompt(engagement: Engagement, finding: Finding, scope: str) -> str:
    return f"""ENGAGEMENT: {engagement.name} ({engagement.slug})
Description: {engagement.description or "(none)"}

SCOPE:
{scope}

REGISTERED TOOLS:
{_tools_summary()}

FINDING:
  id:       {finding.id}
  title:    {finding.title}
  phase:    {finding.phase.value}
  severity: {finding.severity.value}
  tool:     {finding.source_tool or "(unknown)"}
  target:   {finding.target or "(none)"}
  data:     {finding.details!r}

Propose next-step tasks per the rules in your system prompt. Return JSON \
matching the required schema.
"""


def _extract_usage(response: Any) -> tuple[int | None, int | None]:
    """Pull (input_tokens, output_tokens) out of a langchain response if present.

    Langchain wraps token usage on either ``response_metadata['usage']``
    (Anthropic) or ``response_metadata['token_usage']`` (OpenAI), and the
    structured-output wrapper hides the underlying message. We dig defensively
    and return ``(None, None)`` when we can't find anything — non-fatal.
    """
    meta = getattr(response, "response_metadata", None) or {}
    usage = meta.get("usage") or meta.get("token_usage") or {}
    return (
        usage.get("input_tokens") or usage.get("prompt_tokens"),
        usage.get("output_tokens") or usage.get("completion_tokens"),
    )


def _make_chat_model(provider: str, name: str) -> Any:
    """Provider-agnostic chat model factory used by Strategic.

    Cousin of ``app.orchestrator.llm.make_llm`` but WITHOUT ``.bind_tools()`` —
    Strategic doesn't tool-call, it returns structured JSON. Imports lazily so
    the unused providers' SDKs aren't required at import time.
    """
    provider = provider.lower()
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=name, max_tokens=4096)
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=name)
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        from app.core.config import settings

        return ChatOllama(model=name, base_url=settings.ollama_host)
    if provider == "azure":
        from langchain_openai import AzureChatOpenAI

        from app.core.config import settings

        return AzureChatOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key or None,
            azure_deployment=name or settings.azure_openai_deployment,
            api_version=settings.azure_openai_api_version,
        )
    raise ValueError(f"unknown LLM provider {provider!r}")


class StrategicAgent:
    """Pure-watcher planner. ``analyze_finding`` is the only entry point."""

    def __init__(
        self,
        *,
        provider: str | None = None,
        model_name: str | None = None,
        llm: Any | None = None,
    ) -> None:
        """Use ``llm=...`` in tests to inject a fake; otherwise the agent
        resolves the active provider/model from settings on first ``invoke``."""
        self._llm = llm
        self._provider = provider
        self._model_name = model_name

    def _resolve_llm(self) -> tuple[Any, str, str]:
        if self._llm is not None:
            return (
                self._llm,
                self._provider or "test",
                self._model_name or "test",
            )
        provider = self._provider
        model_name = self._model_name
        if not (provider and model_name):
            provider, model_name = default_provider_model()
        return _make_chat_model(provider, model_name), provider, model_name

    def analyze_finding(
        self,
        session: Session,
        *,
        finding: Finding,
        trigger: AgentTrigger,
    ) -> tuple[AgentExecution, list[Suggestion]]:
        """Run Strategic over a finding and persist suggestions + execution row.

        Caller commits the session — we add but don't commit so this composes
        cleanly inside an API request transaction.
        """
        engagement = session.get(Engagement, finding.engagement_id)
        if engagement is None:
            raise ValueError(f"finding {finding.id} has no engagement")
        scope_items = list(
            session.execute(
                select(ScopeItem).where(ScopeItem.engagement_id == engagement.id)
            ).scalars()
        )

        prompt = _build_user_prompt(engagement, finding, _scope_summary(scope_items))

        execution = AgentExecution(
            engagement_id=engagement.id,
            agent=AgentName.strategic,
            trigger=trigger,
            input={
                "finding_id": str(finding.id),
                "engagement_slug": engagement.slug,
            },
            status=AgentExecutionStatus.running,
            started_at=datetime.now(tz=UTC),
        )
        session.add(execution)
        session.flush()  # need execution.id below if we want to backref

        try:
            llm, provider, model_name = self._resolve_llm()
            execution.model_provider = provider
            execution.model_name = model_name
            structured = llm.with_structured_output(_StrategicProposal)
            messages = [
                ("system", STRATEGIC_SYSTEM_PROMPT),
                ("user", prompt),
            ]
            raw_response: Any = structured.invoke(messages)
            # with_structured_output gives us back the parsed Pydantic model
            # directly. Token counting needs the raw response; some langchain
            # versions wrap with .with_raw_response so the parsed model has
            # the metadata attached. We try our best, ignore if missing.
            proposal: _StrategicProposal = (
                raw_response
                if isinstance(raw_response, _StrategicProposal)
                else _StrategicProposal.model_validate(raw_response)
            )
            tokens_in, tokens_out = _extract_usage(raw_response)
            execution.tokens_in = tokens_in
            execution.tokens_out = tokens_out
        except Exception as exc:  # noqa: BLE001 — any LLM failure → mark failed
            execution.status = AgentExecutionStatus.failed
            execution.error = str(exc)[:2000]
            execution.completed_at = datetime.now(tz=UTC)
            logger.warning(
                "strategic.failed",
                finding_id=str(finding.id),
                error=str(exc),
            )
            return execution, []

        suggestions = self._persist_suggestions(
            session,
            engagement_id=engagement.id,
            finding_id=finding.id,
            proposal=proposal,
        )

        execution.output = {
            "summary": proposal.summary,
            "suggestion_ids": [str(s.id) for s in suggestions],
            "rejected_exploit_count": sum(
                1 for t in proposal.tasks if t.kind == TaskKind.exploit
            ),
        }
        execution.status = AgentExecutionStatus.completed
        execution.completed_at = datetime.now(tz=UTC)

        return execution, suggestions

    def _persist_suggestions(
        self,
        session: Session,
        *,
        engagement_id: uuid.UUID,
        finding_id: uuid.UUID,
        proposal: _StrategicProposal,
    ) -> list[Suggestion]:
        suggestions: list[Suggestion] = []
        for task in proposal.tasks:
            if task.kind not in _AGENT_TASK_KINDS:
                # Defense in depth: even if the LLM tries to propose exploit,
                # we silently drop it. The rejection count goes on the
                # execution.output for visibility.
                continue
            suggestion = Suggestion(
                engagement_id=engagement_id,
                finding_id=finding_id,
                title=task.title,
                body=task.rationale,
                kind=SuggestionKind.task,
                payload={
                    "tool": task.tool,
                    "target": task.target,
                    "task_kind": task.kind.value,
                    "owner_eligibility": task.owner_eligibility.value,
                },
                status=SuggestionStatus.open,
                created_by_agent=AgentName.strategic,
            )
            session.add(suggestion)
            session.flush()
            suggestions.append(suggestion)
        return suggestions
