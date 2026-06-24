"""Nessus .nessus v2 XML import — Phase 10 hybrid execution.

Parses Tenable Nessus scan exports into a list of finding-shaped rows
that flow through the same import-persistence path the Phase 11
JSON/CSV importer uses. Each ``ReportItem`` becomes one ``Finding`` with
``phase=vuln_scan``, ``source_tool="nessus_import"``, and the plugin
metadata stashed under ``details`` JSONB for slide-over rendering.

Charter posture (§16 RESOLVED): Nessus is import-first. The analyst
runs it on their own infra and uploads the result here — we don't shell
out to nessuscli or talk to a Nessus server. The hard rule "agents scan,
analysts exploit/validate" is preserved: imported findings land
``status=pending_validation`` like every other imported source.

XML safety: uses ``defusedxml.ElementTree`` because the upload is
untrusted user input. The stdlib ``xml.etree`` is vulnerable to
billion-laughs and XXE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from defusedxml import ElementTree

from app.models import FindingPhase, ScopeItem, Severity

# Nessus severity attribute (string) → our Severity enum.
# 0 = Info, 1 = Low, 2 = Medium, 3 = High, 4 = Critical.
_NESSUS_SEVERITY: dict[str, Severity] = {
    "0": Severity.info,
    "1": Severity.low,
    "2": Severity.medium,
    "3": Severity.high,
    "4": Severity.critical,
}


@dataclass
class ParsedItem:
    """One ReportItem reduced to a finding-shaped row.

    Duck-typed against ``app.schemas.finding.FindingImport`` so the
    shared persistence helper can take either shape.
    """

    title: str
    severity: Severity
    phase: FindingPhase
    summary: str | None
    target: str
    source_tool: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    """What `parse_nessus_xml` returns. The endpoint surfaces the
    skipped counts on the response so the analyst can verify the
    filter didn't drop more than they expected."""

    items: list[ParsedItem]
    skipped_info: int
    skipped_out_of_scope: int
    total_items: int


def _child_text(elem: Any, tag: str) -> str | None:
    """Find a child by tag, return its stripped text, or None."""
    child = elem.find(tag)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _host_target_and_props(report_host: Any) -> tuple[str, dict[str, str]]:
    """Pull the FQDN-or-IP target string plus the raw HostProperties dict."""
    props: dict[str, str] = {}
    hp = report_host.find("HostProperties")
    if hp is not None:
        for tag_elem in hp.findall("tag"):
            name = tag_elem.attrib.get("name")
            if name and tag_elem.text:
                props[name] = tag_elem.text.strip()
    target = (
        props.get("host-fqdn")
        or props.get("host-ip")
        or report_host.attrib.get("name", "")
    )
    return target, props


def _host_in_scope(
    host_target: str,
    host_props: dict[str, str],
    scope_items: list[ScopeItem],
) -> bool:
    """Literal-string scope match against the host's known addresses.

    CIDR/wildcard scope expansion lives in the orchestrator scope-gate
    (``app/orchestrator/gate.py``). The Phase 11 JSON importer doesn't
    apply scope filtering at all; this importer is stricter because
    Nessus exports often span hundreds of hosts and analysts should not
    have to manually prune out-of-scope rows. If exact-string match
    proves insufficient, swap to the gate's scope logic in a follow-up.
    """
    if not scope_items:
        return True
    addrs = {host_target}
    if "host-fqdn" in host_props:
        addrs.add(host_props["host-fqdn"])
    if "host-ip" in host_props:
        addrs.add(host_props["host-ip"])
    addrs.discard("")
    excludes = {item.value for item in scope_items if item.is_exclusion}
    if addrs & excludes:
        return False
    includes = {item.value for item in scope_items if not item.is_exclusion}
    return bool(addrs & includes)


def parse_nessus_xml(
    xml_bytes: bytes,
    *,
    include_info: bool = False,
    scope_items: list[ScopeItem] | None = None,
) -> ParseResult:
    """Parse a .nessus v2 XML payload.

    ``include_info``: when False (default), Nessus severity=0
    (Informational) ReportItems are skipped. Most analysts don't want
    1000s of Info rows; toggle the query param to opt in.

    ``scope_items``: when non-empty, hosts not matching any in-scope
    address (or matching an exclude) are dropped silently. The dropped
    counts come back on ``ParseResult`` so the endpoint can echo them.

    Raises ``ValueError`` on malformed XML or unexpected root element.
    """
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise ValueError(f"invalid Nessus XML: {exc}") from exc

    # v1 has been deprecated since 2010; we only accept v2. The root
    # element name is the only signal Nessus gives.
    if root.tag != "NessusClientData_v2":
        raise ValueError(
            f"expected NessusClientData_v2 root element, got {root.tag!r}"
        )

    items: list[ParsedItem] = []
    skipped_info = 0
    skipped_out_of_scope = 0
    total = 0

    for report in root.findall("Report"):
        for report_host in report.findall("ReportHost"):
            host_target, host_props = _host_target_and_props(report_host)
            in_scope = _host_in_scope(host_target, host_props, scope_items or [])

            for report_item in report_host.findall("ReportItem"):
                total += 1
                severity_attr = report_item.attrib.get("severity", "0")
                severity = _NESSUS_SEVERITY.get(severity_attr, Severity.info)

                if severity is Severity.info and not include_info:
                    skipped_info += 1
                    continue
                if not in_scope:
                    skipped_out_of_scope += 1
                    continue

                port = report_item.attrib.get("port", "0")
                target = host_target if port == "0" else f"{host_target}:{port}"
                plugin_name = report_item.attrib.get(
                    "pluginName", "(unnamed plugin)"
                )
                details: dict[str, Any] = {
                    "plugin_id": report_item.attrib.get("pluginID", ""),
                    "plugin_family": report_item.attrib.get("pluginFamily", ""),
                    "port": port,
                    "protocol": report_item.attrib.get("protocol", ""),
                    "svc_name": report_item.attrib.get("svc_name", ""),
                    "description": _child_text(report_item, "description"),
                    "solution": _child_text(report_item, "solution"),
                    "plugin_output": _child_text(report_item, "plugin_output"),
                    "cvss_base_score": _child_text(report_item, "cvss_base_score"),
                    "cve": _child_text(report_item, "cve"),
                    "risk_factor": _child_text(report_item, "risk_factor"),
                    "host_properties": {
                        k: v
                        for k, v in host_props.items()
                        if k
                        in (
                            "host-fqdn",
                            "host-ip",
                            "operating-system",
                            "mac-address",
                        )
                    },
                }

                items.append(
                    ParsedItem(
                        title=plugin_name,
                        severity=severity,
                        phase=FindingPhase.vuln_scan,
                        summary=_child_text(report_item, "synopsis"),
                        target=target,
                        source_tool="nessus_import",
                        details=details,
                    )
                )

    return ParseResult(
        items=items,
        skipped_info=skipped_info,
        skipped_out_of_scope=skipped_out_of_scope,
        total_items=total,
    )
