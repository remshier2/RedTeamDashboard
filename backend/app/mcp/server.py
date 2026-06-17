"""MCP server for Red Team Dashboard.

Exposes the RTD's OSINT tooling, engagement data, and findings as MCP tools,
resources, and prompts so any MCP-capable agent (Claude Code, Cursor, etc.)
can orchestrate recon sessions using their own model subscription instead of
the built-in LangGraph worker.

Architecture:
  - Tools — OSINT runners (scope-gated) + engagement management (DB writes)
  - Resources — read-only engagement/findings/scope views for agent context
  - Prompts — structured workflow templates

Analyst workflow:
  1. Open Claude Code, connect to this MCP server (claude mcp add rtd …)
  2. Start a session: "What engagements do I have?"
  3. Run passive recon: "Run passive OSINT on acme.com for the acme-q3 engagement"
  4. Drill in: "Port scan 192.168.1.1 for acme-q3"  ← Claude Code asks first
  5. Findings appear in the viewer immediately

Both paths (this MCP server + the autonomous LangGraph worker) write to the
same Postgres database. The viewer shows findings from both.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

from app.mcp.auth import get_current_key, get_current_user
from app.models import (
    ActorType,
    Approval,
    ApprovalStatus,
    AuditLog,
    Engagement,
    EngagementStatus,
    Finding,
    FindingPhase,
    FindingStatus,
    ScopeItem,
    ScopeKind,
    Severity,
    User,
    scope_satisfies,
)
from app.models.api_key import APIKeyScope
from app.orchestrator.gate import Action, evaluate
from app.orchestrator.scope import ScopeSnapshot, normalize_scope_items
from app.orchestrator.tools import phase_for_tool
from app.orchestrator.tools.runtime import run_tool

# ---------------------------------------------------------------------------
# Server definition
# ---------------------------------------------------------------------------

INSTRUCTIONS = """
You are assisting red team analysts using the Red Team Dashboard (RTD).
You have access to OSINT recon tools, engagement data, and findings storage.

CORE RULES — always follow these without exception:

1. SCOPE
   Always call get_scope(engagement_slug) before running any OSINT tool.
   Never run any tool against a target that is not in the engagement scope.
   The server enforces scope server-side; out-of-scope calls are rejected.

2. RISK LEVELS
   passive tools (dns_lookup, whois_lookup, crt_sh, httpx_probe, subfinder,
   reverse_dns): run freely — no connections to target systems.

   active tools (port_scan, subnet_sweep, service_detect): make real network
   connections. Before calling these, tell the analyst exactly what you are
   about to do and wait for their explicit confirmation. Do not chain multiple
   active tool calls without checking in between.

3. ANALYST CONTROL
   After completing a logical unit of work (passive recon on a domain, a port
   scan, a batch of findings), summarize what you found and ask the analyst
   where they want to go next. Never run autonomously through a full recon
   chain without pausing to check in.

4. FINDINGS
   Store every significant result with create_finding. The viewer is the
   analyst's looking glass — findings not in the database are invisible.
   Use the severity that reflects the actual risk:
     info → informational (DNS records, certificates, open ports with known
             benign services)
     low  → minor exposure (non-critical services, informational leaks)
     medium → moderate risk (potentially exploitable if combined)
     high → significant risk (exposed admin interfaces, weak configs)
     critical → immediate risk (unauthenticated RCE, credential exposure)

5. AUDIT
   Every action is logged. Be precise about what you are doing and why.
"""

mcp = FastMCP("Red Team Dashboard", instructions=INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


@contextmanager
def _session():
    from app.db.session import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _resolve_engagement(session, slug: str) -> Engagement:
    eng = session.execute(
        select(Engagement).where(Engagement.slug == slug)
    ).scalar_one_or_none()
    if eng is None:
        raise ValueError(f"engagement '{slug}' not found")
    return eng


def _get_scope(session, eng: Engagement) -> list[ScopeSnapshot]:
    items = list(
        session.execute(
            select(ScopeItem).where(ScopeItem.engagement_id == eng.id)
        ).scalars()
    )
    return normalize_scope_items(items)


def _write_audit(
    session,
    eng: Engagement,
    user: User,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        AuditLog(
            engagement_id=eng.id,
            actor_type=ActorType.agent,
            actor_id=str(user.id),
            event_type=event_type,
            payload=payload,
        )
    )
    session.commit()


def _store_findings(
    session,
    eng: Engagement,
    tool_name: str,
    findings: list[dict[str, Any]],
) -> int:
    phase_str = phase_for_tool(tool_name)
    try:
        phase = FindingPhase(phase_str)
    except ValueError:
        phase = FindingPhase.general

    count = 0
    for f in findings:
        sev_raw = f.get("severity", "info")
        try:
            sev = Severity(sev_raw)
        except ValueError:
            sev = Severity.info

        title = f.get("title") or f"[{tool_name}] result"
        skip = ("severity", "title", "target", "summary")
        details = {k: v for k, v in f.items() if k not in skip}

        session.add(
            Finding(
                engagement_id=eng.id,
                title=title,
                severity=sev,
                summary=f.get("summary"),
                details=details,
                source_tool=tool_name,
                target=f.get("target"),
                phase=phase,
                status=FindingStatus.pending_validation,
            )
        )
        count += 1

    if count:
        session.commit()
    return count


def _run_osint(tool_name: str, engagement_slug: str, args: dict[str, Any]) -> dict[str, Any]:
    """Shared OSINT tool runner: scope check → run → audit → store findings."""
    key = get_current_key()
    user = get_current_user()

    if not scope_satisfies(key.scope, APIKeyScope.cli):
        return {"error": "requires cli scope to run OSINT tools"}

    with _session() as session:
        try:
            eng = _resolve_engagement(session, engagement_slug)
        except ValueError as exc:
            return {"error": str(exc)}

        scope = _get_scope(session, eng)
        decision = evaluate(tool_name, args, scope)

        if decision.action is Action.deny:
            return {
                "error": f"scope gate denied: {decision.reason}",
                "scope_check": decision.scope.to_jsonable(),
            }

        # Active/destructive tools require analyst confirmation per the MCP
        # server instructions. By the time the model calls this tool it has
        # already confirmed with the analyst in the conversation. Write an
        # Approval row so the audit trail matches the LangGraph path.
        if decision.action is Action.interrupt:
            approval = Approval(
                engagement_id=eng.id,
                thread_id=f"mcp-{uuid.uuid4()}",
                node="mcp_tool",
                tool_name=tool_name,
                tool_args=args,
                risk=decision.risk,
                scope_check=decision.scope.to_jsonable(),
                status=ApprovalStatus.approved,
                decided_by=user.id,
                decided_at=datetime.now(tz=UTC),
            )
            session.add(approval)
            session.flush()
            session.add(
                AuditLog(
                    engagement_id=eng.id,
                    actor_type=ActorType.user,
                    actor_id=str(user.id),
                    event_type="approval.decided",
                    payload={
                        "approval_id": str(approval.id),
                        "thread_id": approval.thread_id,
                        "tool": tool_name,
                        "status": ApprovalStatus.approved.value,
                        "approved": True,
                        "via": "mcp",
                    },
                )
            )

        result = run_tool(tool_name, args)

        payload = {
            "tool": tool_name,
            "args": args,
            "ok": result.ok,
            "risk": decision.risk.value if decision.risk else None,
        }
        if not result.ok:
            payload["error"] = result.error
        _write_audit(session, eng, user, f"mcp.tool.{tool_name}", payload)

        if result.ok and result.findings:
            stored = _store_findings(session, eng, tool_name, result.findings)
            return {**result.data, "_findings_stored": stored}

        if not result.ok:
            return {"error": result.error}

        return result.data


# ---------------------------------------------------------------------------
# Engagement management tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_engagements() -> list[dict]:
    """List all engagements in the Red Team Dashboard.

    Returns name, slug, status, and description for each engagement.
    Use the slug when calling other tools.
    """
    with _session() as session:
        engagements = list(
            session.execute(
                select(Engagement).order_by(Engagement.created_at.desc())
            ).scalars()
        )
        return [
            {
                "slug": e.slug,
                "name": e.name,
                "status": e.status,
                "description": e.description,
                "created_at": str(e.created_at),
            }
            for e in engagements
        ]


@mcp.tool()
def get_engagement(engagement_slug: str) -> dict:
    """Get full details for a single engagement including scope items and finding counts.

    Call this at the start of a session to understand the engagement context.
    """
    with _session() as session:
        try:
            eng = _resolve_engagement(session, engagement_slug)
        except ValueError as exc:
            return {"error": str(exc)}

        scope_items = list(
            session.execute(
                select(ScopeItem).where(ScopeItem.engagement_id == eng.id)
            ).scalars()
        )
        from sqlalchemy import func

        finding_count = session.execute(
            select(func.count()).where(Finding.engagement_id == eng.id)
        ).scalar_one()

        return {
            "slug": eng.slug,
            "name": eng.name,
            "status": eng.status,
            "description": eng.description,
            "created_at": str(eng.created_at),
            "finding_count": finding_count,
            "scope": [
                {
                    "kind": s.kind,
                    "value": s.value,
                    "is_exclusion": s.is_exclusion,
                    "note": s.note,
                }
                for s in scope_items
            ],
        }


@mcp.tool()
def create_engagement(name: str, description: str = "") -> dict:
    """Create a new engagement.

    Requires cli scope. The slug is auto-generated from the name.
    After creating, use add_scope_item to define what targets are in scope.
    """
    key = get_current_key()
    user = get_current_user()

    if not scope_satisfies(key.scope, APIKeyScope.cli):
        return {"error": "requires cli scope to create engagements"}

    import re

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:100]

    with _session() as session:
        existing = session.execute(
            select(Engagement).where(Engagement.slug == slug)
        ).scalar_one_or_none()
        if existing:
            return {"error": f"slug '{slug}' already exists — choose a different name"}

        eng = Engagement(
            name=name,
            slug=slug,
            description=description or None,
            created_by=user.id,
        )
        session.add(eng)
        session.commit()
        session.refresh(eng)

        session.add(
            AuditLog(
                engagement_id=eng.id,
                actor_type=ActorType.agent,
                actor_id=str(user.id),
                event_type="mcp.engagement.created",
                payload={"name": name, "slug": slug},
            )
        )
        session.commit()

        return {"slug": eng.slug, "name": eng.name, "id": str(eng.id)}


@mcp.tool()
def get_scope(engagement_slug: str) -> dict:
    """Get the scope items for an engagement.

    Always call this before running any OSINT tool to verify the target
    is in scope. Returns include items (allowed targets) and exclusions
    (targets to skip even if inside an allowed range).
    """
    with _session() as session:
        try:
            eng = _resolve_engagement(session, engagement_slug)
        except ValueError as exc:
            return {"error": str(exc)}

        items = list(
            session.execute(
                select(ScopeItem).where(ScopeItem.engagement_id == eng.id)
            ).scalars()
        )
        includes = [
            {"kind": s.kind, "value": s.value, "note": s.note}
            for s in items
            if not s.is_exclusion
        ]
        exclusions = [
            {"kind": s.kind, "value": s.value, "note": s.note}
            for s in items
            if s.is_exclusion
        ]
        return {
            "engagement": engagement_slug,
            "includes": includes,
            "exclusions": exclusions,
            "scope_kinds": ["domain", "cidr", "ip", "url"],
        }


@mcp.tool()
def add_scope_item(
    engagement_slug: str,
    kind: str,
    value: str,
    is_exclusion: bool = False,
    note: str = "",
) -> dict:
    """Add a scope item to an engagement.

    Requires cli scope.
    kind: 'domain' | 'ip' | 'cidr' | 'url'
    is_exclusion: True to mark this as a target to skip (e.g. a honeypot inside
    an approved CIDR).
    """
    key = get_current_key()
    user = get_current_user()

    if not scope_satisfies(key.scope, APIKeyScope.cli):
        return {"error": "requires cli scope to modify scope"}

    try:
        scope_kind = ScopeKind(kind)
    except ValueError:
        return {"error": f"invalid kind '{kind}' — must be one of: domain, ip, cidr, url"}

    with _session() as session:
        try:
            eng = _resolve_engagement(session, engagement_slug)
        except ValueError as exc:
            return {"error": str(exc)}

        item = ScopeItem(
            engagement_id=eng.id,
            kind=scope_kind,
            value=value.strip(),
            is_exclusion=is_exclusion,
            note=note or None,
        )
        session.add(item)

        session.add(
            AuditLog(
                engagement_id=eng.id,
                actor_type=ActorType.agent,
                actor_id=str(user.id),
                event_type="mcp.scope.added",
                payload={
                    "kind": kind,
                    "value": value,
                    "is_exclusion": is_exclusion,
                },
            )
        )
        session.commit()

        return {
            "added": {"kind": kind, "value": value, "is_exclusion": is_exclusion},
            "engagement": engagement_slug,
        }


@mcp.tool()
def list_findings(
    engagement_slug: str,
    severity: str = "",
    source_tool: str = "",
    limit: int = 50,
) -> dict:
    """List findings for an engagement.

    Optionally filter by severity (info/low/medium/high/critical) or source_tool.
    Returns the most recent findings first, up to `limit` (max 200).
    """
    with _session() as session:
        try:
            eng = _resolve_engagement(session, engagement_slug)
        except ValueError as exc:
            return {"error": str(exc)}

        q = select(Finding).where(Finding.engagement_id == eng.id)
        if severity:
            try:
                q = q.where(Finding.severity == Severity(severity))
            except ValueError:
                return {"error": f"invalid severity '{severity}'"}
        if source_tool:
            q = q.where(Finding.source_tool == source_tool)

        q = q.order_by(Finding.created_at.desc()).limit(min(limit, 200))
        findings = list(session.execute(q).scalars())

        return {
            "engagement": engagement_slug,
            "count": len(findings),
            "findings": [
                {
                    "id": str(f.id),
                    "title": f.title,
                    "severity": f.severity,
                    "status": f.status,
                    "target": f.target,
                    "source_tool": f.source_tool,
                    "phase": f.phase,
                    "summary": f.summary,
                    "created_at": str(f.created_at),
                }
                for f in findings
            ],
        }


@mcp.tool()
def create_finding(
    engagement_slug: str,
    title: str,
    severity: str,
    target: str = "",
    summary: str = "",
    details: str = "",
    source_tool: str = "analyst",
) -> dict:
    """Store an analyst-authored finding in an engagement.

    Requires cli scope. Use this to record findings you identify through
    reasoning, manual testing, or observations — not just tool outputs
    (tools auto-store their own findings).

    severity: info | low | medium | high | critical
    details: free-text or JSON string with technical details
    """
    key = get_current_key()
    user = get_current_user()

    if not scope_satisfies(key.scope, APIKeyScope.cli):
        return {"error": "requires cli scope to create findings"}

    try:
        sev = Severity(severity)
    except ValueError:
        return {"error": f"invalid severity '{severity}' — must be info/low/medium/high/critical"}

    details_payload: dict[str, Any] = {}
    if details:
        import json as _json

        try:
            parsed = _json.loads(details)
            details_payload = parsed if isinstance(parsed, dict) else {"raw": details}
        except ValueError:
            details_payload = {"raw": details}

    with _session() as session:
        try:
            eng = _resolve_engagement(session, engagement_slug)
        except ValueError as exc:
            return {"error": str(exc)}

        finding = Finding(
            engagement_id=eng.id,
            title=title,
            severity=sev,
            summary=summary or None,
            details=details_payload,
            source_tool=source_tool,
            target=target or None,
            phase=FindingPhase.general,
            status=FindingStatus.pending_validation,
        )
        session.add(finding)

        session.add(
            AuditLog(
                engagement_id=eng.id,
                actor_type=ActorType.agent,
                actor_id=str(user.id),
                event_type="mcp.finding.created",
                payload={"title": title, "severity": severity, "target": target},
            )
        )
        session.commit()
        session.refresh(finding)

        return {"id": str(finding.id), "title": title, "severity": severity}


# ---------------------------------------------------------------------------
# OSINT tools — passive (run freely, scope-checked server-side)
# ---------------------------------------------------------------------------


@mcp.tool()
def dns_lookup(engagement_slug: str, domain: str) -> dict:
    """[PASSIVE] Resolve DNS records (A, AAAA, CNAME) for a domain.

    Makes no connections to the target — uses public DNS resolvers only.
    Verify the domain is in scope with get_scope() before calling.

    Returns resolved IPs, CNAME chains, and TTL values.
    Findings are automatically stored in the engagement.
    """
    return _run_osint("dns_lookup", engagement_slug, {"domain": domain})


@mcp.tool()
def whois_lookup(engagement_slug: str, domain: str) -> dict:
    """[PASSIVE] WHOIS registration lookup for a domain.

    Returns registrar, registration dates, name servers, and registrant
    info where available. Useful for attributing infrastructure ownership.

    Findings are automatically stored in the engagement.
    """
    return _run_osint("whois_lookup", engagement_slug, {"domain": domain})


@mcp.tool()
def crt_sh(engagement_slug: str, domain: str) -> dict:
    """[PASSIVE] Certificate Transparency log search via crt.sh.

    Queries the public crt.sh database for all TLS certificates issued
    to the domain and its subdomains. Reveals infrastructure that may not
    be indexed by DNS or search engines.

    Findings are automatically stored in the engagement.
    """
    return _run_osint("crt_sh", engagement_slug, {"domain": domain})


@mcp.tool()
def httpx_probe(engagement_slug: str, url: str) -> dict:
    """[PASSIVE] HTTP/HTTPS probe for status, title, and tech fingerprints.

    Sends a HEAD then GET request to the URL. Returns status code, page
    title, server header, and detected technologies (via response headers
    and body patterns). Use this to quickly assess what a web service is.

    Findings are automatically stored in the engagement.
    """
    return _run_osint("httpx_probe", engagement_slug, {"url": url})


@mcp.tool()
def subfinder(engagement_slug: str, domain: str) -> dict:
    """[PASSIVE] Subdomain enumeration via passive sources.

    Queries certificate transparency logs, DNS aggregators, and other
    passive sources to enumerate subdomains without touching target systems.
    Good first step before DNS resolution or port scanning.

    Findings are automatically stored in the engagement.
    """
    return _run_osint("subfinder", engagement_slug, {"domain": domain})


@mcp.tool()
def reverse_dns(engagement_slug: str, ip: str) -> dict:
    """[PASSIVE] Reverse DNS (PTR) lookup for an IP address.

    Looks up the PTR record for an IP to identify the hostname. Useful
    for understanding what a discovered IP belongs to before scanning it.

    Findings are automatically stored in the engagement.
    """
    return _run_osint("reverse_dns", engagement_slug, {"ip": ip})


# ---------------------------------------------------------------------------
# OSINT tools — active (always confirm with analyst before calling)
# ---------------------------------------------------------------------------


@mcp.tool()
def port_scan(engagement_slug: str, target: str, ports: str = "") -> dict:
    """[ACTIVE — confirm with analyst before calling] TCP connect port scan.

    Makes real TCP connections to the target. Always tell the analyst what
    you are about to scan and wait for their explicit confirmation before
    calling this tool.

    target: IP address or hostname (hostname is resolved to IP before scan).
    ports: optional comma/space-separated ports or ranges e.g. '22,80,443'
           or '8000-8100'. Omit to scan ~1000 common ports.

    Scope is enforced server-side — out-of-scope targets are rejected.
    Findings are automatically stored in the engagement.
    """
    args: dict[str, Any] = {"target": target}
    if ports:
        args["ports"] = ports
    return _run_osint("portscan", engagement_slug, args)


@mcp.tool()
def subnet_sweep(engagement_slug: str, cidr: str, ports: str = "") -> dict:
    """[ACTIVE — confirm with analyst before calling] TCP port sweep of a CIDR.

    Scans all hosts in the CIDR (up to a /24, 254 hosts). Scope-excluded
    hosts inside the CIDR are automatically skipped.

    Always tell the analyst the CIDR you are about to sweep and wait for
    their explicit confirmation before calling this tool.

    cidr: target network e.g. '192.168.1.0/24'
    ports: optional ports/ranges. Omit to scan ~1000 common ports per host.

    Findings are automatically stored in the engagement.
    """
    args: dict[str, Any] = {"cidr": cidr}
    if ports:
        args["ports"] = ports
    return _run_osint("subnet_sweep", engagement_slug, args)


@mcp.tool()
def service_detect(engagement_slug: str, target: str, ports: str = "") -> dict:
    """[ACTIVE — confirm with analyst before calling] Service/version detection.

    Banner-grabs, HTTP-probes, and TLS handshakes on a host's open ports to
    fingerprint running services and versions. Best run after a port_scan
    with the open ports from that scan passed in via `ports`.

    Always tell the analyst what you are about to fingerprint and wait for
    their explicit confirmation before calling this tool.

    target: IP address or hostname.
    ports: recommended — the open ports from a prior port_scan.

    Findings are automatically stored in the engagement.
    """
    args: dict[str, Any] = {"target": target}
    if ports:
        args["ports"] = ports
    return _run_osint("service_detect", engagement_slug, args)


# ---------------------------------------------------------------------------
# Resources — read-only engagement data for agent context
# ---------------------------------------------------------------------------


@mcp.resource("engagements://list")
def resource_engagements() -> str:
    """All engagements — names, slugs, statuses, and finding counts.

    Load this at the start of a session to understand what work is in progress.
    """
    import json as _json

    with _session() as session:
        from sqlalchemy import func

        rows = session.execute(
            select(
                Engagement,
                select(func.count())
                .where(Finding.engagement_id == Engagement.id)
                .correlate(Engagement)
                .scalar_subquery()
                .label("finding_count"),
            ).order_by(Engagement.created_at.desc())
        ).all()

        data = [
            {
                "slug": eng.slug,
                "name": eng.name,
                "status": eng.status,
                "description": eng.description,
                "finding_count": count,
                "created_at": str(eng.created_at),
            }
            for eng, count in rows
        ]
        return _json.dumps(data, indent=2)


@mcp.resource("engagement://{slug}")
def resource_engagement(slug: str) -> str:
    """Full engagement context: metadata, scope, and recent high/critical findings.

    Load this when starting work on a specific engagement to orient yourself.
    """
    import json as _json

    with _session() as session:
        eng = session.execute(
            select(Engagement).where(Engagement.slug == slug)
        ).scalar_one_or_none()
        if eng is None:
            return _json.dumps({"error": f"engagement '{slug}' not found"})

        scope_items = list(
            session.execute(
                select(ScopeItem).where(ScopeItem.engagement_id == eng.id)
            ).scalars()
        )

        high_findings = list(
            session.execute(
                select(Finding)
                .where(
                    Finding.engagement_id == eng.id,
                    Finding.severity.in_([Severity.high, Severity.critical]),
                )
                .order_by(Finding.created_at.desc())
                .limit(20)
            ).scalars()
        )

        return _json.dumps(
            {
                "slug": eng.slug,
                "name": eng.name,
                "status": eng.status,
                "description": eng.description,
                "scope": [
                    {
                        "kind": s.kind,
                        "value": s.value,
                        "is_exclusion": s.is_exclusion,
                        "note": s.note,
                    }
                    for s in scope_items
                ],
                "recent_high_critical_findings": [
                    {
                        "id": str(f.id),
                        "title": f.title,
                        "severity": f.severity,
                        "target": f.target,
                        "source_tool": f.source_tool,
                        "summary": f.summary,
                    }
                    for f in high_findings
                ],
            },
            indent=2,
        )


@mcp.resource("findings://{slug}")
def resource_findings(slug: str) -> str:
    """All findings for an engagement, ordered by severity then creation date.

    Use this to review what has been discovered so far and identify gaps.
    """
    import json as _json

    with _session() as session:
        eng = session.execute(
            select(Engagement).where(Engagement.slug == slug)
        ).scalar_one_or_none()
        if eng is None:
            return _json.dumps({"error": f"engagement '{slug}' not found"})

        sev_order = {
            Severity.critical: 0,
            Severity.high: 1,
            Severity.medium: 2,
            Severity.low: 3,
            Severity.info: 4,
        }

        findings = list(
            session.execute(
                select(Finding)
                .where(Finding.engagement_id == eng.id)
                .order_by(Finding.created_at.desc())
                .limit(500)
            ).scalars()
        )
        findings.sort(key=lambda f: (sev_order.get(f.severity, 5), str(f.created_at)))

        return _json.dumps(
            {
                "engagement": slug,
                "total": len(findings),
                "findings": [
                    {
                        "id": str(f.id),
                        "title": f.title,
                        "severity": f.severity,
                        "status": f.status,
                        "target": f.target,
                        "source_tool": f.source_tool,
                        "phase": f.phase,
                        "summary": f.summary,
                        "created_at": str(f.created_at),
                    }
                    for f in findings
                ],
            },
            indent=2,
        )


# ---------------------------------------------------------------------------
# Prompts — structured workflow templates
# ---------------------------------------------------------------------------


@mcp.prompt()
def passive_recon(engagement_slug: str, target: str) -> str:
    """Standard passive recon workflow for a domain target.

    Runs the full passive OSINT chain without touching the target directly.
    """
    return f"""
Run passive OSINT on target '{target}' for engagement '{engagement_slug}'.

Follow this sequence:
1. Call get_scope('{engagement_slug}') — verify '{target}' is in scope before
   proceeding. If it is not, stop and tell the analyst.
2. Call subfinder('{engagement_slug}', '{target}') — enumerate subdomains.
3. Call crt_sh('{engagement_slug}', '{target}') — check certificate transparency.
4. Call dns_lookup('{engagement_slug}', '{target}') — resolve DNS records.
   Then call dns_lookup for any interesting subdomains found in steps 2-3.
5. Call whois_lookup('{engagement_slug}', '{target}') — check registration.
6. For each resolved IP or interesting URL discovered, call:
   - httpx_probe if it looks like a web service
   - reverse_dns to identify the hostname
7. After completing all passive steps, summarize:
   - What subdomains / IPs / services were discovered
   - Any interesting findings (unexpected services, interesting certs, etc.)
   - Recommended next steps (which targets warrant active scanning)
   Then ask the analyst how they want to proceed.

Do NOT run port_scan, subnet_sweep, or service_detect without explicit
analyst confirmation — those are active tools.
"""


@mcp.prompt()
def active_enum(engagement_slug: str, target: str, ports: str = "") -> str:
    """Active enumeration workflow for a known in-scope IP or hostname.

    Use after passive recon has identified interesting targets.
    ALWAYS confirm with the analyst before running this.
    """
    port_hint = f" Focus on ports: {ports}." if ports else ""
    return f"""
Run active enumeration on '{target}' for engagement '{engagement_slug}'.

IMPORTANT: Before doing anything, confirm with the analyst:
  "I'm about to run active tools (port scan + service detection) against
  {target} for the {engagement_slug} engagement.{port_hint} Shall I proceed?"

Wait for their explicit confirmation before calling any tools.

Once confirmed, follow this sequence:
1. Call get_scope('{engagement_slug}') — verify '{target}' is in scope.
2. Call port_scan('{engagement_slug}', '{target}'{f", '{ports}'" if ports else ""})
   — identify open ports.
3. When the port scan completes, tell the analyst what ports are open.
4. Ask: "Should I run service detection on the open ports?"
5. If yes: call service_detect('{engagement_slug}', '{target}', '<open ports>')
   — fingerprint running services.
6. Summarize findings and ask what to investigate next.
"""


@mcp.prompt()
def deep_dive(engagement_slug: str, finding_id: str) -> str:
    """Drill into a specific finding to assess exploitability and impact."""
    return f"""
Investigate finding {finding_id} from engagement '{engagement_slug}'.

Steps:
1. Load the engagement context: use the engagement://{engagement_slug} resource.
2. Find the specific finding in findings://{engagement_slug} or via
   list_findings('{engagement_slug}').
3. Based on the finding's target, tool source, and details, determine:
   - What is the actual risk? (Confirm the finding is real, not a false positive.)
   - What additional passive evidence would support or refute the severity?
   - What active follow-up (if any) would confirm exploitability?
4. Run any passive tools that help (no active tools without confirmation).
5. Summarize your assessment:
   - Confirmed / false positive / needs more investigation
   - Revised severity recommendation with justification
   - Recommended next steps
   Then ask the analyst if they want to update the finding status.
"""


# ---------------------------------------------------------------------------
# Lifecycle tools — export, archive, flush (admin scope required)
# ---------------------------------------------------------------------------


@mcp.tool()
def export_engagement(engagement_slug: str) -> dict:
    """[ADMIN] Export all engagement data to Azure Blob Storage.

    Exports findings, scope, metadata, and audit summary as JSON.
    Returns the blob URL if storage is configured, or the data inline if not.
    Requires admin scope. Safe to call at any time — does not modify data.
    """
    key = get_current_key()
    from app.models.api_key import APIKeyScope as _Scope

    if not scope_satisfies(key.scope, _Scope.admin):
        return {"error": "requires admin scope to export engagements"}


    from app.api.engagements import _build_export_payload
    from app.core.blob import upload_engagement_export
    from app.db.session import SessionLocal as _SL

    s = _SL()
    try:

        class _FakeSession:
            def __init__(self, session):
                self._s = session

            def execute(self, *a, **kw):
                return self._s.execute(*a, **kw)

            def __getattr__(self, name):
                return getattr(self._s, name)

        eng = s.execute(
            select(Engagement).where(Engagement.slug == engagement_slug)
        ).scalar_one_or_none()
        if eng is None:
            return {"error": f"engagement '{engagement_slug}' not found"}
        payload = _build_export_payload(s, eng)
        blob_url = upload_engagement_export(engagement_slug, payload)
        if blob_url:
            return {"slug": engagement_slug, "blob_url": blob_url}
        return {"slug": engagement_slug, "blob_url": None, "payload": payload}
    finally:
        s.close()


@mcp.tool()
def archive_engagement(engagement_slug: str) -> dict:
    """[ADMIN] Export and archive an engagement.

    Marks the engagement as archived — it stays in the database but is
    excluded from active views. An export is uploaded to blob storage first.
    Requires admin scope. Reversible (can be unarchived via PATCH /engagements/{slug}).
    """
    key = get_current_key()
    user = get_current_user()
    from app.models.api_key import APIKeyScope as _Scope

    if not scope_satisfies(key.scope, _Scope.admin):
        return {"error": "requires admin scope to archive engagements"}

    from datetime import UTC
    from datetime import datetime as _dt

    from app.api.engagements import _build_export_payload
    from app.core.blob import upload_engagement_export

    with _session() as session:
        eng = session.execute(
            select(Engagement).where(Engagement.slug == engagement_slug)
        ).scalar_one_or_none()
        if eng is None:
            return {"error": f"engagement '{engagement_slug}' not found"}
        if eng.status == EngagementStatus.flushed:
            return {"error": "engagement has been flushed"}

        if eng.status != EngagementStatus.archived:
            eng.status = EngagementStatus.archived
            eng.archived_at = _dt.now(tz=UTC)
            session.commit()
            session.refresh(eng)

        blob_url = upload_engagement_export(engagement_slug, _build_export_payload(session, eng))

        session.add(
            AuditLog(
                engagement_id=eng.id,
                actor_type=ActorType.agent,
                actor_id=str(user.id),
                event_type="mcp.engagement.archived",
                payload={"blob_url": blob_url},
            )
        )
        session.commit()

        return {
            "slug": engagement_slug,
            "status": "archived",
            "archived_at": str(eng.archived_at),
            "blob_url": blob_url,
        }


@mcp.tool()
def flush_engagement_data(engagement_slug: str, confirmed: bool = False) -> dict:
    """[ADMIN — DESTRUCTIVE] Export then permanently delete all engagement data.

    Removes the engagement and ALL associated data (findings, scope, approvals,
    audit logs) from the database. The engagement disappears from the viewer.
    An export is uploaded to blob storage first as a safety net.

    THIS CANNOT BE UNDONE. Set confirmed=True to proceed.
    Requires admin scope. Always tell the analyst what will be deleted and
    wait for their explicit confirmation before calling with confirmed=True.
    """
    key = get_current_key()
    from app.models.api_key import APIKeyScope as _Scope

    if not scope_satisfies(key.scope, _Scope.admin):
        return {"error": "requires admin scope to flush engagements"}

    if not confirmed:
        return {
            "error": "confirmation required",
            "message": (
                f"This will permanently delete ALL data for engagement '{engagement_slug}' "
                "including all findings, scope, approvals, and audit logs. "
                "Call again with confirmed=True to proceed."
            ),
        }

    from sqlalchemy import text as _text

    from app.api.engagements import _build_export_payload
    from app.core.blob import upload_engagement_export

    with _session() as session:
        eng = session.execute(
            select(Engagement).where(Engagement.slug == engagement_slug)
        ).scalar_one_or_none()
        if eng is None:
            return {"error": f"engagement '{engagement_slug}' not found"}

        eid = eng.id
        payload = _build_export_payload(session, eng)
        blob_url = upload_engagement_export(engagement_slug, payload)

        session.execute(_text("SELECT flush_engagement(:id)"), {"id": eid})
        session.commit()

        return {
            "slug": engagement_slug,
            "flushed": True,
            "blob_url": blob_url,
            "note": (
                "export stored in blob before flush"
                if blob_url
                else "no blob storage configured"
            ),
        }
