"""Finding-scoped AI assistant (Phase 2 of the pane of glass).

This service assembles a read-only dossier for one finding and asks the
analyst's BYO LLM for a narrative response. It deliberately does NOT run tools,
mutate findings, or create tasks. Phase 3 will add consent-gated action bubbles
that route approvals through existing suggestion/task/finding APIs.
"""
from __future__ import annotations

import contextlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.agents import TacticalAgent, TacticalRefusedExploit
from app.agents.strategic import _extract_usage, _make_chat_model
from app.core import pricing
from app.models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentName,
    AgentTrigger,
    Attachment,
    Conversation,
    ConversationMessage,
    Engagement,
    Finding,
    FindingPhase,
    FindingStatus,
    FindingSummary,
    Observation,
    ObservationFindingLink,
    OwnerEligibility,
    ScopeItem,
    ScopeKind,
    Severity,
    Suggestion,
    SuggestionKind,
    SuggestionStatus,
    Task,
    TaskKind,
    TaskStatus,
)
from app.orchestrator.llm import default_provider_model
from app.orchestrator.tools import all_tools, get_tool
from app.services.ephemeral_provider_key import resolve_for_user
from app.services.finding_activity import build_finding_activity

_SYSTEM_PROMPT = """You are a finding-scoped copilot for an authorized security engagement.
Use only the dossier supplied by the dashboard. Do not invent evidence, targets,
credentials, exploitability, or client impact. If the dossier is thin, say what
is missing and suggest safe next steps.

Return a JSON object only, with this shape:
{
  "answer": "the narrative response to show the analyst",
  "actions": [
    {
      "type": "run_tool",
      "title": "short button/card title",
      "description": "why this action is useful",
      "params": {}
    }
  ]
}

Actions are proposals only. Do not claim they have run. Actions must be
agent-executable tool runs using one of the built-in tools from the dossier's
agent_tools list. Never propose exploit actions. If no useful tool action exists,
return an empty actions array and the dashboard will create safe defaults.

The conversation history lists every action you already proposed with its
lifecycle: [proposed], [accepted, run <status>, produced N finding(s)], or
[denied]. DO NOT re-propose an action that is already proposed, accepted, or
denied — the analyst is still working through them. Only propose genuinely NEW
actions. When an accepted run has produced findings, reference them in your
answer instead of re-suggesting the run. If an accepted run FAILED (often
because the target is not in engagement scope), say so and explain what you
saw — propose an add_scope action for that target (the dashboard marks it
source='found' so it shows as discovered-during-engagement) rather than
re-dispatching the same run. Action types you may propose: run_tool (enum/scan
only) and add_scope. Keep proposals minimal and never duplicate an action
already in the ledger.

Be concise: maximum 5 bullets or 2 short paragraphs. Put detail in proposed
action descriptions, not the chat narrative."""


def get_latest_conversation(
    session: Session,
    *,
    finding_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Conversation | None:
    return session.execute(
        select(Conversation)
        .where(
            Conversation.finding_id == finding_id,
            Conversation.created_by_user_id == user_id,
        )
        .order_by(Conversation.updated_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def get_conversation_messages(
    session: Session,
    conversation_id: uuid.UUID,
) -> list[ConversationMessage]:
    return list(
        session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.created_at.asc())
        ).scalars()
    )


def get_or_create_conversation(
    session: Session,
    *,
    finding: Finding,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None = None,
) -> Conversation:
    if conversation_id is not None:
        conv = session.get(Conversation, conversation_id)
        if conv is None or conv.finding_id != finding.id:
            raise ValueError("conversation not found for this finding")
        return conv

    conv = get_latest_conversation(session, finding_id=finding.id, user_id=user_id)
    if conv is not None:
        return conv

    title = finding.title[:180]
    conv = Conversation(
        engagement_id=finding.engagement_id,
        finding_id=finding.id,
        created_by_user_id=user_id,
        title=title,
    )
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return conv


def build_finding_dossier(session: Session, finding: Finding) -> dict[str, Any]:
    """Collect the read-only context the chatbot is allowed to use."""
    engagement = session.get(Engagement, finding.engagement_id)
    scope = list(
        session.execute(
            select(ScopeItem).where(ScopeItem.engagement_id == finding.engagement_id)
        ).scalars()
    )
    summaries = list(
        session.execute(
            select(FindingSummary)
            .where(FindingSummary.finding_id == finding.id)
            .order_by(FindingSummary.created_at.desc())
            .limit(5)
        ).scalars()
    )
    observations = list(
        session.execute(
            select(Observation)
            .join(
                ObservationFindingLink,
                ObservationFindingLink.observation_id == Observation.id,
            )
            .where(ObservationFindingLink.finding_id == finding.id)
            .order_by(Observation.created_at.desc())
            .limit(10)
        ).scalars()
    )
    suggestions = list(
        session.execute(
            select(Suggestion)
            .where(Suggestion.finding_id == finding.id)
            .order_by(Suggestion.created_at.desc())
            .limit(10)
        ).scalars()
    )
    attachments = list(
        session.execute(
            select(Attachment)
            .where(Attachment.finding_id == finding.id)
            .order_by(Attachment.created_at.desc())
            .limit(10)
        ).scalars()
    )

    return {
        "engagement": {
            "id": str(finding.engagement_id),
            "slug": engagement.slug if engagement else None,
            "name": engagement.name if engagement else None,
        },
        "finding": {
            "id": str(finding.id),
            "title": finding.title,
            "severity": finding.severity.value,
            "status": finding.status.value,
            "phase": finding.phase.value,
            "target": finding.target,
            "source_tool": finding.source_tool,
            "summary": finding.summary,
            "details": finding.details,
            "tags": finding.tags or [],
            "created_at": finding.created_at.isoformat(),
            "observed_at": finding.observed_at.isoformat()
            if finding.observed_at
            else None,
        },
        "activity": build_finding_activity(session, finding.id)[:25],
        "scope": [
            {
                "kind": item.kind.value,
                "value": item.value,
                "is_exclusion": item.is_exclusion,
                "source": getattr(item, "source", None),
            }
            for item in scope[:50]
        ],
        "summary_history": [
            {
                "body": s.body,
                "author_user_id": str(s.author_user_id) if s.author_user_id else None,
                "created_at": s.created_at.isoformat(),
            }
            for s in summaries
        ],
        "observations": [
            {
                "id": str(o.id),
                "content": o.content,
                "phase": o.phase.value if o.phase else None,
                "created_at": o.created_at.isoformat(),
            }
            for o in observations
        ],
        "suggestions": [
            {
                "id": str(s.id),
                "title": s.title,
                "body": s.body,
                "kind": s.kind.value,
                "status": s.status.value,
                "payload": s.payload,
                "created_at": s.created_at.isoformat(),
            }
            for s in suggestions
        ],
        "attachments": [
            {
                "id": str(a.id),
                "filename": a.filename,
                "content_type": a.content_type,
                "size_bytes": a.size_bytes,
                "created_at": a.created_at.isoformat(),
            }
            for a in attachments
        ],
        "agent_tools": [
            {
                "name": t.name,
                "risk": t.risk.value,
                "target_arg": t.target_arg,
                "kind": t.kind.value,
                "description": t.description,
                "extra_properties": dict(t.extra_properties),
            }
            for t in all_tools()
        ],
    }


def _action_ledger_line(
    action: dict[str, Any],
    *,
    task_by_id: dict[uuid.UUID, Task],
    findings_by_run: dict[str, int],
) -> str:
    """One compact ledger line so the AI can see an action's lifecycle.

    Prevents the re-suggestion loop: without the status + outcome fed back
    into history, the model re-derives the same proposals every turn. The
    Task + findings-count lookups are precomputed by the caller (batched) —
    this function is pure, no per-action DB hits (was an N+1).
    """
    title = str(action.get("title") or action.get("type") or "action")
    status = str(action.get("status") or "proposed")
    typ = str(action.get("type") or "")
    result = action.get("result") if isinstance(action.get("result"), dict) else {}
    outcome = ""
    if typ == "run_tool" and result.get("task_id"):
        try:
            task = task_by_id.get(uuid.UUID(str(result["task_id"])))
        except (ValueError, TypeError):
            task = None
        if task is not None:
            outcome = f", run {task.status.value}"
            run_id = result.get("run_id")
            if task.status == TaskStatus.completed and run_id:
                n = findings_by_run.get(str(run_id), 0)
                outcome += f", produced {n} finding(s)"
            if task.status == TaskStatus.failed:
                outcome += " (failed — likely out-of-scope or approval gate)"
    elif result:
        outcome = f", result: {json.dumps(result, default=str)[:140]}"
    return f"  • [{status}{outcome}] {typ}: {title}"


def _conversation_history_prompt(
    session: Session, messages: list[ConversationMessage]
) -> str:
    clipped = messages[-12:]

    # Batch the Task + produced-findings lookups ONCE instead of per action
    # (was a messages×actions N+1 on every chat turn).
    task_ids: set[uuid.UUID] = set()
    run_ids: set[str] = set()
    for m in clipped:
        payload = m.action_payload if isinstance(m.action_payload, dict) else {}
        actions = payload.get("actions", []) if isinstance(payload.get("actions"), list) else []
        for a in actions:
            if not isinstance(a, dict):
                continue
            r = a.get("result") if isinstance(a.get("result"), dict) else {}
            tid = r.get("task_id")
            rid = r.get("run_id")
            if a.get("type") == "run_tool":
                if tid:
                    with contextlib.suppress(ValueError, TypeError):
                        task_ids.add(uuid.UUID(str(tid)))
                if rid:
                    run_ids.add(str(rid))
    task_by_id: dict[uuid.UUID, Task] = {}
    if task_ids:
        task_by_id = {
            t.id: t
            for t in session.execute(
                select(Task).where(Task.id.in_(task_ids))
            ).scalars()
        }
    findings_by_run: dict[str, int] = {}
    if run_ids:
        rows = session.execute(
            select(
                Finding.details["thread_id"].astext,
                func.count(Finding.id),
            )
            .where(Finding.details["thread_id"].astext.in_(run_ids))
            .group_by(Finding.details["thread_id"].astext)
        ).all()
        findings_by_run = {str(tid): int(cnt or 0) for tid, cnt in rows}

    lines: list[str] = []
    for m in clipped:
        if m.content.strip():
            lines.append(f"{m.role.upper()}: {m.content[:1500]}")
        payload = m.action_payload if isinstance(m.action_payload, dict) else {}
        actions = (
            payload.get("actions")
            if isinstance(payload.get("actions"), list)
            else []
        )
        for a in actions:
            if isinstance(a, dict):
                lines.append(
                    _action_ledger_line(
                        a, task_by_id=task_by_id, findings_by_run=findings_by_run
                    )
                )
    return "\n".join(lines)


_ALLOWED_ACTION_TYPES = {"run_tool", "add_scope"}
_ALLOWED_TASK_KINDS = {"enum", "scan"}
_ALLOWED_SEVERITIES = {"info", "low", "medium", "high", "critical"}
_ALLOWED_PHASES = {"osint", "vuln_scan", "exploit", "phishing", "general"}


def _coerce_llm_content(raw: Any) -> str:
    """Normalize an LLM response body to a string.

    LangChain chat models return ``content`` as either a string or a list of
    content blocks (Anthropic: ``[{"type":"text","text":"..."}]``). An empty
    block list was being stringified to ``"[]"`` and stored verbatim as the
    assistant message — the "only response is []" bug. This extracts the text.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(raw)


def _parse_assistant_payload(
    raw: Any, finding: Finding, *, allow_defaults: bool = True
) -> tuple[str, dict[str, Any]]:
    """Extract narrative text + executable agent action proposals.

    The prompt asks for JSON, but providers occasionally wrap it in prose or
    fences. Parse failure falls back to deterministic built-in tool actions
    derived from the finding target/summary — but ONLY on the first turn
    (``allow_defaults``). On later turns an empty actions array is respected
    so the assistant can legitimately propose nothing new (no re-suggestion).
    """
    text = _coerce_llm_content(raw).strip()
    parsed: Any | None = None
    candidates = [text]
    if "```" in text:
        parts = text.split("```")
        candidates.extend(part.strip().removeprefix("json").strip() for part in parts)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(parsed, dict):
        # Model returned non-JSON (or an empty/array response like "[]").
        # Don't store a bare "[]"/empty string as the bubble — surface a
        # graceful note + still offer default actions on the first turn.
        body = text if text and text not in ("[]", "{}") else (
            "I couldn't generate a structured response that turn. "
            "Try rephrasing, or ask me to suggest agent actions for this finding."
        )
        return body, {
            "actions": _default_agent_actions(finding) if allow_defaults else []
        }

    answer = str(parsed.get("answer") or "").strip() or text
    actions = _normalize_actions(parsed.get("actions"))
    actions = [a for a in actions if a.get("type") in _ALLOWED_ACTION_TYPES]
    if not actions and allow_defaults:
        actions = _default_agent_actions(finding)
    return answer, {"actions": actions}


def _default_agent_actions(finding: Finding) -> list[dict[str, Any]]:
    """Deterministic executable tool actions for thin/prose LLM responses."""
    text = " ".join(
        str(part or "")
        for part in [finding.title, finding.target, finding.summary, finding.details]
    )
    ips = list(dict.fromkeys(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)))
    host_candidates = re.findall(
        r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b",
        text,
    )
    hosts = [h for h in dict.fromkeys(host_candidates) if not re.match(r"^\d+\.", h)]
    targets = ips or hosts[:3]
    if not targets and finding.target:
        targets = [finding.target]

    ports = _extract_ports(text)
    actions: list[dict[str, Any]] = []
    for target in targets[:3]:
        params: dict[str, Any] = {
            "tool": "service_detect",
            "task_kind": "enum",
            "target": target,
            "args": ({"ports": ports} if ports else {}),
        }
        actions.append(
            {
                "type": "run_tool",
                "title": f"Fingerprint services on {target}",
                "description": (
                    "Dispatch the built-in service_detect agent tool to grab "
                    "banners/TLS/HTTP fingerprints for this target. Active "
                    "tools still stop at the approval gate before execution."
                ),
                "params": params,
                "status": "proposed",
            }
        )
    if actions:
        return actions
    return [
        {
            "type": "run_tool",
            "title": "Probe HTTP surface",
            "description": (
                "Dispatch the built-in httpx_probe agent tool against the "
                "finding target."
            ),
            "params": {
                "tool": "httpx_probe",
                "task_kind": "enum",
                "target": finding.target or "",
                "args": {},
            },
            "status": "proposed",
        }
    ]


def _extract_ports(text: str) -> str | None:
    common = {"22", "25", "80", "443", "993", "2082", "2083", "2086", "2087", "2095", "2096"}
    found = set(re.findall(r"(?<!\d)(\d{2,5})(?!\d)", text)) & common
    if not found:
        return None
    return ",".join(sorted(found, key=lambda p: int(p)))


def _normalize_actions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "").strip().lower()
        if typ not in _ALLOWED_ACTION_TYPES:
            continue
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        params = _normalize_action_params(typ, params)
        if typ == "run_tool":
            tool_name = str(params.get("tool") or "")
            target = str(params.get("target") or "")
            if not tool_name or not target or get_tool(tool_name) is None:
                continue
        title = str(item.get("title") or typ.replace("_", " ").title()).strip()[:120]
        description = str(item.get("description") or "").strip()[:1000]
        out.append(
            {
                "type": typ,
                "title": title,
                "description": description,
                "params": params,
                "status": "proposed",
            }
        )
    return out


def _normalize_action_params(typ: str, params: dict[str, Any]) -> dict[str, Any]:
    if typ == "tag_incident":
        tags = params.get("tags") if isinstance(params.get("tags"), list) else []
        return {"tags": _normalize_tags([str(t) for t in tags])[:5]}
    if typ == "add_finding":
        severity = str(params.get("severity") or "info").lower()
        phase = str(params.get("phase") or "general").lower()
        return {
            "title": str(params.get("title") or "AI-suggested finding").strip()[:300],
            "summary": str(params.get("summary") or "").strip()[:4000],
            "severity": severity if severity in _ALLOWED_SEVERITIES else "info",
            "phase": phase if phase in _ALLOWED_PHASES else "general",
            "target": str(params.get("target") or "").strip()[:500] or None,
        }
    if typ == "run_tool":
        task_kind = str(params.get("task_kind") or "enum").lower()
        tool_args = params.get("args") if isinstance(params.get("args"), dict) else {}
        return {
            "tool": str(params.get("tool") or "").strip()[:120],
            "task_kind": task_kind if task_kind in _ALLOWED_TASK_KINDS else "enum",
            "target": str(params.get("target") or "").strip()[:500] or None,
            "args": tool_args,
        }
    if typ == "add_scope":
        kind = str(params.get("kind") or "domain").strip().lower()
        return {
            "value": str(params.get("value") or "").strip()[:500],
            "kind": kind if kind in {"domain", "ip", "cidr", "url"} else "domain",
            "note": str(params.get("note") or "").strip()[:500] or None,
        }
    return dict(params)


def _normalize_tags(raw: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in raw:
        cleaned = tag.strip()[:40]
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out[:20]


def generate_finding_chat_reply(
    session: Session,
    redis_client: Any,
    *,
    finding: Finding,
    conversation: Conversation,
    acting_user_id: uuid.UUID,
) -> tuple[AgentExecution, ConversationMessage]:
    """Ask the BYO LLM for the assistant response and persist the bubble."""
    provider, model_name = default_provider_model()
    resolved = resolve_for_user(redis_client, user_id=acting_user_id, provider=provider)
    llm = _make_chat_model(
        provider,
        model_name,
        api_key=resolved.api_key,
        endpoint=resolved.endpoint,
    )

    dossier = build_finding_dossier(session, finding)
    messages = get_conversation_messages(session, conversation.id)
    prompt = (
        "Finding dossier JSON:\n"
        f"{json.dumps(dossier, default=str, indent=2)[:30000]}\n\n"
        "Conversation so far:\n"
        f"{_conversation_history_prompt(session, messages) or '(first turn)'}\n\n"
        "Respond to the analyst's latest message."
    )

    execution = AgentExecution(
        engagement_id=finding.engagement_id,
        agent=AgentName.strategic,
        trigger=AgentTrigger.manual,
        input={
            "finding_id": str(finding.id),
            "conversation_id": str(conversation.id),
            "mode": "finding_chat",
        },
        model_provider=provider,
        model_name=model_name,
        status=AgentExecutionStatus.running,
        started_at=datetime.now(tz=UTC),
    )
    session.add(execution)
    session.commit()
    session.refresh(execution)

    try:
        response = llm.invoke([("system", _SYSTEM_PROMPT), ("user", prompt)])
        # Some providers intermittently stop after ~1 token (no usable
        # output). Detect via near-zero output tokens and retry once — the
        # next call almost always returns the full answer.
        _tin, _tout = _extract_usage(response)
        if (_tout or 0) < 8:
            response = llm.invoke(
                [("system", _SYSTEM_PROMPT), ("user", prompt)]
            )
        # Defaults (deterministic tool proposals) only on the first turn;
        # once any action exists in the thread, respect an empty array so
        # the assistant isn't forced to re-propose.
        allow_defaults = not any(
            isinstance(m.action_payload, dict)
            and m.action_payload.get("actions")
            for m in messages
        )
        content, action_payload = _parse_assistant_payload(
            response.content, finding, allow_defaults=allow_defaults
        )
        tokens_in, tokens_out = _extract_usage(response)
        execution.status = AgentExecutionStatus.completed
        execution.completed_at = datetime.now(tz=UTC)
        execution.tokens_in = tokens_in
        execution.tokens_out = tokens_out
        execution.cost_usd = pricing.cost_usd(
            model_name, tokens_in, tokens_out, provider=provider
        )
        execution.output = {"message_chars": len(content)}
        assistant = ConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=content,
            action_payload=action_payload,
            execution_id=execution.id,
        )
        conversation.updated_at = datetime.now(tz=UTC)
        session.add(assistant)
        session.commit()
        session.refresh(execution)
        session.refresh(assistant)
        return execution, assistant
    except Exception as exc:
        execution.status = AgentExecutionStatus.failed
        execution.completed_at = datetime.now(tz=UTC)
        execution.error = str(exc)[:1000]
        session.commit()
        raise


_SUMMARY_PROMPT = (
    "Summarize this finding-scoped AI assistant conversation for the engagement "
    "record. Capture: what was discussed, which actions were proposed and their "
    "outcome (accepted/denied/run result), any new findings or scope items "
    "surfaced, and open questions. 3-6 sentences, plain text, no headers. "
    "Do not invent details not present in the transcript."
)


def summarize_finding_chat(
    session: Session,
    redis_client: Any,
    *,
    finding: Finding,
    conversation: Conversation,
    acting_user_id: uuid.UUID,
) -> tuple[str, int]:
    """LLM-summarize a conversation so closing it leaves a reviewable record.

    Writes an audit_log row (``finding.chat_summarized``) carrying the summary
    so the activity timeline can surface it as a clickable entry. Returns
    ``(summary, message_count)``. Falls back to a deterministic digest if the
    BYO key is missing or the LLM call fails so close still works.
    """
    from app.models import ActorType, AuditLog

    messages = get_conversation_messages(session, conversation.id)
    transcript = "\n".join(
        f"{m.role.upper()}: {m.content[:800]}" for m in messages if m.content.strip()
    )
    message_count = len(messages)

    summary = _fallback_summary(messages)
    try:
        provider, model_name = default_provider_model()
        resolved = resolve_for_user(
            redis_client, user_id=acting_user_id, provider=provider
        )
        llm = _make_chat_model(
            provider,
            model_name,
            api_key=resolved.api_key,
            endpoint=resolved.endpoint,
        )
        raw = llm.invoke(
            [
                ("system", _SUMMARY_PROMPT),
                ("user", f"Transcript:\n{transcript[:20000]}"),
            ]
        )
        text = _coerce_llm_content(raw.content)
        if text.strip():
            summary = text.strip()
    except Exception:  # noqa: BLE001 — close must still succeed without an LLM
        pass

    session.add(
        AuditLog(
            engagement_id=finding.engagement_id,
            actor_type=ActorType.user,
            actor_id=str(acting_user_id),
            event_type="finding.chat_summarized",
            payload={
                "finding_id": str(finding.id),
                "conversation_id": str(conversation.id),
                "message_count": message_count,
                "summary": summary,
            },
        )
    )
    session.commit()
    return summary, message_count


def _fallback_summary(messages: list[ConversationMessage]) -> str:
    if not messages:
        return "AI session closed — no messages were exchanged."
    proposed = accepted = denied = 0
    for m in messages:
        payload = m.action_payload if isinstance(m.action_payload, dict) else {}
        for a in payload.get("actions", []):
            status = a.get("status") if isinstance(a, dict) else None
            if status == "accepted":
                accepted += 1
            elif status == "denied":
                denied += 1
            else:
                proposed += 1
    return (
        f"AI session with {len(messages)} message(s). "
        f"Actions: {accepted} approved, {denied} denied, {proposed} left proposed."
    )


def accept_chat_action(
    session: Session,
    *,
    finding: Finding,
    message: ConversationMessage,
    action_index: int,
    acting_user_id: uuid.UUID,
    redis_client: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    """Apply one consent-gated action bubble.

    The LLM only writes inert JSON. This function is the narrow allow-list that
    turns an approved bubble into a dashboard mutation or an agent task
    dispatch. Destructive/exploit dispatch remains out of scope.
    """
    payload = message.action_payload if isinstance(message.action_payload, dict) else {}
    actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    if action_index >= len(actions) or not isinstance(actions[action_index], dict):
        raise ValueError("action not found")
    action = dict(actions[action_index])
    if action.get("status") != "proposed":
        raise ValueError(f"action is {action.get('status')}; cannot accept")

    typ = str(action.get("type") or "")
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    result: dict[str, Any]

    if typ == "tag_incident":
        result = _accept_tag_incident(finding, params)
    elif typ == "add_finding":
        result = _accept_add_finding(session, finding, params, acting_user_id)
    elif typ == "add_scope":
        result = _accept_add_scope(session, finding, params)
    elif typ == "next_step":
        result = _accept_next_step(session, finding, action, acting_user_id)
    elif typ == "run_tool":
        if redis_client is None:
            raise ValueError("run_tool action requires redis dispatch context")
        result = _accept_run_tool(
            session, finding, action, acting_user_id, redis_client
        )
    elif typ == "context":
        result = {"noop": True, "message": "Context acknowledged."}
    else:
        raise ValueError(f"unsupported action type: {typ}")

    action["status"] = "accepted"
    action["result"] = result
    actions[action_index] = action
    message.action_payload = {"actions": actions}
    flag_modified(message, "action_payload")
    session.add(message)
    return typ, result


def deny_chat_action(
    session: Session,
    *,
    message: ConversationMessage,
    action_index: int,
) -> tuple[str, dict[str, Any]]:
    """Mark one proposed action bubble as denied (no dispatch).

    Denied actions stay in the ledger so the assistant sees the analyst
    declined them and doesn't re-propose.
    """
    payload = message.action_payload if isinstance(message.action_payload, dict) else {}
    actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    if action_index >= len(actions) or not isinstance(actions[action_index], dict):
        raise ValueError("action not found")
    action = dict(actions[action_index])
    if action.get("status") not in ("proposed", None):
        raise ValueError(f"action is {action.get('status')}; cannot deny")
    action["status"] = "denied"
    actions[action_index] = action
    message.action_payload = {"actions": actions}
    flag_modified(message, "action_payload")
    session.add(message)
    return str(action.get("type") or ""), {"denied": True}

def _accept_add_scope(
    session: Session,
    finding: Finding,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Mint a scope item the AI surfaced, marked source='found' so the scope
    editor highlights it as discovered-during-engagement (#94)."""
    value = str(params.get("value") or "").strip()
    if not value:
        raise ValueError("add_scope action missing value")
    kind = str(params.get("kind") or "domain")
    try:
        scope_kind = ScopeKind(kind)
    except ValueError:
        scope_kind = ScopeKind.domain
    item = ScopeItem(
        engagement_id=finding.engagement_id,
        kind=scope_kind,
        value=value,
        is_exclusion=False,
        note=str(params.get("note") or "Added during engagement via AI assistant"),
        source="found",
    )
    session.add(item)
    session.flush()
    return {
        "scope_item_id": str(item.id),
        "kind": scope_kind.value,
        "value": value,
        "source": "found",
    }



def _accept_tag_incident(finding: Finding, params: dict[str, Any]) -> dict[str, Any]:
    tags = params.get("tags") if isinstance(params.get("tags"), list) else []
    merged = _normalize_tags([*(finding.tags or []), *[str(t) for t in tags]])
    finding.tags = merged
    return {"tags": merged}


def _accept_add_finding(
    session: Session,
    finding: Finding,
    params: dict[str, Any],
    acting_user_id: uuid.UUID,
) -> dict[str, Any]:
    phase = FindingPhase(str(params.get("phase") or "general"))
    severity = Severity(str(params.get("severity") or "info"))
    now = datetime.now(tz=UTC)
    status = (
        FindingStatus.validated
        if phase == FindingPhase.osint
        else FindingStatus.pending_validation
    )
    created = Finding(
        engagement_id=finding.engagement_id,
        title=str(params.get("title") or "AI-suggested finding")[:300],
        summary=str(params.get("summary") or "") or None,
        severity=severity,
        phase=phase,
        target=params.get("target") if params.get("target") else finding.target,
        source_tool="ai_assistant",
        details={
            "source": "finding_chat_action",
            "parent_finding_id": str(finding.id),
            "created_by_user_id": str(acting_user_id),
        },
        status=status,
        validated_at=now if status == FindingStatus.validated else None,
        validated_by=acting_user_id if status == FindingStatus.validated else None,
    )
    session.add(created)
    session.flush()
    return {"finding_id": str(created.id), "title": created.title}


def _accept_next_step(
    session: Session,
    finding: Finding,
    action: dict[str, Any],
    acting_user_id: uuid.UUID,
) -> dict[str, Any]:
    suggestion = Suggestion(
        engagement_id=finding.engagement_id,
        finding_id=finding.id,
        title=str(action.get("title") or "Suggested next step")[:300],
        body=str(action.get("description") or "") or None,
        kind=SuggestionKind.note,
        payload={
            "source": "finding_chat_action",
            "approved_by": str(acting_user_id),
            "params": action.get("params") if isinstance(action.get("params"), dict) else {},
        },
        status=SuggestionStatus.open,
        created_by_agent=AgentName.strategic,
    )
    session.add(suggestion)
    session.flush()
    return {"suggestion_id": str(suggestion.id), "kind": suggestion.kind.value}


def _accept_run_tool(
    session: Session,
    finding: Finding,
    action: dict[str, Any],
    acting_user_id: uuid.UUID,
    redis_client: Any,
) -> dict[str, Any]:
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    tool_name = str(params.get("tool") or "").strip()
    if not tool_name:
        raise ValueError("run_tool action missing tool")
    spec = get_tool(tool_name)
    if spec is None:
        raise ValueError(f"unknown built-in agent tool: {tool_name}")
    task_kind = str(params.get("task_kind") or "enum")
    if task_kind not in _ALLOWED_TASK_KINDS:
        raise ValueError("run_tool action must be enum or scan")
    target = str(params.get("target") or finding.target or "").strip()
    if not target:
        raise ValueError("run_tool action missing target")
    args = params.get("args") if isinstance(params.get("args"), dict) else {}
    payload = {
        "source": "finding_chat_action",
        "approved_by": str(acting_user_id),
        "tool": tool_name,
        "target": target,
        "args": args,
        "task_kind": task_kind,
        "owner_eligibility": "agent",
    }
    # Tactical's deterministic dispatcher reads payload['tool'] and
    # payload['target']; keep extra args for trace/future prompt expansion.
    task = Task(
        engagement_id=finding.engagement_id,
        finding_id=finding.id,
        title=str(action.get("title") or f"Run {tool_name} on {target}")[:300],
        kind=TaskKind(task_kind),
        owner_eligibility=OwnerEligibility.agent,
        status=TaskStatus.pending,
        payload=payload,
    )
    session.add(task)
    session.flush()
    dispatched = False
    run_id: str | None = None
    try:
        thread_id = TacticalAgent(redis_client).dispatch(
            session,
            task=task,
            trigger=AgentTrigger.manual,
            acting_user_id=acting_user_id,
        )
        dispatched = True
        run_id = str(thread_id)
    except TacticalRefusedExploit:
        dispatched = False
    return {
        "task_id": str(task.id),
        "tool": tool_name,
        "target": target,
        "risk": spec.risk.value,
        "dispatched": dispatched,
        "run_id": run_id,
        "note": (
            "Dispatched to the existing agent run path. Active tools still "
            "pause at the approval gate before execution."
            if dispatched
            else "Task created but not dispatched."
        ),
    }
