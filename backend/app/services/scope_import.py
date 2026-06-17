"""Bulk scope-import parser.

Takes a free-form blob (.txt / .csv body or a textarea paste) and produces a
list of structured scope items. Pure function — no DB, no FastAPI deps —
so the endpoint can call it directly and tests can hammer it without a
session.

Per-line rules:

- Trailing/leading whitespace stripped, line endings normalized.
- Empty lines and lines starting with ``#`` are skipped.
- Commas inside a line act as additional separators (so a CSV with one
  column of targets parses the same as a newline-separated .txt list).
- A leading ``!`` marks the target as an exclusion. ``!10.0.0.5/32`` excludes
  that host; ``!evil.acme.test`` excludes that subdomain.
- Kind is auto-detected per token:
    * contains ``://``                     -> url
    * parses as ip_network (strict=False) AND has '/' -> cidr
    * parses as ip_address                 -> ip
    * matches the domain regex             -> domain
- Tokens that fail every detector are reported back with a line number so
  the analyst can fix them; the rest still import.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from app.models import ScopeKind

# Conservative domain regex — labels 1-63 chars, total <=253, TLD letters only.
# Doesn't try to validate every RFC edge case; rejecting an oddball is fine
# because the analyst can add it manually.
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ParsedScopeRow:
    """One successfully-parsed token from the import blob."""

    line: int
    value: str
    kind: ScopeKind
    is_exclusion: bool


@dataclass(frozen=True, slots=True)
class ScopeImportError:
    """One token the parser couldn't classify."""

    line: int
    raw: str
    reason: str


def detect_kind(value: str) -> ScopeKind | None:
    """Return the best-fit ``ScopeKind`` for ``value`` or ``None`` if nothing matches."""
    v = value.strip()
    if not v:
        return None
    if "://" in v:
        return ScopeKind.url
    if "/" in v:
        try:
            ipaddress.ip_network(v, strict=False)
            return ScopeKind.cidr
        except ValueError:
            return None
    try:
        ipaddress.ip_address(v)
        return ScopeKind.ip
    except ValueError:
        pass
    if _DOMAIN_RE.match(v):
        return ScopeKind.domain
    return None


def parse_scope_text(
    text: str,
) -> tuple[list[ParsedScopeRow], list[ScopeImportError]]:
    """Parse a free-form scope blob.

    Returns ``(rows, errors)``. Both lists carry 1-based line numbers from the
    source text so the UI can highlight problem rows.
    """
    rows: list[ParsedScopeRow] = []
    errors: list[ScopeImportError] = []

    # Strip optional UTF-8 BOM; normalize CRLF.
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    for lineno, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # CSV-friendly: a single line can carry multiple comma-separated tokens.
        for token in line.split(","):
            token = token.strip()
            if not token or token.startswith("#"):
                continue
            is_exclusion = False
            if token.startswith("!"):
                is_exclusion = True
                token = token[1:].strip()
                if not token:
                    errors.append(
                        ScopeImportError(
                            line=lineno,
                            raw=raw_line,
                            reason="'!' exclusion marker with no value",
                        )
                    )
                    continue
            kind = detect_kind(token)
            if kind is None:
                errors.append(
                    ScopeImportError(
                        line=lineno,
                        raw=token,
                        reason=(
                            "could not classify as url, cidr, ip, or domain"
                        ),
                    )
                )
                continue
            rows.append(
                ParsedScopeRow(
                    line=lineno,
                    value=token,
                    kind=kind,
                    is_exclusion=is_exclusion,
                )
            )

    return rows, errors
