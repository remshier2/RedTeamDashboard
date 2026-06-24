"""Nessus .nessus v2 XML importer — Phase 10 first slice.

Covers the pure parser (no DB) and the upload endpoint integration with
the engagement + findings persistence path.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.main import app
from app.models import (
    Engagement,
    FindingPhase,
    FindingStatus,
    ScopeItem,
    ScopeKind,
    Severity,
)
from app.runs.streams import inbound_stream, outbound_stream
from app.services.nessus_import import ParsedItem, parse_nessus_xml
from tests.test_engagements_api import _create, _headers

# Local fixtures — keep this file self-contained (matches the pattern in
# tests/test_direct_run_lease.py so pytest doesn't trip on F811 with
# imported fixtures).


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def redis_client() -> Iterator[redis_lib.Redis]:
    r = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield r
    finally:
        r.close()


@pytest.fixture()
def cleanup_slugs(
    db: Session, redis_client: redis_lib.Redis
) -> Iterator[list[str]]:
    slugs: list[str] = []
    yield slugs
    for slug in slugs:
        eng_id = db.execute(
            select(Engagement.id).where(Engagement.slug == slug)
        ).scalar_one_or_none()
        if eng_id is None:
            continue
        db.execute(
            text("DELETE FROM approvals WHERE engagement_id = :id"),
            {"id": eng_id},
        )
        db.commit()
        db.execute(text("SELECT flush_engagement(:id)"), {"id": eng_id})
        db.commit()
        redis_client.delete(inbound_stream(eng_id), outbound_stream(eng_id))


# ---------------------------------------------------------------------------
# Fixtures: minimal valid Nessus XML payloads
# ---------------------------------------------------------------------------


def _make_xml(*report_hosts: str) -> bytes:
    body = "\n".join(report_hosts)
    return f"""<?xml version="1.0"?>
<NessusClientData_v2>
  <Policy><policyName>test</policyName></Policy>
  <Report name="r">
{body}
  </Report>
</NessusClientData_v2>""".encode()


def _host(
    fqdn: str, ip: str, *report_items: str, os: str = "Linux"
) -> str:
    items = "\n".join(report_items)
    return f"""    <ReportHost name="{fqdn}">
      <HostProperties>
        <tag name="host-fqdn">{fqdn}</tag>
        <tag name="host-ip">{ip}</tag>
        <tag name="operating-system">{os}</tag>
      </HostProperties>
{items}
    </ReportHost>"""


def _item(
    *,
    severity: int,
    plugin_id: str = "12345",
    plugin_name: str = "SSL Cert Self-Signed",
    port: str = "443",
    protocol: str = "tcp",
    synopsis: str = "Cert is self-signed.",
    description: str = "Long description.",
    solution: str = "Replace cert.",
    cve: str | None = None,
) -> str:
    cve_block = f"<cve>{cve}</cve>" if cve else ""
    attrs = (
        f'port="{port}" protocol="{protocol}" severity="{severity}" '
        f'pluginID="{plugin_id}" pluginName="{plugin_name}" '
        f'pluginFamily="General"'
    )
    return f"""      <ReportItem {attrs}>
        <synopsis>{synopsis}</synopsis>
        <description>{description}</description>
        <solution>{solution}</solution>
        <risk_factor>Medium</risk_factor>
        {cve_block}
      </ReportItem>"""


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


def test_parse_happy_path_maps_severity_and_target() -> None:
    xml = _make_xml(_host("box.example.test", "10.0.0.5", _item(severity=3)))
    result = parse_nessus_xml(xml)

    assert result.total_items == 1
    assert len(result.items) == 1
    item = result.items[0]
    assert item.title == "SSL Cert Self-Signed"
    assert item.severity is Severity.high
    assert item.phase is FindingPhase.vuln_scan
    assert item.target == "box.example.test:443"
    assert item.source_tool == "nessus_import"
    assert item.summary == "Cert is self-signed."
    assert item.details["plugin_id"] == "12345"
    assert item.details["protocol"] == "tcp"
    assert item.details["risk_factor"] == "Medium"
    assert (
        item.details["host_properties"]["host-fqdn"] == "box.example.test"
    )


def test_parse_skips_info_by_default() -> None:
    xml = _make_xml(
        _host(
            "box.example.test",
            "10.0.0.5",
            _item(severity=0, plugin_name="info-row"),
            _item(severity=2, plugin_name="medium-row"),
        )
    )
    result = parse_nessus_xml(xml)

    assert result.total_items == 2
    assert result.skipped_info == 1
    assert len(result.items) == 1
    assert result.items[0].title == "medium-row"


def test_parse_include_info_keeps_severity_zero() -> None:
    xml = _make_xml(
        _host(
            "box.example.test",
            "10.0.0.5",
            _item(severity=0, plugin_name="info-row"),
        )
    )
    result = parse_nessus_xml(xml, include_info=True)
    assert result.skipped_info == 0
    assert len(result.items) == 1
    assert result.items[0].severity is Severity.info


def test_parse_drops_out_of_scope_hosts(db: Session) -> None:
    # Two hosts, only one in scope.
    xml = _make_xml(
        _host(
            "in-scope.example.test",
            "10.0.0.5",
            _item(severity=2, plugin_name="kept"),
        ),
        _host(
            "out.example.test",
            "192.168.1.1",
            _item(severity=3, plugin_name="dropped"),
        ),
    )
    scope_items = [
        ScopeItem(
            engagement_id=uuid.uuid4(),
            kind=ScopeKind.domain,
            value="in-scope.example.test",
            is_exclusion=False,
        ),
    ]

    result = parse_nessus_xml(xml, scope_items=scope_items)
    assert result.skipped_out_of_scope == 1
    assert [i.title for i in result.items] == ["kept"]


def test_parse_empty_scope_does_no_filtering() -> None:
    xml = _make_xml(
        _host(
            "anywhere.example.test",
            "1.2.3.4",
            _item(severity=2, plugin_name="kept"),
        )
    )
    result = parse_nessus_xml(xml, scope_items=[])
    assert result.skipped_out_of_scope == 0
    assert len(result.items) == 1


def test_parse_rejects_malformed_xml() -> None:
    with pytest.raises(ValueError, match="invalid Nessus XML"):
        parse_nessus_xml(b"<not really xml")


def test_parse_rejects_wrong_root_element() -> None:
    with pytest.raises(ValueError, match="NessusClientData_v2"):
        parse_nessus_xml(b"<?xml version='1.0'?><WrongRoot/>")


def test_parse_port_zero_omits_colon_from_target() -> None:
    """Plugin reports against the whole host (port=0) → bare hostname."""
    xml = _make_xml(
        _host("hostonly.example.test", "10.0.0.5", _item(severity=2, port="0"))
    )
    result = parse_nessus_xml(xml)
    assert result.items[0].target == "hostonly.example.test"


def test_parse_multi_host_multi_item() -> None:
    xml = _make_xml(
        _host(
            "a.example.test",
            "10.0.0.1",
            _item(severity=1, plugin_name="low-a"),
            _item(severity=4, plugin_name="crit-a"),
        ),
        _host(
            "b.example.test",
            "10.0.0.2",
            _item(severity=2, plugin_name="med-b"),
        ),
    )
    result = parse_nessus_xml(xml)
    titles = sorted(i.title for i in result.items)
    assert titles == ["crit-a", "low-a", "med-b"]
    sev = {i.title: i.severity for i in result.items}
    assert sev["low-a"] is Severity.low
    assert sev["crit-a"] is Severity.critical
    assert sev["med-b"] is Severity.medium


# ---------------------------------------------------------------------------
# Endpoint integration
# ---------------------------------------------------------------------------


def test_endpoint_imports_uploaded_file(
    client: TestClient,
    cleanup_slugs: list[str],
    db: Session,
) -> None:
    """POST a .nessus XML file. Findings persist with status=pending_validation;
    response surfaces the skipped counts; Info rows skipped by default."""
    eng = _create(client, "Nessus import target")
    cleanup_slugs.append(eng["slug"])

    xml = _make_xml(
        _host(
            "box.example.test",
            "10.0.0.5",
            _item(severity=0, plugin_name="info-row"),
            _item(severity=3, plugin_name="high-row"),
            _item(severity=4, plugin_name="crit-row", cve="CVE-2024-1234"),
        )
    )

    response = client.post(
        f"/engagements/{eng['slug']}/findings/import/nessus",
        files={"file": ("scan.nessus", xml, "application/xml")},
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["total_items"] == 3
    assert body["skipped_info"] == 1
    assert body["skipped_out_of_scope"] == 0
    assert len(body["imported"]) == 2

    titles = sorted(f["title"] for f in body["imported"])
    assert titles == ["crit-row", "high-row"]

    for f in body["imported"]:
        assert f["status"] == FindingStatus.pending_validation.value
        assert f["phase"] == FindingPhase.vuln_scan.value


def test_endpoint_rejects_invalid_xml(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, "Nessus reject malformed")
    cleanup_slugs.append(eng["slug"])

    response = client.post(
        f"/engagements/{eng['slug']}/findings/import/nessus",
        files={"file": ("scan.nessus", b"<not really>", "application/xml")},
        headers=_headers(),
    )
    assert response.status_code == 400
    assert "invalid Nessus XML" in response.json()["detail"]


def test_endpoint_include_info_query_param(
    client: TestClient,
    cleanup_slugs: list[str],
) -> None:
    """``?include_info=true`` opts into Info findings."""
    eng = _create(client, "Nessus include info")
    cleanup_slugs.append(eng["slug"])

    xml = _make_xml(
        _host(
            "box.example.test",
            "10.0.0.5",
            _item(severity=0, plugin_name="info-row"),
        )
    )

    response = client.post(
        f"/engagements/{eng['slug']}/findings/import/nessus?include_info=true",
        files={"file": ("scan.nessus", xml, "application/xml")},
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["skipped_info"] == 0
    assert len(body["imported"]) == 1
    assert body["imported"][0]["severity"] == Severity.info.value


def test_parsed_item_is_duck_typed_against_finding_import() -> None:
    """Sanity: ParsedItem exposes the attributes the shared persistence
    helper reads (title, severity, phase, summary, target, source_tool,
    details). If this drifts, the helper will silently break."""
    item = ParsedItem(
        title="x",
        severity=Severity.low,
        phase=FindingPhase.vuln_scan,
        summary=None,
        target="t",
        source_tool="nessus_import",
        details={},
    )
    for attr in ("title", "severity", "phase", "summary", "target", "source_tool", "details"):
        assert hasattr(item, attr)
