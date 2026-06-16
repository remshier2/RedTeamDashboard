"""Central tool registry.

Every tool the agent can call must be declared here with its risk classification
and the shape of its scope-relevant input. The gate uses this metadata to decide
whether a call is in scope and whether it can auto-approve.

Auditing risk levels = reading this one file. Adding a tool without a ToolSpec
is a deliberate choice — ``evaluate()`` denies any tool not in the registry.

Per-tool implementations (when they land) live as siblings in this package
(e.g. ``app.orchestrator.tools.subfinder``); this module only describes them.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.models import RiskLevel, ScopeKind


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    risk: RiskLevel
    target_arg: str
    kind: ScopeKind
    description: str = ""
    # When True, the dispatch node resolves a hostname target_arg to an IP
    # *before* the scope gate, so we authorize and act on the same address
    # (used by IP-kind tools that should also accept hostnames, e.g. portscan).
    resolve_host: bool = False
    # When True, the dispatch node injects the engagement's ip/cidr scope
    # exclusions as an ``exclude`` arg before the tool runs, so range tools
    # (e.g. subnet_sweep) skip carved-out hosts inside an approved CIDR.
    inject_exclusions: bool = False
    # Extra JSON-schema properties merged into the tool's input_schema beyond
    # the (required) target_arg — e.g. an optional `ports` arg. Not required.
    extra_properties: Mapping[str, Any] = field(default_factory=dict)


_PHASE_0_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="subfinder",
        risk=RiskLevel.passive,
        target_arg="domain",
        kind=ScopeKind.domain,
        description="Passive subdomain enumeration via subfinder.",
    ),
    ToolSpec(
        name="crt_sh",
        risk=RiskLevel.passive,
        target_arg="domain",
        kind=ScopeKind.domain,
        description="Certificate Transparency log query via crt.sh.",
    ),
    ToolSpec(
        name="dns_lookup",
        risk=RiskLevel.passive,
        target_arg="domain",
        kind=ScopeKind.domain,
        description="Resolve A/AAAA/CNAME records.",
    ),
    ToolSpec(
        name="whois_lookup",
        risk=RiskLevel.passive,
        target_arg="domain",
        kind=ScopeKind.domain,
        description="WHOIS registration lookup.",
    ),
    ToolSpec(
        name="httpx_probe",
        risk=RiskLevel.passive,
        target_arg="url",
        kind=ScopeKind.url,
        description="HEAD/GET probe for status, title, and tech fingerprints.",
    ),
    ToolSpec(
        name="reverse_dns",
        risk=RiskLevel.passive,
        target_arg="ip",
        kind=ScopeKind.ip,
        description="Reverse DNS (PTR) lookup for an IP.",
    ),
    ToolSpec(
        name="portscan",
        risk=RiskLevel.active,
        target_arg="target",
        kind=ScopeKind.ip,
        description=(
            "ACTIVE TCP connect port scan of a single host. Accepts an IP or a "
            "hostname (resolved to an IP before scanning). Requires operator "
            "approval before it runs. Scans ~1000 common ports by default; "
            "pass `ports` to narrow (e.g. '22,80,443' or '8000-8100')."
        ),
        resolve_host=True,
        extra_properties={
            "ports": {
                "type": "string",
                "description": (
                    "Optional. Comma/space-separated ports and 'A-B' ranges, "
                    "e.g. '22,80,443,8000-8100'. Omit to scan the default "
                    "~1000 common ports."
                ),
            },
        },
    ),
    ToolSpec(
        name="subnet_sweep",
        risk=RiskLevel.active,
        target_arg="cidr",
        kind=ScopeKind.cidr,
        description=(
            "ACTIVE TCP port sweep of an entire CIDR (up to a /24, 254 hosts). "
            "One approval authorizes the whole range; hosts excluded from scope "
            "are skipped automatically. Scans ~1000 ports per host by default. "
            "Use portscan for a single host; use this for a subnet."
        ),
        inject_exclusions=True,
        extra_properties={
            "ports": {
                "type": "string",
                "description": (
                    "Optional. Comma/space-separated ports and 'A-B' ranges "
                    "applied to every host. Omit to scan the default ~1000 "
                    "common ports per host."
                ),
            },
        },
    ),
    ToolSpec(
        name="service_detect",
        risk=RiskLevel.active,
        target_arg="target",
        kind=ScopeKind.ip,
        description=(
            "ACTIVE service/version detection for a single host. Accepts an IP "
            "or hostname (resolved to an IP). For each port it grabs the "
            "banner, sends an HTTP request, and does a TLS handshake to read "
            "the cert. Requires operator approval. Pass `ports` with the OPEN "
            "ports a prior scan found; omit to probe a small common set."
        ),
        resolve_host=True,
        extra_properties={
            "ports": {
                "type": "string",
                "description": (
                    "Optional. Comma/space-separated ports and 'A-B' ranges to "
                    "fingerprint, e.g. '22,80,443'. Best supplied with the open "
                    "ports from a prior portscan/subnet_sweep."
                ),
            },
        },
    ),
)


_TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in _PHASE_0_TOOLS}


def get_tool(name: str, registry: Mapping[str, ToolSpec] | None = None) -> ToolSpec | None:
    return (registry or _TOOLS).get(name)


# Tool → engagement phase, for tagging the findings a tool produces (Phase 8).
# Passive recon → OSINT; active enumeration → Vuln Scan. Unknown → general.
_TOOL_PHASE: dict[str, str] = {
    "subfinder": "osint",
    "crt_sh": "osint",
    "dns_lookup": "osint",
    "whois_lookup": "osint",
    "httpx_probe": "osint",
    "reverse_dns": "osint",
    "portscan": "vuln_scan",
    "subnet_sweep": "vuln_scan",
    "service_detect": "vuln_scan",
}


def phase_for_tool(name: str | None) -> str:
    """Engagement phase a tool's findings belong to. Falls back to 'general'."""
    return _TOOL_PHASE.get(name or "", "general")


def all_tools(registry: Mapping[str, ToolSpec] | None = None) -> list[ToolSpec]:
    return list((registry or _TOOLS).values())


__all__ = ["ToolSpec", "all_tools", "get_tool"]
