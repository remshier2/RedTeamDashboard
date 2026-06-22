"""Tool packs: default tool/prompt bundles Strategic chooses from when
provisioning an MCP lease for a Task.

Stage 1 keeps this dead simple: lookup by ``TaskKind``. Stage 3 will let
the Strategic LLM override the default bundle per-task. The packs are
intentionally small and conservative — the charter is "agents enumerate
and scan", so the exploit pack is empty by design and ``Tactical.dispatch``
hard-refuses kind=exploit before this is ever consulted.

The bundles deliberately overlap (``httpx_probe`` is in both enum and scan)
because the same tool genuinely serves both phases. The MCP lease records
the bundle as a snapshot at mint time — future edits here don't retro-affect
live runs.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Engagement, Finding, ScopeItem, Task, TaskKind

TOOL_PACKS: dict[TaskKind, list[str]] = {
    TaskKind.enum: [
        "subfinder",
        "crt_sh",
        "dns_lookup",
        "reverse_dns",
        "whois_lookup",
        "httpx_probe",
    ],
    TaskKind.scan: [
        "portscan",
        "subnet_sweep",
        "service_detect",
        "httpx_probe",
    ],
    # Charter invariant — agents never exploit. Provisioning a lease for
    # exploit-kind would normally never happen (Tactical refuses it earlier);
    # the empty pack is a defense-in-depth fallback.
    TaskKind.exploit: [],
}

PROMPT_PACKS: dict[TaskKind, list[str]] = {
    TaskKind.enum: ["passive_recon"],
    TaskKind.scan: ["active_enum"],
    TaskKind.exploit: [],
}


def tools_for_task(task: Task) -> list[str]:
    """Default tool list for ``task.kind``. Returns a fresh list so callers
    can mutate without poisoning the registry."""
    return list(TOOL_PACKS.get(task.kind, []))


def prompts_for_task(task: Task) -> list[str]:
    """Default prompt keys for ``task.kind``."""
    return list(PROMPT_PACKS.get(task.kind, []))


def context_for_task(session: Session, task: Task) -> dict[str, Any]:
    """Curated context dict exposed to the Execution Agent via the
    ``lease://current`` MCP resource.

    Keeps the agent grounded without forcing it to issue a bunch of
    ``resources/read`` calls just to learn the basics. Engagement + scope
    are always present; finding is included only when ``task.finding_id``
    is set (i.e. the task was suggested off a specific finding).
    """
    engagement = session.get(Engagement, task.engagement_id)
    scope_items = list(
        session.scalars(
            select(ScopeItem).where(ScopeItem.engagement_id == task.engagement_id)
        )
    )
    ctx: dict[str, Any] = {
        "engagement": (
            {
                "slug": engagement.slug,
                "name": engagement.name,
                "description": engagement.description,
            }
            if engagement is not None
            else None
        ),
        "scope": [
            {
                "kind": item.kind.value,
                "value": item.value,
                "is_exclusion": item.is_exclusion,
            }
            for item in scope_items
        ],
        "task": {
            "id": str(task.id),
            "title": task.title,
            "kind": task.kind.value,
            "payload": dict(task.payload or {}),
        },
    }
    if task.finding_id is not None:
        finding = session.get(Finding, task.finding_id)
        if finding is not None:
            ctx["finding"] = {
                "id": str(finding.id),
                "title": finding.title,
                "target": finding.target,
                "severity": finding.severity.value,
                "phase": finding.phase.value,
            }
    return ctx
