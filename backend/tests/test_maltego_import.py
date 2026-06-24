"""Maltego .mtgx importer — Phase 10.

Covers the pure parser (no DB) and the upload endpoint + UPSERT
persistence path.
"""
from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import Iterator

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.main import app
from app.models import Engagement, EngagementStatus, Entity
from app.runs.streams import inbound_stream, outbound_stream
from app.services.maltego_import import ParsedEntity, parse_mtgx
from tests.test_engagements_api import _create, _headers


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


@pytest.fixture()
def engagement(db: Session) -> Iterator[Engagement]:
    eng = Engagement(
        name="mtgx-test",
        slug=f"mtgx-{uuid.uuid4().hex[:8]}",
        status=EngagementStatus.active,
    )
    db.add(eng)
    db.commit()
    db.refresh(eng)
    try:
        yield eng
    finally:
        # Ensure no stored entities linger between tests beyond what
        # CASCADE handles.
        db.execute(
            text("DELETE FROM entities WHERE engagement_id = :id"),
            {"id": eng.id},
        )
        db.commit()
        db.execute(text("SELECT flush_engagement(:id)"), {"id": eng.id})
        db.commit()


# ---------------------------------------------------------------------------
# Synthetic .mtgx fixtures
# ---------------------------------------------------------------------------


def _entity_xml(
    *,
    node_id: str,
    type_attr: str,
    primary_prop_name: str,
    primary_value: str,
    extra_props: dict[str, str] | None = None,
) -> str:
    extras = ""
    if extra_props:
        for name, val in extra_props.items():
            extras += (
                f'<mtg:Property name="{name}" displayName="{name}" type="string">'
                f"<mtg:Value>{val}</mtg:Value></mtg:Property>"
            )
    primary = (
        f'<mtg:Property name="{primary_prop_name}" '
        f'displayName="{primary_prop_name}" type="string">'
        f"<mtg:Value>{primary_value}</mtg:Value></mtg:Property>"
    )
    return f"""    <node id="{node_id}">
      <data key="d0">
        <mtg:MaltegoEntity type="{type_attr}">
          <mtg:Properties>
            {primary}
            {extras}
          </mtg:Properties>
        </mtg:MaltegoEntity>
      </data>
    </node>"""


def _make_mtgx(*node_xmls: str) -> bytes:
    nodes = "\n".join(node_xmls)
    graphml = f"""<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns"
         xmlns:mtg="http://maltego.paterva.com/xml/mtgx">
  <graph edgedefault="directed">
{nodes}
  </graph>
</graphml>""".encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Graphs/Graph1.graphml", graphml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


def test_parse_normalizes_known_maltego_types() -> None:
    xml = _make_mtgx(
        _entity_xml(
            node_id="n0",
            type_attr="maltego.Domain",
            primary_prop_name="fqdn",
            primary_value="acme.example.test",
        ),
        _entity_xml(
            node_id="n1",
            type_attr="maltego.EmailAddress",
            primary_prop_name="email",
            primary_value="admin@acme.example.test",
        ),
        _entity_xml(
            node_id="n2",
            type_attr="maltego.IPv4Address",
            primary_prop_name="ipv4-address",
            primary_value="10.0.0.5",
        ),
    )
    result = parse_mtgx(xml, source_attribution="t.mtgx")

    assert result.total_nodes == 3
    by_value = {item.value: item for item in result.items}
    assert by_value["acme.example.test"].type == "domain"
    assert by_value["acme.example.test"].maltego_type == "maltego.Domain"
    assert by_value["admin@acme.example.test"].type == "email"
    assert by_value["10.0.0.5"].type == "ip"


def test_parse_passes_through_unknown_types_verbatim() -> None:
    xml = _make_mtgx(
        _entity_xml(
            node_id="n0",
            type_attr="maltego.SomeNovelType",
            primary_prop_name="something",
            primary_value="weird-value",
        )
    )
    result = parse_mtgx(xml)
    assert len(result.items) == 1
    assert result.items[0].type == "maltego.SomeNovelType"
    assert result.items[0].maltego_type == "maltego.SomeNovelType"


def test_parse_captures_extra_properties() -> None:
    xml = _make_mtgx(
        _entity_xml(
            node_id="n0",
            type_attr="maltego.Domain",
            primary_prop_name="fqdn",
            primary_value="acme.example.test",
            extra_props={"description": "primary domain", "country": "US"},
        )
    )
    result = parse_mtgx(xml, source_attribution="scan.mtgx")
    item = result.items[0]
    assert item.properties["fqdn"] == "acme.example.test"
    assert item.properties["description"] == "primary domain"
    assert item.properties["country"] == "US"
    # Source attribution stamped onto properties for UI display.
    assert item.properties["_source_attribution"] == "scan.mtgx"


def test_parse_skips_entity_without_value() -> None:
    """An entity element with no usable property value bumps skipped_empty
    rather than blowing up the whole import."""
    # Build a graphml that has a node with a MaltegoEntity that has an
    # empty primary property (whitespace-only). The hand-written XML
    # below mirrors the _entity_xml shape but with an empty value.
    graphml = b"""<?xml version="1.0"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns"
         xmlns:mtg="http://maltego.paterva.com/xml/mtgx">
  <graph>
    <node id="n0">
      <data key="d0">
        <mtg:MaltegoEntity type="maltego.Domain">
          <mtg:Properties>
            <mtg:Property name="fqdn" displayName="fqdn" type="string">
              <mtg:Value>   </mtg:Value>
            </mtg:Property>
          </mtg:Properties>
        </mtg:MaltegoEntity>
      </data>
    </node>
  </graph>
</graphml>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Graphs/Graph1.graphml", graphml)

    result = parse_mtgx(buf.getvalue())
    assert result.total_nodes == 1
    assert result.skipped_empty == 1
    assert len(result.items) == 0


def test_parse_rejects_non_zip() -> None:
    with pytest.raises(ValueError, match="not a .mtgx zip"):
        parse_mtgx(b"not a zip at all")


def test_parse_rejects_missing_graphml() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("notes.txt", "no graphml here")
    with pytest.raises(ValueError, match="no Graphs/"):
        parse_mtgx(buf.getvalue())


def test_parse_rejects_malformed_graphml() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Graphs/bad.graphml", b"<not really xml")
    with pytest.raises(ValueError, match="invalid GraphML"):
        parse_mtgx(buf.getvalue())


def test_parse_rejects_wrong_root_element() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Graphs/x.graphml",
            b"<?xml version='1.0'?><WrongRoot/>",
        )
    with pytest.raises(ValueError, match="expected graphml"):
        parse_mtgx(buf.getvalue())


# ---------------------------------------------------------------------------
# Endpoint + persistence integration
# ---------------------------------------------------------------------------


def test_import_endpoint_persists_entities(
    client: TestClient,
    db: Session,
    cleanup_slugs: list[str],
) -> None:
    eng = _create(client, "mtgx import")
    cleanup_slugs.append(eng["slug"])

    xml = _make_mtgx(
        _entity_xml(
            node_id="n0",
            type_attr="maltego.Domain",
            primary_prop_name="fqdn",
            primary_value="acme.example.test",
        ),
        _entity_xml(
            node_id="n1",
            type_attr="maltego.EmailAddress",
            primary_prop_name="email",
            primary_value="admin@acme.example.test",
        ),
    )

    res = client.post(
        f"/engagements/{eng['slug']}/entities/import/maltego",
        files={"file": ("scan.mtgx", xml, "application/zip")},
        headers=_headers(),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["inserted"] == 2
    assert body["merged"] == 0
    assert body["total_nodes"] == 2
    assert {e["value"] for e in body["entities"]} == {
        "acme.example.test",
        "admin@acme.example.test",
    }

    persisted = list(
        db.execute(
            select(Entity).where(Entity.engagement_id == uuid.UUID(eng["id"]))
        ).scalars()
    )
    assert len(persisted) == 2
    for e in persisted:
        assert e.source_tool == "maltego_import"
        assert e.source_attribution == "scan.mtgx"


def test_reimport_merges_properties(
    client: TestClient,
    db: Session,
    cleanup_slugs: list[str],
) -> None:
    """Re-import of the same (type, value) merges properties JSONB
    rather than creating a duplicate row. New keys win on collision."""
    eng = _create(client, "mtgx merge")
    cleanup_slugs.append(eng["slug"])

    first = _make_mtgx(
        _entity_xml(
            node_id="n0",
            type_attr="maltego.Domain",
            primary_prop_name="fqdn",
            primary_value="acme.example.test",
            extra_props={"description": "v1"},
        )
    )
    res1 = client.post(
        f"/engagements/{eng['slug']}/entities/import/maltego",
        files={"file": ("first.mtgx", first, "application/zip")},
        headers=_headers(),
    )
    assert res1.status_code == 201, res1.text
    assert res1.json()["inserted"] == 1

    second = _make_mtgx(
        _entity_xml(
            node_id="n0",
            type_attr="maltego.Domain",
            primary_prop_name="fqdn",
            primary_value="acme.example.test",
            extra_props={"description": "v2", "country": "US"},
        )
    )
    res2 = client.post(
        f"/engagements/{eng['slug']}/entities/import/maltego",
        files={"file": ("second.mtgx", second, "application/zip")},
        headers=_headers(),
    )
    assert res2.status_code == 201, res2.text
    body2 = res2.json()
    assert body2["merged"] == 1
    assert body2["inserted"] == 0

    # Exactly one row (UPSERT, not append).
    rows = list(
        db.execute(
            select(Entity).where(Entity.engagement_id == uuid.UUID(eng["id"]))
        ).scalars()
    )
    assert len(rows) == 1
    row = rows[0]
    # Both old keys (description) and new keys (country) present;
    # description bumped to v2 by the merge.
    assert row.properties["description"] == "v2"
    assert row.properties["country"] == "US"
    assert row.source_attribution == "second.mtgx"


def test_import_endpoint_400_on_bad_upload(
    client: TestClient, cleanup_slugs: list[str]
) -> None:
    eng = _create(client, "mtgx bad")
    cleanup_slugs.append(eng["slug"])

    res = client.post(
        f"/engagements/{eng['slug']}/entities/import/maltego",
        files={"file": ("bad.mtgx", b"definitely not a zip", "application/zip")},
        headers=_headers(),
    )
    assert res.status_code == 400


def test_list_stored_returns_persisted_rows(
    client: TestClient,
    db: Session,
    cleanup_slugs: list[str],
) -> None:
    eng = _create(client, "mtgx list")
    cleanup_slugs.append(eng["slug"])

    xml = _make_mtgx(
        _entity_xml(
            node_id="n0",
            type_attr="maltego.Domain",
            primary_prop_name="fqdn",
            primary_value="acme.example.test",
        ),
        _entity_xml(
            node_id="n1",
            type_attr="maltego.EmailAddress",
            primary_prop_name="email",
            primary_value="admin@acme.example.test",
        ),
    )
    client.post(
        f"/engagements/{eng['slug']}/entities/import/maltego",
        files={"file": ("scan.mtgx", xml, "application/zip")},
        headers=_headers(),
    )

    # Unfiltered.
    res = client.get(
        f"/engagements/{eng['slug']}/entities/stored", headers=_headers()
    )
    assert res.status_code == 200, res.text
    assert len(res.json()) == 2

    # Filter by type.
    res2 = client.get(
        f"/engagements/{eng['slug']}/entities/stored?type=domain",
        headers=_headers(),
    )
    assert res2.status_code == 200
    values = [e["value"] for e in res2.json()]
    assert values == ["acme.example.test"]

    # Substring query on value.
    res3 = client.get(
        f"/engagements/{eng['slug']}/entities/stored?q=admin",
        headers=_headers(),
    )
    assert res3.status_code == 200
    values = [e["value"] for e in res3.json()]
    assert values == ["admin@acme.example.test"]


def test_parsed_entity_is_duck_typed_against_persist_helper() -> None:
    """Sanity: ParsedEntity exposes the attribute set the persistence
    helper reads (type, value, properties)."""
    item = ParsedEntity(
        type="domain",
        value="x",
        properties={"k": "v"},
        maltego_type="maltego.Domain",
    )
    for attr in ("type", "value", "properties"):
        assert hasattr(item, attr)
