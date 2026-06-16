"""Entra ID Bearer-token auth on ``current_user``.

Verifies the JWT validation path end-to-end against a mocked JWKS:
- a valid token resolves to (and lazily creates) a User by its ``oid``
- expired / wrong-audience / wrong-issuer / bad-signature tokens are 401
- when Entra is not configured, a Bearer token is ignored (falls through)

A throwaway RSA keypair signs the test tokens; the public half is served as
the JWKS via respx (app.core.entra fetches JWKS with httpx, so respx catches it).
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy.orm import Session

from app.core import entra
from app.core.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.models import User

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
KID = "test-signing-key"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def entra_configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "entra_tenant_id", TENANT)
    monkeypatch.setattr(settings, "entra_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "entra_audience", "")
    entra._jwks_cache.clear()
    yield
    entra._jwks_cache.clear()


def _jwks(key: rsa.RSAPrivateKey) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


def _token(key: rsa.RSAPrivateKey, **overrides: object) -> str:
    now = dt.datetime.now(tz=dt.UTC)
    claims: dict[str, object] = {
        "iss": settings.entra_issuer,
        "aud": CLIENT_ID,
        "exp": now + dt.timedelta(hours=1),
        "iat": now,
        "oid": f"oid-{uuid.uuid4().hex[:12]}",
        "preferred_username": f"analyst-{uuid.uuid4().hex[:6]}@example.com",
        "name": "Test Analyst",
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@respx.mock
def test_valid_bearer_resolves_and_creates_user(
    client: TestClient, db: Session, entra_configured: None, rsa_key: rsa.RSAPrivateKey
) -> None:
    respx.get(settings.entra_jwks_uri).mock(
        return_value=httpx.Response(200, json=_jwks(rsa_key))
    )
    oid = f"oid-{uuid.uuid4().hex[:12]}"
    email = f"analyst-{uuid.uuid4().hex[:6]}@example.com"
    token = _token(rsa_key, oid=oid, preferred_username=email, name="Jane Analyst")

    resp = client.post(
        "/engagements", json={"name": "Via Entra"}, headers=_bearer(token)
    )
    assert resp.status_code == 201, resp.text

    created_by = uuid.UUID(resp.json()["created_by"])
    user = db.get(User, created_by)
    assert user is not None
    assert user.entra_oid == oid
    assert user.email == email
    assert user.display_name == "Jane Analyst"


@respx.mock
def test_expired_token_is_401(
    client: TestClient, entra_configured: None, rsa_key: rsa.RSAPrivateKey
) -> None:
    respx.get(settings.entra_jwks_uri).mock(
        return_value=httpx.Response(200, json=_jwks(rsa_key))
    )
    past = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=1)
    token = _token(rsa_key, exp=past)
    resp = client.post("/engagements", json={"name": "x"}, headers=_bearer(token))
    assert resp.status_code == 401
    assert "invalid token" in resp.json()["detail"]


@respx.mock
def test_wrong_audience_is_401(
    client: TestClient, entra_configured: None, rsa_key: rsa.RSAPrivateKey
) -> None:
    respx.get(settings.entra_jwks_uri).mock(
        return_value=httpx.Response(200, json=_jwks(rsa_key))
    )
    token = _token(rsa_key, aud="api://some-other-app")
    resp = client.post("/engagements", json={"name": "x"}, headers=_bearer(token))
    assert resp.status_code == 401


@respx.mock
def test_wrong_issuer_is_401(
    client: TestClient, entra_configured: None, rsa_key: rsa.RSAPrivateKey
) -> None:
    respx.get(settings.entra_jwks_uri).mock(
        return_value=httpx.Response(200, json=_jwks(rsa_key))
    )
    token = _token(rsa_key, iss="https://login.microsoftonline.com/evil/v2.0")
    resp = client.post("/engagements", json={"name": "x"}, headers=_bearer(token))
    assert resp.status_code == 401


@respx.mock
def test_bad_signature_is_401(
    client: TestClient, entra_configured: None, rsa_key: rsa.RSAPrivateKey
) -> None:
    # JWKS publishes rsa_key's public half, but the token is signed by a
    # different key carrying the same kid → signature verification fails.
    respx.get(settings.entra_jwks_uri).mock(
        return_value=httpx.Response(200, json=_jwks(rsa_key))
    )
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _token(impostor)
    resp = client.post("/engagements", json={"name": "x"}, headers=_bearer(token))
    assert resp.status_code == 401


def test_bearer_ignored_when_entra_disabled(client: TestClient) -> None:
    # Default settings have Entra off. A Bearer token (and nothing else) must
    # not authenticate — it falls through to the missing-header 401.
    resp = client.post(
        "/engagements", json={"name": "x"}, headers=_bearer("not.a.real.token")
    )
    assert resp.status_code == 401
    assert "header required" in resp.json()["detail"]
