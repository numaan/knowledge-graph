"""Control API: app factory, JWT auth (test JWKS), /healthz, /me, start-up key check."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from kg_pipeline.control import app as app_mod
from kg_pipeline.control.app import create_app
from kg_pipeline.control.auth import Authenticator, require_admin

ISSUER = "https://sso.example.com/realms/kg"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(groups=("kg-admins",), aud="kg-mcp", iss=ISSUER, exp_in=300, user="alice", claim="groups", key=KEY):
    claims = {"iss": iss, **({"aud": aud} if aud else {}), "exp": int(time.time()) + exp_in, "preferred_username": user, claim: list(groups)}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def client(monkeypatch, issuer=ISSUER, **env):
    for k in ("KG_OIDC_ISSUER", "KG_OIDC_AUDIENCE", "KG_OIDC_GROUPS_CLAIM", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    if issuer:
        monkeypatch.setenv("KG_OIDC_ISSUER", issuer)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    auth = Authenticator.from_env(key_resolver=lambda tok: KEY.public_key())
    app = create_app(auth=auth)
    probe = APIRouter()

    @probe.get("/api/v1/sources")
    def sources(user=Depends(require_admin)):
        return {"items": [], "user": user.name}

    app.include_router(probe)
    return TestClient(app)


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def test_healthz_needs_no_token(monkeypatch):
    c = client(monkeypatch)
    r = c.get("/api/v1/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_no_token_is_401_with_error_body(monkeypatch):
    c = client(monkeypatch)
    for path in ("/api/v1/me", "/api/v1/sources"):
        r = c.get(path)
        assert r.status_code == 401
        assert "error" in r.json()


def test_bad_tokens_are_401(monkeypatch):
    c = client(monkeypatch)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for t in (token(exp_in=-120), token(iss="https://evil"), token(key=other), "garbage"):
        assert c.get("/api/v1/me", headers=bearer(t)).status_code == 401


def test_wrong_audience_is_401(monkeypatch):
    c = client(monkeypatch, KG_OIDC_AUDIENCE="kg-control")
    assert c.get("/api/v1/me", headers=bearer(token(aud="other"))).status_code == 401
    assert c.get("/api/v1/me", headers=bearer(token(aud="kg-control"))).status_code == 200


def test_token_without_group_is_403_except_me(monkeypatch):
    c = client(monkeypatch)
    t = token(groups=("staff",))
    r = c.get("/api/v1/sources", headers=bearer(t))
    assert r.status_code == 403 and "error" in r.json()
    r = c.get("/api/v1/me", headers=bearer(t))
    assert r.status_code == 200
    assert r.json() == {"user": "alice", "is_admin": False}


def test_admin_group_is_200(monkeypatch):
    c = client(monkeypatch)
    t = token()
    assert c.get("/api/v1/me", headers=bearer(t)).json() == {"user": "alice", "is_admin": True}
    assert c.get("/api/v1/sources", headers=bearer(t)).status_code == 200


def test_groups_claim_is_configurable_and_dotted(monkeypatch):
    c = client(monkeypatch, KG_OIDC_GROUPS_CLAIM="roles")
    assert c.get("/api/v1/me", headers=bearer(token(claim="roles"))).json()["is_admin"] is True
    assert c.get("/api/v1/me", headers=bearer(token())).json()["is_admin"] is False


def test_local_mode_without_issuer(monkeypatch):
    c = client(monkeypatch, issuer=None)
    assert c.get("/api/v1/me").json() == {"user": "local", "is_admin": True}
    assert c.get("/api/v1/sources").status_code == 200
    assert c.get("/api/v1/healthz").status_code == 200


def test_unknown_route_has_error_body(monkeypatch):
    c = client(monkeypatch)
    r = c.get("/api/v1/nope")
    assert r.status_code == 404 and "error" in r.json()


# --- start-up key check (decision at the gate) ---

def test_factory_exits_naming_key_when_database_set_and_key_missing(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.delenv("KG_SECRET_KEY", raising=False)
    with pytest.raises(SystemExit) as e:
        create_app()
    assert "KG_SECRET_KEY" in str(e.value)


def test_factory_ok_with_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("KG_OIDC_ISSUER", raising=False)
    assert create_app() is not None


def test_main_exits_naming_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.delenv("KG_SECRET_KEY", raising=False)
    from kg_pipeline.control import __main__ as m
    with pytest.raises(SystemExit) as e:
        m.main()
    assert "KG_SECRET_KEY" in str(e.value)


def test_main_runs_uvicorn_on_8090(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("KG_OIDC_ISSUER", raising=False)
    from kg_pipeline.control import __main__ as m
    seen = {}
    monkeypatch.setattr(m.uvicorn, "run", lambda app, **kw: seen.update(kw))
    m.main()
    assert seen["port"] == 8090 and seen["host"] == "0.0.0.0"
    assert app_mod  # module imported


def test_admin_group_is_configurable(monkeypatch):
    c = client(monkeypatch, KG_CONTROL_ADMIN_GROUP="ops-admins")
    assert c.get("/api/v1/me", headers=bearer(token(groups=("kg-admins",)))).json()["is_admin"] is False
    assert c.get("/api/v1/me", headers=bearer(token(groups=("/ops-admins",)))).json()["is_admin"] is True
    r = c.get("/api/v1/sources", headers=bearer(token(groups=("kg-admins",))))
    assert r.status_code == 403 and "ops-admins" in r.json()["error"]


def test_admin_group_defaults_to_kg_admins(monkeypatch):
    monkeypatch.delenv("KG_CONTROL_ADMIN_GROUP", raising=False)
    c = client(monkeypatch)
    assert c.get("/api/v1/me", headers=bearer(token())).json()["is_admin"] is True


def test_empty_admin_group_env_uses_default(monkeypatch):
    c = client(monkeypatch, KG_CONTROL_ADMIN_GROUP="")
    assert c.get("/api/v1/me", headers=bearer(token())).json()["is_admin"] is True


# ---- FIX 5: local mode is loud ----
def test_local_mode_logs_a_warning_at_start(caplog):
    from kg_pipeline.control.app import create_app
    with caplog.at_level("WARNING"):
        create_app(auth=Authenticator())
    assert "KG_OIDC_ISSUER is not set" in caplog.text and "every caller is admin" in caplog.text


def test_oidc_mode_logs_no_local_warning(caplog):
    from kg_pipeline.control.app import create_app
    with caplog.at_level("WARNING"):
        create_app(auth=Authenticator(issuer="https://sso/realms/kg", audience="kg-mcp"))
    assert "KG_OIDC_ISSUER is not set" not in caplog.text


# ---- FIX 6: the audience is checked by default ----
def test_audience_defaults_to_kg_mcp(monkeypatch):
    c = client(monkeypatch)
    assert c.get("/api/v1/me", headers=bearer(token(aud=None))).status_code == 401  # no aud claim
    assert c.get("/api/v1/me", headers=bearer(token(aud="other"))).status_code == 401
    assert c.get("/api/v1/me", headers=bearer(token(aud="kg-mcp"))).status_code == 200


def test_explicit_empty_audience_refuses_start(monkeypatch):
    for value in ("", "  "):
        with pytest.raises(SystemExit, match="KG_OIDC_AUDIENCE"):
            client(monkeypatch, KG_OIDC_AUDIENCE=value)


def test_local_mode_ignores_empty_audience(monkeypatch):
    client(monkeypatch, issuer=None, KG_OIDC_AUDIENCE="")
