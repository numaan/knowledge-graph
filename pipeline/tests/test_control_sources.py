"""Control API sources CRUD on SQLite: validation, write-only secrets, audit, delete detaches runs."""
import json
import re
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.pool import StaticPool

from kg_pipeline.control import db, secrets
from kg_pipeline.control.app import create_app
from kg_pipeline.control.auth import Authenticator

SECRET_VALUE = "s3cr3t-value-XYZ"


@pytest.fixture
def env(monkeypatch):
    for k in ("KG_OIDC_ISSUER", "DATABASE_URL", "KG_CONTROL_ALLOW_HTTP"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())


@pytest.fixture
def engine(env):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)

    @event.listens_for(eng, "connect")
    def _fk(dbapi, _):
        dbapi.execute("PRAGMA foreign_keys=ON")  # PostgreSQL gets the same effect from ON DELETE SET NULL

    return eng


@pytest.fixture
def client(engine):
    return TestClient(create_app(auth=Authenticator(), engine=engine))


BODIES = {
    "git": {"kind": "git", "name": "repo-a", "config": {"url": "https://git.example.com/a.git"},
            "secret": {"token": SECRET_VALUE}},
    "jira": {"kind": "jira", "name": "jira-main", "config": {"base_url": "https://x.atlassian.net", "projects": ["A", "B"]},
             "secret": {"email": "a@b.c", "api_token": SECRET_VALUE}},
    "confluence": {"kind": "confluence", "name": "wiki", "config": {"base_url": "https://x.atlassian.net", "spaces": ["ENG"]}},
    "s3": {"kind": "s3", "name": "docs-bucket", "config": {"bucket": "b"}},
}


@pytest.mark.parametrize("kind", list(BODIES))
def test_create_each_kind(client, kind):
    r = client.post("/api/v1/sources", json=BODIES[kind])
    assert r.status_code == 201, r.text
    assert re.match(r"^src_[0-9a-f]{12}$", r.json()["id"])
    assert r.json()["kind"] == kind and r.json()["origin"] == "ui" and r.json()["enabled"] is True


@pytest.mark.parametrize("patch", [
    {"config": {"url": "https://g.example.com/a.git", "bogus": 1}},
    {"name": "Bad Name"},
    {"config": {"url": "http://g.example.com/a.git"}},
    {"schedule": "not a cron"},
    {"extra": 1},
])
def test_create_invalid_is_400(client, patch):
    r = client.post("/api/v1/sources", json={**BODIES["git"], **patch})
    assert r.status_code == 400
    assert isinstance(r.json()["error"], str) and SECRET_VALUE not in r.text


def test_duplicate_name_is_409(client):
    assert client.post("/api/v1/sources", json=BODIES["git"]).status_code == 201
    r = client.post("/api/v1/sources", json=BODIES["git"])
    assert r.status_code == 409 and "error" in r.json()


def test_secret_is_write_only_and_encrypted(client, engine):
    created = client.post("/api/v1/sources", json=BODIES["jira"]).json()
    sid = created["id"]
    assert created["has_secret"] is True and created["secret_keys"] == ["api_token", "email"]
    client.patch(f"/api/v1/sources/{sid}", json={"schedule": "0 * * * *"})
    for r in (client.get("/api/v1/sources"), client.get(f"/api/v1/sources/{sid}"),
              client.patch(f"/api/v1/sources/{sid}", json={"enabled": False})):
        assert SECRET_VALUE not in r.text and "secret_keys" in r.text
    with engine.connect() as conn:
        token = conn.execute(select(db.sources.c.secret)).scalar_one()
        assert SECRET_VALUE not in token
        assert secrets.decrypt(token) == BODIES["jira"]["secret"]
        for row in conn.execute(select(db.audit)):
            assert SECRET_VALUE not in json.dumps(row.detail)


def test_patch_secret_null_keeps_and_empty_clears(client, engine):
    sid = client.post("/api/v1/sources", json=BODIES["git"]).json()["id"]
    r = client.patch(f"/api/v1/sources/{sid}", json={"secret": None, "schedule": "*/5 * * * *"})
    assert r.status_code == 200 and r.json()["has_secret"] is True and r.json()["schedule"] == "*/5 * * * *"
    r = client.patch(f"/api/v1/sources/{sid}", json={"secret": {"token": "new-value"}})
    assert r.json()["secret_keys"] == ["token"] and "new-value" not in r.text
    r = client.patch(f"/api/v1/sources/{sid}", json={"secret": {}})
    assert r.json()["has_secret"] is False and r.json()["secret_keys"] == []
    with engine.connect() as conn:
        assert conn.execute(select(db.sources.c.secret)).scalar_one() is None


def test_patch_validation_and_404(client):
    sid = client.post("/api/v1/sources", json=BODIES["git"]).json()["id"]
    assert client.patch(f"/api/v1/sources/{sid}", json={"config": {"url": "http://x.example.com"}}).status_code == 400
    assert client.patch(f"/api/v1/sources/{sid}", json={"secret": {"nope": "x"}}).status_code == 400
    assert client.patch(f"/api/v1/sources/{sid}", json={"name": "other"}).status_code == 400
    assert client.patch("/api/v1/sources/src_000000000000", json={}).status_code == 404
    assert client.get("/api/v1/sources/src_000000000000").status_code == 404
    assert client.delete("/api/v1/sources/src_000000000000").status_code == 404


def test_delete_sets_runs_source_id_null_and_audits(client, engine):
    sid = client.post("/api/v1/sources", json=BODIES["git"]).json()["id"]
    with engine.begin() as conn:
        conn.execute(insert(db.runs).values(
            id="run_000000000001", source_id=sid, source_name="repo-a", kind="git", status="succeeded",
            trigger="manual", mode="incremental", started_at=datetime.now(timezone.utc)))
    got = client.get(f"/api/v1/sources/{sid}").json()
    assert got["last_run"]["id"] == "run_000000000001" and got["last_run"]["status"] == "succeeded"
    assert client.delete(f"/api/v1/sources/{sid}").status_code == 204
    assert client.get(f"/api/v1/sources/{sid}").status_code == 404
    with engine.connect() as conn:
        assert conn.execute(select(db.runs.c.source_id)).scalar_one() is None
        actions = [r.action for r in conn.execute(select(db.audit).order_by(db.audit.c.at))]
        assert actions == ["source.create", "source.delete"]
        assert {r.actor for r in conn.execute(select(db.audit))} == {"local"}


def test_audit_has_update(client, engine):
    sid = client.post("/api/v1/sources", json=BODIES["git"]).json()["id"]
    client.patch(f"/api/v1/sources/{sid}", json={"enabled": False})
    with engine.connect() as conn:
        rows = conn.execute(select(db.audit).where(db.audit.c.action == "source.update")).all()
        assert len(rows) == 1 and rows[0].target == sid


def test_requires_admin_before_db(engine, monkeypatch):
    monkeypatch.setenv("KG_OIDC_ISSUER", "https://sso.example.com/realms/kg")
    c = TestClient(create_app(auth=Authenticator.from_env(key_resolver=lambda t: None), engine=engine))
    assert c.get("/api/v1/sources").status_code == 401
