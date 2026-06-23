"""LangGraph runtime for the OSINT agent.

Topology::

    START ──▶ agent ──▶ (tool_dispatch ──▶ agent)* ──▶ END

The agent node calls the LLM. If the response carries ``tool_calls``, the graph
routes to ``tool_dispatch``, which runs each call through the scope+approval
gate before either executing the impl, recording a denial, or calling
``interrupt()`` for human approval. The dispatch node then loops back to the
agent so it can react to the ``ToolMessage`` results. The loop terminates when
the agent returns an ``AIMessage`` with no tool_calls.

Phase 0: the interrupt branch reports pending approvals in state but does not
yet persist an Approval row to the DB — that lands with the approvals API.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.models import RiskLevel, ScopeKind
from app.orchestrator.gate import Action, ScopeDecision, evaluate
from app.orchestrator.scope import ScopeSnapshot, normalize_scope_items
from app.orchestrator.state import OsintState
from app.orchestrator.tools import ToolSpec, get_tool
from app.orchestrator.tools.runtime import ToolImpl, ToolResult, run_tool

# Stage 1.5 MCP execution: when set on the dispatch node, tool calls are
# routed to the MCP server over SSE instead of the local IMPLEMENTATIONS
# registry. Same ``(name, args) -> ToolResult`` contract as ``run_tool`` so
# the dispatch logic (gate, interrupt, fan-out) stays identical.
MCPExecutor = Callable[[str, Mapping[str, Any]], ToolResult]

# Custom types we serialize into graph state. Explicit allowlisting silences
# LangGraph's "unregistered type" warning at deserialize and future-proofs us
# against LANGGRAPH_STRICT_MSGPACK=true. All standard types (langchain
# messages, UUID, datetime, Interrupt, Command, etc.) are already in the
# built-in SAFE_MSGPACK_TYPES list.
CUSTOM_MSGPACK_TYPES: tuple[tuple[str, str], ...] = (
    ("app.models.scope_item", "ScopeKind"),
    ("app.orchestrator.scope", "ScopeSnapshot"),
)

# Looks up a standing session grant: given an engagement and tool name, returns
# the authorization id covering active calls to that tool, or None. Injected
# into the graph so the dispatch node stays free of DB imports (worker provides
# a DB-backed one; tests pass a static stub).
Authorizer = Callable[[UUID | None, str], UUID | None]


def custom_serde() -> JsonPlusSerializer:
    """Shared by MemorySaver (tests) and PostgresSaver (prod worker) so the
    msgpack allowlist is one source of truth."""
    return JsonPlusSerializer(allowed_msgpack_modules=CUSTOM_MSGPACK_TYPES)


def _default_checkpointer() -> MemorySaver:
    return MemorySaver(serde=custom_serde())


def _agent_node(state: OsintState, llm: Any) -> dict[str, Any]:
    response = llm.invoke(state["messages"])
    return {"messages": [response]}


def _tool_payload(result: ToolResult) -> str:
    if result.ok:
        return json.dumps(result.data, default=str)
    return json.dumps({"error": result.error or "unknown error"})


def _execute_tool(
    name: str,
    args: Mapping[str, Any],
    *,
    mcp_executor: MCPExecutor | None,
    implementations: Mapping[str, ToolImpl] | None,
) -> ToolResult:
    """Single tool invocation: MCP path when an executor is wired, local
    ``run_tool`` otherwise. Centralized so the batched-passive fan-out and
    the main loop share the routing decision."""
    if mcp_executor is not None:
        return mcp_executor(name, args)
    return run_tool(name, args, implementations=implementations)


def _tool_dispatch_node(
    state: OsintState,
    *,
    registry: Mapping[str, ToolSpec] | None,
    implementations: Mapping[str, ToolImpl] | None,
    authorizer: Authorizer | None = None,
    mcp_executor: MCPExecutor | None = None,
) -> dict[str, Any]:
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {}

    scope_items = normalize_scope_items(state.get("scope_items"))
    engagement_id = state.get("engagement_id")

    out_messages: list[Any] = []
    out_findings: list[dict[str, Any]] = []
    out_denials: list[dict[str, Any]] = []
    out_pending: list[dict[str, Any]] = []
    out_auto_approvals: list[dict[str, Any]] = []

    for call in last.tool_calls:
        name = call["name"]
        args = dict(call.get("args") or {})
        call_id = call.get("id") or ""

        # Llama-class models sometimes pack multiple targets into one call:
        # either a JSON list `{"domain": ["a.com", "b.com"]}` or a delimited
        # string `{"domain": "a.com, b.com"}`. The schema declares a single
        # string, but they batch anyway. We fan either form out into N
        # single-target sub-calls (passive tools only; active needs per-target
        # human approval and the interrupt UX for batched approvals is messy).
        spec = get_tool(name, registry=registry)
        if spec is not None and spec.risk is RiskLevel.passive:
            batched = _coerce_target_list(args.get(spec.target_arg), spec)
            if batched is not None:
                update_chunk = _dispatch_batched_passive(
                    name=name,
                    call_id=call_id,
                    args=args,
                    targets=batched,
                    spec=spec,
                    scope_items=scope_items,
                    registry=registry,
                    implementations=implementations,
                    mcp_executor=mcp_executor,
                )
                out_messages.extend(update_chunk["messages"])
                out_findings.extend(update_chunk["findings"])
                out_denials.extend(update_chunk["denials"])
                continue

        # Tools that scan by IP but accept hostnames (e.g. portscan) resolve
        # here, BEFORE the gate, so we authorize the exact address we touch.
        # The resolved IP replaces the target arg; the original host is kept
        # under `resolved_from` for the approval display + finding context.
        if spec is not None and spec.resolve_host:
            raw_target = args.get(spec.target_arg)
            if isinstance(raw_target, str) and raw_target.strip():
                host = raw_target.strip()
                resolved = _resolve_to_ip(host)
                if resolved is None:
                    reason = f"could not resolve host {host!r} to an IP address"
                    out_denials.append(
                        {
                            "tool": name,
                            "args": args,
                            "reason": reason,
                            "scope": ScopeDecision(
                                ok=False, reason=reason, target=host
                            ).to_jsonable(),
                        }
                    )
                    out_messages.append(
                        ToolMessage(
                            content=json.dumps({"denied": reason}),
                            tool_call_id=call_id,
                        )
                    )
                    continue
                args = {**args, spec.target_arg: resolved}
                if resolved != host:
                    args["resolved_from"] = host

        # A standing session grant for this tool (per engagement) auto-approves
        # an otherwise-interrupting active call. The gate still enforces scope.
        authorization_id = None
        if (
            authorizer is not None
            and spec is not None
            and spec.risk in (RiskLevel.active, RiskLevel.destructive)
        ):
            authorization_id = authorizer(engagement_id, name)

        decision = evaluate(
            name,
            args,
            scope_items,
            authorization_id=authorization_id,
            registry=registry,
        )

        if decision.action is Action.deny:
            out_denials.append(
                {
                    "tool": name,
                    "args": args,
                    "reason": decision.reason,
                    "scope": decision.scope.to_jsonable(),
                }
            )
            out_messages.append(
                ToolMessage(
                    content=json.dumps({"denied": decision.reason}),
                    tool_call_id=call_id,
                )
            )
            continue

        if decision.action is Action.interrupt:
            request = {
                "tool": name,
                "args": args,
                "risk": decision.risk.value if decision.risk else None,
                "scope": decision.scope.to_jsonable(),
                "tool_call_id": call_id,
            }
            resume_value = interrupt(request)
            out_pending.append({**request, "resolved": True})

            approved = isinstance(resume_value, dict) and bool(resume_value.get("approved"))
            if not approved:
                reason = (
                    resume_value.get("reason")
                    if isinstance(resume_value, dict)
                    else None
                ) or "human denied the request"
                out_denials.append(
                    {
                        "tool": name,
                        "args": args,
                        "reason": reason,
                        "scope": decision.scope.to_jsonable(),
                    }
                )
                out_messages.append(
                    ToolMessage(
                        content=json.dumps({"denied": reason}),
                        tool_call_id=call_id,
                    )
                )
                continue

            edited = resume_value.get("edited_args") if isinstance(resume_value, dict) else None
            effective_args = dict(edited) if isinstance(edited, dict) else args
        else:
            effective_args = args
            # Auto-approved an active/destructive call via a session grant —
            # record it so the worker writes the (still-required) audit entry.
            if (
                decision.authorization_id is not None
                and spec is not None
                and spec.risk is not RiskLevel.passive
            ):
                out_auto_approvals.append(
                    {
                        "tool": name,
                        "args": args,
                        "authorization_id": str(decision.authorization_id),
                        "risk": decision.risk.value if decision.risk else None,
                    }
                )

        # Range tools (e.g. subnet_sweep) get the engagement's ip/cidr scope
        # exclusions injected so they skip carved-out hosts inside the approved
        # CIDR. The single CIDR approval covers the range; exclusions are still
        # enforced per host by the tool.
        if spec is not None and spec.inject_exclusions:
            effective_args = {
                **effective_args,
                "exclude": [
                    item.value
                    for item in scope_items
                    if item.is_exclusion
                    and item.kind in (ScopeKind.ip, ScopeKind.cidr)
                ],
            }

        result = _execute_tool(
            name,
            effective_args,
            mcp_executor=mcp_executor,
            implementations=implementations,
        )
        out_messages.append(
            ToolMessage(content=_tool_payload(result), tool_call_id=call_id)
        )
        if result.ok:
            out_findings.extend(_expand_findings(name, effective_args, result))

    update: dict[str, Any] = {"messages": out_messages}
    if out_findings:
        update["findings"] = out_findings
    if out_denials:
        update["denials"] = out_denials
    if out_pending:
        update["pending"] = out_pending
    if out_auto_approvals:
        update["auto_approvals"] = out_auto_approvals
    return update


def _expand_findings(
    name: str, effective_args: Mapping[str, Any], result: ToolResult
) -> list[dict[str, Any]]:
    """Fan one tool result into N findings (one per ``result.findings`` entry).

    Tools that opt into per-row findings (portscan, subnet_sweep, service_detect)
    set ``result.findings`` so each open port / probe becomes its own row with
    its own severity and title. Tools that don't set it fall back to one
    info-severity row built from ``result.data`` — the original behavior.
    """
    if result.findings:
        return [
            {
                "tool": name,
                "args": dict(effective_args),
                "target": item.get("target"),
                "severity": item.get("severity") or "info",
                "title": item.get("title"),
                "data": item.get("data") or {},
            }
            for item in result.findings
        ]
    return [
        {
            "tool": name,
            "args": dict(effective_args),
            "target": None,
            "severity": "info",
            "title": None,
            "data": result.data,
        }
    ]


def _resolve_to_ip(host: str) -> str | None:
    """Resolve a hostname to an IP, or return the input unchanged if it is
    already an IP literal. Returns ``None`` if the hostname can't be resolved.

    Runs in the (synchronous) dispatch node before the scope gate so we
    authorize and act on the same address. Uses stdlib ``getaddrinfo`` — no
    extra DNS dependency, and it honors the host's resolver config (so docker
    service names resolve inside the compose network).
    """
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return None
    for info in infos:
        addr = info[4][0]
        try:
            return str(ipaddress.ip_address(addr.split("%", 1)[0]))
        except ValueError:
            continue
    return None


_BATCH_SPLIT_DELIMITED = re.compile(r"[,\s]+")
_BATCH_SPLIT_WHITESPACE = re.compile(r"\s+")


def _coerce_target_list(raw: Any, spec: ToolSpec) -> list[str] | None:
    """Normalize a possibly-batched target arg into a list of targets.

    Models batch multiple targets into one tool call in two shapes: a JSON
    list (``["a.com", "b.com"]``) or a delimited string (``"a.com, b.com"``).
    Both collapse to the same per-target fan-out. Returns ``None`` when the arg
    is a single target (or unusable), so the caller keeps the normal
    single-target path and its one clean ToolMessage.

    Domains/IPs/CIDRs never contain commas or whitespace, so splitting a string
    on either is safe. URLs may legitimately carry commas (query strings), so
    URL batches are split on whitespace only.
    """
    if isinstance(raw, list):
        items = [str(t).strip() for t in raw if isinstance(t, (str, int))]
        items = [t for t in items if t]
        return items or None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        splitter = (
            _BATCH_SPLIT_WHITESPACE
            if spec.kind is ScopeKind.url
            else _BATCH_SPLIT_DELIMITED
        )
        parts = [p for p in splitter.split(text) if p]
        return parts if len(parts) > 1 else None
    return None


def _dispatch_batched_passive(
    *,
    name: str,
    call_id: str,
    args: dict[str, Any],
    targets: list[str],
    spec: ToolSpec,
    scope_items: list[ScopeSnapshot],
    registry: Mapping[str, ToolSpec] | None,
    implementations: Mapping[str, ToolImpl] | None,
    mcp_executor: MCPExecutor | None = None,
) -> dict[str, list[Any]]:
    """Fan-out a single tool_call across a list of (already-normalized) targets.

    Each target becomes an independent gate evaluation + tool run. Findings and
    denials are accumulated independently so the UI surfaces them per-target. We
    respond to the original tool_call with ONE ToolMessage that has a per-target
    results array — preserving the AIMessage <-> ToolMessage pairing invariant.
    """
    messages: list[Any] = []
    findings: list[dict[str, Any]] = []
    denials: list[dict[str, Any]] = []
    per_target: list[dict[str, Any]] = []

    for target in targets:
        sub_args = {**args, spec.target_arg: target}
        decision = evaluate(name, sub_args, scope_items, registry=registry)

        if decision.action is Action.deny:
            denials.append(
                {
                    "tool": name,
                    "args": sub_args,
                    "reason": decision.reason,
                    "scope": decision.scope.to_jsonable(),
                }
            )
            per_target.append({"target": target, "denied": decision.reason})
            continue

        result = _execute_tool(
            name,
            sub_args,
            mcp_executor=mcp_executor,
            implementations=implementations,
        )
        if result.ok:
            findings.extend(_expand_findings(name, sub_args, result))
            per_target.append({"target": target, "data": result.data})
        else:
            per_target.append({"target": target, "error": result.error})

    messages.append(
        ToolMessage(
            content=json.dumps(
                {
                    "fanned_out": True,
                    "targets": len(targets),
                    "per_target": per_target,
                }
            ),
            tool_call_id=call_id,
        )
    )

    return {"messages": messages, "findings": findings, "denials": denials}


def _should_continue(state: OsintState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tool_dispatch"
    return END


def build_graph(
    *,
    llm: Any,
    checkpointer: BaseCheckpointSaver | None = None,
    registry: Mapping[str, ToolSpec] | None = None,
    implementations: Mapping[str, ToolImpl] | None = None,
    authorizer: Authorizer | None = None,
    mcp_executor: MCPExecutor | None = None,
) -> Any:
    """Compile and return the OSINT StateGraph.

    The ``llm`` must already be bound to its tool schemas (see
    ``app.orchestrator.llm.default_llm``). Tests pass a fake whose ``invoke``
    returns scripted ``AIMessage`` instances. ``registry`` / ``implementations``
    let tests inject extra tools (e.g. an active tool to exercise the interrupt
    path) without mutating package globals. ``authorizer`` resolves standing
    session grants so covered active calls auto-approve instead of interrupting;
    omit it and every active/destructive call interrupts for a human.

    ``mcp_executor`` (Stage 1.5) routes every tool invocation through the MCP
    server over SSE instead of the local ``IMPLEMENTATIONS`` registry. The
    scope gate, interrupt-for-active, fan-out, and host resolution all stay
    in-process — only the *execution* of the tool moves. When omitted, runs
    use the legacy local-execution path (no lease, no MCP url on envelope).
    """
    builder: StateGraph = StateGraph(OsintState)
    builder.add_node("agent", lambda s: _agent_node(s, llm=llm))
    builder.add_node(
        "tool_dispatch",
        lambda s: _tool_dispatch_node(
            s,
            registry=registry,
            implementations=implementations,
            authorizer=authorizer,
            mcp_executor=mcp_executor,
        ),
    )
    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent",
        _should_continue,
        {"tool_dispatch": "tool_dispatch", END: END},
    )
    builder.add_edge("tool_dispatch", "agent")
    return builder.compile(checkpointer=checkpointer or _default_checkpointer())
