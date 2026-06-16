"""Microsoft Entra ID access-token validation.

The viewer SPA signs analysts in with MSAL and sends the resulting access
token as ``Authorization: Bearer <jwt>``. This module validates that token
against the tenant's published JWKS (signature, issuer, audience, expiry) and
returns its claims. ``app.api.deps.current_user`` maps the claims to a ``User``.

JWKS is fetched with ``httpx`` (not PyJWT's bundled urllib client) so tests can
mock it with ``respx``, and cached in-process with a short TTL. On a ``kid``
miss the cache is busted once and refetched, covering Entra key rotation.
"""
from __future__ import annotations

import json
import time

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.core.config import settings

_JWKS_TTL_SECONDS = 3600.0
# uri -> (fetched_at_monotonic, jwks_dict)
_jwks_cache: dict[str, tuple[float, dict]] = {}


class EntraError(Exception):
    """Raised when an Entra token is missing, malformed, or fails validation."""


def _fetch_jwks(uri: str, *, force: bool = False) -> dict:
    now = time.monotonic()
    cached = _jwks_cache.get(uri)
    if not force and cached is not None and (now - cached[0]) < _JWKS_TTL_SECONDS:
        return cached[1]
    try:
        resp = httpx.get(uri, timeout=5.0)
        resp.raise_for_status()
        jwks = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise EntraError(f"could not fetch JWKS: {exc}") from exc
    _jwks_cache[uri] = (now, jwks)
    return jwks


def _signing_key(token: str, jwks_uri: str):
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise EntraError(f"malformed token header: {exc}") from exc
    if not kid:
        raise EntraError("token header has no 'kid'")

    for force in (False, True):  # second pass busts the cache for key rotation
        for key in _fetch_jwks(jwks_uri, force=force).get("keys", []):
            if key.get("kid") == kid:
                return RSAAlgorithm.from_jwk(json.dumps(key))
    raise EntraError("no matching signing key for token 'kid'")


def _accepted_audiences() -> list[str]:
    # Entra v2 access tokens carry `aud` as either the app's client id (GUID)
    # or the api://<client-id> URI depending on configuration — accept both,
    # plus an explicit override if set.
    candidates = {
        settings.entra_audience,
        settings.entra_client_id,
        f"api://{settings.entra_client_id}" if settings.entra_client_id else "",
    }
    return [a for a in candidates if a]


def validate_token(token: str) -> dict:
    """Validate an Entra access token and return its claims.

    Raises ``EntraError`` on any failure (not configured, bad signature, wrong
    audience/issuer, expired, …). Never raises a bare library exception.
    """
    if not settings.entra_enabled:
        raise EntraError("Entra auth is not configured")
    if not token:
        raise EntraError("empty bearer token")

    signing_key = _signing_key(token, settings.entra_jwks_uri)
    try:
        return jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=_accepted_audiences(),
            issuer=settings.entra_issuer,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise EntraError(f"token validation failed: {exc}") from exc
