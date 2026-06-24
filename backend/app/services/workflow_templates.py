"""Workflow templates service — Phase 10.

Owns the seed of the ``is_system=true`` starter packs (CHARTER §16
RESOLVED: Network Recon, OSINT Enum, Web App) plus the ``apply``
operation that turns one template + one target into N Tasks for an
engagement.

The seed function is **idempotent** on ``(name, is_system=True)``:
re-running it does nothing if the named template already exists. We
deliberately do NOT mutate an existing system template's steps even if
the code constant changed — the analyst's in-flight engagements rely
on stable template shapes. If you need to update a starter template,
rename it (add v2) and let the old row stay; or write a one-off
migration that rebuilds it explicitly.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Engagement,
    OwnerEligibility,
    Task,
    TaskKind,
    TaskStatus,
    WorkflowTemplate,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Starter set (CHARTER §16 RESOLVED)
# ---------------------------------------------------------------------------
#
# Each entry mirrors the WorkflowTemplate columns + a list of step dicts.
# Steps are minimal on purpose — agents scan/enum only per the charter, so
# the seed exploit set is empty and Web App / Network Recon are thin until
# the tool surface grows.

STARTER_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "OSINT Enum",
        "description": "Passive subdomain + DNS + WHOIS sweep against a root domain.",
        "target_kind": "domain",
        "steps": [
            {
                "tool": "subfinder",
                "kind": "enum",
                "owner_eligibility": "agent",
                "title": "Enumerate subdomains via subfinder",
                "rationale": "Wide passive recon — surfaces public subdomains.",
            },
            {
                "tool": "crt_sh",
                "kind": "enum",
                "owner_eligibility": "agent",
                "title": "Harvest hostnames from crt.sh",
                "rationale": "Certificate transparency complements subfinder's sources.",
            },
            {
                "tool": "dns_lookup",
                "kind": "enum",
                "owner_eligibility": "agent",
                "title": "Resolve A/AAAA/MX/TXT records",
                "rationale": "Baseline DNS picture for the root domain.",
            },
            {
                "tool": "whois_lookup",
                "kind": "enum",
                "owner_eligibility": "agent",
                "title": "WHOIS registration data",
                "rationale": "Ownership + registrar + creation date for context.",
            },
        ],
    },
    {
        "name": "Web App",
        "description": "Light fingerprint of an HTTP target.",
        "target_kind": "url",
        "steps": [
            {
                "tool": "httpx_probe",
                "kind": "enum",
                "owner_eligibility": "agent",
                "title": "Fingerprint server + tech via httpx",
                "rationale": "Title, status, server, tech stack — cheap first probe.",
            },
        ],
    },
    {
        "name": "Network Recon",
        "description": "Host discovery + light port sweep across a CIDR.",
        "target_kind": "cidr",
        "steps": [
            {
                "tool": "subnet_sweep",
                "kind": "scan",
                "owner_eligibility": "either",
                "title": "Sweep CIDR for live hosts",
                "rationale": "Host discovery + top-port probe to scope follow-on scans.",
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


def seed_system_templates(session: Session) -> int:
    """Insert any missing ``is_system=True`` templates from
    ``STARTER_TEMPLATES``. Returns the count of rows inserted (0 on
    repeat runs).

    Idempotent on ``name`` — if a template by that name already exists,
    we leave it alone even if the code constant changed. Rename in
    code (add v2) to ship new starter shapes; the old row stays so
    in-flight engagements keep their stable contract.
    """
    inserted = 0
    for entry in STARTER_TEMPLATES:
        existing = session.execute(
            select(WorkflowTemplate).where(WorkflowTemplate.name == entry["name"])
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            WorkflowTemplate(
                name=entry["name"],
                description=entry["description"],
                is_system=True,
                target_kind=entry["target_kind"],
                steps=entry["steps"],
            )
        )
        inserted += 1
    if inserted:
        session.flush()
        logger.info(
            "workflow_templates.seeded", inserted=inserted, total=len(STARTER_TEMPLATES)
        )
    return inserted


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def list_templates(session: Session) -> list[WorkflowTemplate]:
    """All templates, system first, then user-created in creation order.

    The system-first sort matches how the UI renders them — starter
    packs at the top, analyst-authored packs below.
    """
    return list(
        session.execute(
            select(WorkflowTemplate).order_by(
                WorkflowTemplate.is_system.desc(),
                WorkflowTemplate.created_at,
            )
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


class WorkflowTemplateApplyError(Exception):
    """Apply rejected for a structural reason — empty steps, malformed
    step, unknown template. The API layer maps this to 4xx."""


def apply_template(
    session: Session,
    *,
    template: WorkflowTemplate,
    engagement: Engagement,
    target: str,
) -> list[Task]:
    """Create one ``Task`` per step in ``template`` for ``engagement``,
    parametrized by ``target``. Caller commits.

    Tasks land ``status=pending``: the analyst still has to dispatch
    each one via the existing Tactical path. We don't auto-dispatch
    here on purpose — matches the Strategic-suggestion flow's posture
    (suggest, don't auto-run) and keeps the analyst in the loop.
    """
    if not target.strip():
        raise WorkflowTemplateApplyError("target must not be blank")
    if not template.steps:
        raise WorkflowTemplateApplyError(
            f"template {template.name!r} has no steps"
        )

    tasks: list[Task] = []
    for step in template.steps:
        try:
            kind = TaskKind(step["kind"])
            owner = OwnerEligibility(step["owner_eligibility"])
            tool = str(step["tool"])
            title = str(step["title"])
        except (KeyError, ValueError) as exc:
            raise WorkflowTemplateApplyError(
                f"step {step!r} is malformed: {exc}"
            ) from exc
        # Charter defense-in-depth: agents never exploit. The seed set
        # doesn't include exploit-kind tools, but a future user template
        # could try; refuse here as a fallback to the Tactical check.
        if kind is TaskKind.exploit:
            raise WorkflowTemplateApplyError(
                "template contains an exploit-kind step (charter forbids)"
            )
        tasks.append(
            Task(
                engagement_id=engagement.id,
                title=title,
                kind=kind,
                owner_eligibility=owner,
                status=TaskStatus.pending,
                payload={"tool": tool, "target": target.strip()},
            )
        )
    session.add_all(tasks)
    session.flush()
    logger.info(
        "workflow_templates.applied",
        template_id=str(template.id),
        template_name=template.name,
        engagement_id=str(engagement.id),
        target=target,
        tasks_created=len(tasks),
    )
    return tasks


def get_template_or_none(
    session: Session, template_id: uuid.UUID
) -> WorkflowTemplate | None:
    return session.get(WorkflowTemplate, template_id)
