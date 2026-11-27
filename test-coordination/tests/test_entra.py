"""Microsoft Entra ID sign-in (AUTH_MODE=oidc): token validation, organisation and person mapping, roles.

Tokens are signed here with a locally generated RSA key and served as the JWKS, exactly as Entra would.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from app import config, security
from app.db import worker_engine

ENTRA_TID = "7d1f3c2a-5b6e-4f80-9a1b-2c3d4e5f6a7b"
CLIENT_ID = "0b6f8c3e-1d2a-4e5f-8a9b-c0d1e2f3a4b5"
KID = "test-key-1"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNER = _key()
STRANGER = _key()   # a key Entra never published


@pytest.fixture
def oidc(monkeypatch, seeded):
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(SIGNER.public_key(), as_dict=True)
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    monkeypatch.setattr(security, "_fetch_jwks", lambda url: {"keys": [jwk]})
    monkeypatch.setitem(security._jwks, "keys", {})
    monkeypatch.setitem(security._jwks, "fetched_at", 0.0)
    monkeypatch.setattr(config, "settings", replace(config.settings, auth_mode="oidc",
                                                    entra_tenant_id=ENTRA_TID, entra_client_id=CLIENT_ID))
    with worker_engine().begin() as c:
        c.execute(text("UPDATE tenants SET entra_tenant_id = :e WHERE id = :t"), {"e": ENTRA_TID, "t": seeded["A"]["tenant"]})
    yield seeded["A"]
    with worker_engine().begin() as c:
        c.execute(text("UPDATE tenants SET entra_tenant_id = NULL WHERE id = :t"), {"t": seeded["A"]["tenant"]})


def token(*, key=SIGNER, kid=KID, alg="RS256", **over):
    """A signed access token; any claim can be overridden, and a claim set to None is left out."""
    now = int(time.time())
    claims = {"iss": f"https://login.microsoftonline.com/{ENTRA_TID}/v2.0", "aud": CLIENT_ID, "tid": ENTRA_TID,
              "oid": str(uuid.uuid4()), "scp": "access_as_user", "iat": now, "nbf": now, "exp": now + 3600,
              "name": "Test Person", "preferred_username": "nobody@bank.example", "ver": "2.0"}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def call(client, tok, path="/api/v1/me"):
    return client.get(path, headers={"Authorization": f"Bearer {tok}"})


def provision(tenant_id, email, roles=("TechnicalStakeholder",)):
    with worker_engine().begin() as c:
        c.execute(text("""INSERT INTO users (tenant_id, idp_subject, display_name, email, roles)
                          VALUES (:t, :s, :n, :e, :r)"""),
                  {"t": tenant_id, "s": f"pending:{email}", "n": email.split("@")[0].title(), "e": email, "r": list(roles)})


def test_first_sign_in_links_person_provisioned_by_email(client, oidc):
    provision(oidc["tenant"], "asha.rao@bank.example")
    oid = str(uuid.uuid4())
    r = call(client, token(oid=oid, preferred_username="Asha.Rao@bank.example"))
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["display_name"] == "Asha.Rao" and me["tenant_id"] == str(oidc["tenant"])
    with worker_engine().connect() as c:
        row = c.execute(text("SELECT idp_subject, idp_bound_at FROM users WHERE email = 'asha.rao@bank.example'")).one()
    assert row.idp_subject == oid and row.idp_bound_at is not None
    # Afterwards the object id alone identifies them, whatever the email claim says.
    assert call(client, token(oid=oid, preferred_username="renamed@bank.example")).json()["user_id"] == me["user_id"]


def test_a_linked_account_cannot_be_claimed_again_by_email(client, oidc):
    provision(oidc["tenant"], "dev.shah@bank.example")
    assert call(client, token(oid=str(uuid.uuid4()), preferred_username="dev.shah@bank.example")).status_code == 200
    r = call(client, token(oid=str(uuid.uuid4()), preferred_username="dev.shah@bank.example"))   # different Entra account
    assert r.status_code == 403 and "isn't set up in ResilienceAI" in r.json()["error"]


def test_entra_app_roles_set_the_persons_roles(client, oidc):
    provision(oidc["tenant"], "ravi.k@bank.example", roles=("TechnicalStakeholder",))
    oid = str(uuid.uuid4())
    r = call(client, token(oid=oid, preferred_username="ravi.k@bank.example", roles=["TestManager", "NotARealRole"]))
    assert r.json()["roles"] == ["TestManager"]                       # unknown role values are ignored
    assert call(client, token(oid=oid, preferred_username="ravi.k@bank.example")).json()["roles"] == ["TestManager"]  # no claim: kept


def test_unknown_person_and_unknown_directory_are_refused(client, oidc, monkeypatch):
    r = call(client, token(preferred_username="stranger@bank.example"))
    assert r.status_code == 403 and "Ask your administrator to add you" in r.json()["error"]
    other_tid = str(uuid.uuid4())
    monkeypatch.setattr(config, "settings", replace(config.settings, entra_tenant_id="organizations"))
    r = call(client, token(tid=other_tid, iss=f"https://login.microsoftonline.com/{other_tid}/v2.0"))
    assert r.status_code == 403 and "organisation isn't set up" in r.json()["error"]


@pytest.mark.parametrize("bad, reason", [
    (dict(exp=int(time.time()) - 600, iat=int(time.time()) - 4000, nbf=int(time.time()) - 4000), "expired"),
    (dict(aud="some-other-api"), "Invalid token"),
    (dict(iss="https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000/v2.0"), "Invalid token"),
    (dict(scp="User.Read"), "not issued for this API"),
    (dict(oid=None), "Invalid token"),
    (dict(key=STRANGER), "Invalid token"),
    (dict(kid="rotated-away"), "unknown key"),
])
def test_bad_tokens_are_rejected(client, oidc, bad, reason):
    provision_email = f"{uuid.uuid4().hex[:8]}@bank.example"
    provision(oidc["tenant"], provision_email)
    r = call(client, token(**{"preferred_username": provision_email, **bad}))
    assert r.status_code == 401 and reason in r.json()["error"], r.text


def test_token_from_another_directory_is_rejected_when_one_directory_is_configured(client, oidc):
    other = str(uuid.uuid4())
    r = call(client, token(tid=other, iss=f"https://login.microsoftonline.com/{other}/v2.0"))
    assert r.status_code == 401 and "doesn't accept" in r.json()["error"]


def test_unsigned_and_header_auth_are_refused_in_oidc_mode(client, oidc):
    unsigned = jwt.encode({"tid": ENTRA_TID, "oid": str(uuid.uuid4())}, key=None, algorithm="none")
    assert call(client, unsigned).status_code == 401
    r = client.get("/api/v1/me", headers={"X-Tenant-Id": str(oidc["tenant"]), "X-User-Subject": "arjun.pm"})
    assert r.status_code == 401 and "Bearer token required" in r.json()["error"]


def test_auth_config_and_dev_directory(client, oidc, monkeypatch):
    cfg = client.get("/api/v1/auth/config").json()
    assert cfg == {"mode": "oidc", "provider": "entra", "client_id": CLIENT_ID,
                   "authority": f"https://login.microsoftonline.com/{ENTRA_TID}",
                   "scopes": [f"api://{CLIENT_ID}/access_as_user"], "break_glass": False}
    assert client.get("/api/v1/dev/directory").status_code == 404
    monkeypatch.setattr(config, "settings", replace(config.settings, auth_mode="dev"))
    assert client.get("/api/v1/auth/config").json() == {"mode": "dev", "break_glass": False}


def test_production_needs_entra_settings():
    s = replace(config.settings, app_env="production", auth_mode="oidc", entra_client_id="", entra_tenant_id="")
    assert any("ENTRA_CLIENT_ID" in p for p in config.check_production(s))
    s = replace(s, entra_client_id=CLIENT_ID, entra_tenant_id=ENTRA_TID)
    assert not any("ENTRA" in p for p in config.check_production(s))
