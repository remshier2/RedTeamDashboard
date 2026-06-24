"""Maltego .mtgx graph import — Phase 10.

Parses Maltego's Graph Exchange Format (``.mtgx``, a ZIP containing
GraphML XML in ``Graphs/`` using the ``mtg`` namespace) into a list of
entity-shaped rows that flow through ``entity_store.persist_entities``.
Imported entities land in the new ``entities`` table — NOT the
``findings`` table — because Maltego entities are OSINT data points
(domains, emails, persons, ASNs, phone numbers), not security
findings. Mapping them to findings would flood the report with
``severity=info`` rows that aren't really findings.

Charter posture (§16 RESOLVED): Maltego is import-first like Nessus.
The analyst runs Maltego on their own infra and uploads the .mtgx
export. We don't talk to the Maltego server or shell out to any tool.

Safety:
- ``zipfile`` extracts in-memory only — no disk writes.
- ``defusedxml.ElementTree`` parses the GraphML to defang XXE /
  billion-laughs in untrusted input.
- ``zipfile.is_zipfile`` rejects non-ZIP uploads before we read.
- Per-file size cap on uncompressed GraphML prevents zip-bomb pivots.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import Any

from defusedxml import ElementTree

# Maltego's GraphML namespace. Constant since 2010-era Maltego releases.
_MTG_NS = "http://maltego.paterva.com/xml/mtgx"
_GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"

# Resolve the type prefix in path-qualified lookups.
_MTG = f"{{{_MTG_NS}}}"
_GML = f"{{{_GRAPHML_NS}}}"

# Hard cap on a single GraphML's uncompressed size. .mtgx exports run
# 50–500KB in practice; ten megabytes leaves slack for unusual graphs
# while preventing a malicious zip from claiming gigabytes.
_MAX_GRAPHML_BYTES = 10 * 1024 * 1024


# Normalize Maltego's namespaced types to our shared entity vocabulary
# (matching the existing derived-entities view's terms wherever they
# overlap). Types not in this map pass through verbatim — analysts can
# filter on the raw ``maltego.*`` string in the UI.
_TYPE_NORMALIZE: dict[str, str] = {
    "maltego.Domain": "domain",
    "maltego.DNSName": "domain",
    "maltego.Website": "url",
    "maltego.URL": "url",
    "maltego.EmailAddress": "email",
    "maltego.IPv4Address": "ip",
    "maltego.IPv6Address": "ip",
    "maltego.NetworkBlock": "cidr",
    "maltego.AS": "asn",
    "maltego.Person": "person",
    "maltego.PhoneNumber": "phone",
    "maltego.Hash": "hash",
}

# For each Maltego entity type, which property name carries the
# canonical "value" of the entity. Falls back to the first property
# when the type isn't listed.
_VALUE_PROPERTY_FOR_TYPE: dict[str, str] = {
    "maltego.Domain": "fqdn",
    "maltego.DNSName": "fqdn",
    "maltego.Website": "fqdn",
    "maltego.URL": "short.title",
    "maltego.EmailAddress": "email",
    "maltego.IPv4Address": "ipv4-address",
    "maltego.IPv6Address": "ipv6-address",
    "maltego.NetworkBlock": "ipv4-range",
    "maltego.AS": "as.number",
    "maltego.Person": "person.fullname",
    "maltego.PhoneNumber": "phone.numbermatcher",
    "maltego.Hash": "properties.hash",
}


@dataclass
class ParsedEntity:
    """One Maltego entity reduced to our entity vocabulary."""

    type: str  # normalized
    value: str
    properties: dict[str, Any] = field(default_factory=dict)
    # The original Maltego type (e.g. "maltego.Domain") preserved for
    # round-trip debugging + analyst filtering by Maltego-specific type.
    maltego_type: str = ""


@dataclass
class ParseResult:
    items: list[ParsedEntity]
    skipped_empty: int
    skipped_unknown: int
    total_nodes: int


def _extract_graphml_bytes(zip_bytes: bytes) -> bytes:
    """Open the .mtgx zip in-memory, find the first ``Graphs/*.graphml``
    member, return its uncompressed bytes. Raises ``ValueError`` on any
    structural problem (not a zip, missing graphml, oversize)."""
    if not zipfile.is_zipfile(io.BytesIO(zip_bytes)):
        raise ValueError("not a .mtgx zip archive")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        graphml_names = [
            n
            for n in zf.namelist()
            if n.startswith("Graphs/") and n.endswith(".graphml")
        ]
        if not graphml_names:
            raise ValueError(
                "no Graphs/*.graphml entry in archive — not a Maltego .mtgx?"
            )
        info = zf.getinfo(graphml_names[0])
        if info.file_size > _MAX_GRAPHML_BYTES:
            raise ValueError(
                f"GraphML entry too large ({info.file_size} bytes; "
                f"max {_MAX_GRAPHML_BYTES})"
            )
        return zf.read(graphml_names[0])


def _read_properties(entity_elem: Any) -> tuple[dict[str, str], str | None]:
    """Walk ``mtg:Properties > mtg:Property > mtg:Value`` children.

    Returns (properties_dict_by_name, first_property_value_or_None).
    The first-property-value fallback is used when the entity type
    doesn't have a known canonical-value property.
    """
    props: dict[str, str] = {}
    first_value: str | None = None
    props_container = entity_elem.find(f"{_MTG}Properties")
    if props_container is None:
        return props, first_value
    for prop in props_container.findall(f"{_MTG}Property"):
        name = prop.attrib.get("name", "")
        if not name:
            continue
        value_elem = prop.find(f"{_MTG}Value")
        if value_elem is None or value_elem.text is None:
            continue
        text = value_elem.text.strip()
        if not text:
            continue
        props[name] = text
        if first_value is None:
            first_value = text
    return props, first_value


def parse_mtgx(
    zip_bytes: bytes, *, source_attribution: str | None = None
) -> ParseResult:
    """Parse a Maltego ``.mtgx`` export.

    Returns a list of normalized entities + counters for empty/unknown
    nodes so the API can surface the breakdown. Raises ``ValueError``
    on structural problems (not a zip, missing graphml, malformed XML,
    wrong namespace).

    ``source_attribution`` is stored on each item for downstream
    persistence (typically the uploaded filename).
    """
    graphml = _extract_graphml_bytes(zip_bytes)
    try:
        root = ElementTree.fromstring(graphml)
    except ElementTree.ParseError as exc:
        raise ValueError(f"invalid GraphML inside .mtgx: {exc}") from exc

    # Sanity-check namespaces. GraphML's default ns is graphdrawing.org;
    # Maltego entities are in the mtg namespace as inner-data nodes. If
    # the file isn't a GraphML at all (e.g., someone zipped a CSV), the
    # node iteration below just returns empty.
    if not root.tag.endswith("graphml"):
        raise ValueError(
            f"unexpected root element {root.tag!r}; expected graphml"
        )

    items: list[ParsedEntity] = []
    skipped_empty = 0
    skipped_unknown = 0
    total_nodes = 0

    # Walk every node anywhere in the document so we don't depend on the
    # exact <graph> nesting depth (Maltego occasionally wraps graphs).
    for node in root.iter(f"{_GML}node"):
        total_nodes += 1
        entity = None
        # The MaltegoEntity lives inside a <data> child; iterate so we
        # don't depend on the key id (varies by Maltego version).
        for data in node.findall(f"{_GML}data"):
            cand = data.find(f"{_MTG}MaltegoEntity")
            if cand is not None:
                entity = cand
                break
        if entity is None:
            skipped_unknown += 1
            continue

        maltego_type = entity.attrib.get("type", "").strip()
        if not maltego_type:
            skipped_unknown += 1
            continue

        properties, first_value = _read_properties(entity)
        canonical_prop = _VALUE_PROPERTY_FOR_TYPE.get(maltego_type)
        value: str | None = None
        if canonical_prop is not None:
            value = properties.get(canonical_prop)
        if not value:
            value = first_value
        if not value:
            skipped_empty += 1
            continue

        # Source attribution stamped onto properties so the UI can show
        # "from <filename>" alongside each row. Stored separately too on
        # the Entity model, but having it inline simplifies the read
        # path when joining derived + stored views.
        if source_attribution:
            properties = {**properties, "_source_attribution": source_attribution}

        normalized_type = _TYPE_NORMALIZE.get(maltego_type, maltego_type)

        items.append(
            ParsedEntity(
                type=normalized_type,
                value=value,
                properties=properties,
                maltego_type=maltego_type,
            )
        )

    return ParseResult(
        items=items,
        skipped_empty=skipped_empty,
        skipped_unknown=skipped_unknown,
        total_nodes=total_nodes,
    )
