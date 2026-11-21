"""Connection test: SSRF guard, per-kind testers with fakes, audit, no secret in output, no Neo4j."""
import sys

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from kg_pipeline.control import db, probe
from kg_pipeline.control.app import create_app
from kg_pipeline.control.auth import Authenticator

SECRET = "tok-SECRET-123456"
JIRA = {"kind": "jira", "name": "jira-main", "config": {"base_url": "https://x.atlassian.net", "projects": ["A"]},
        "secret": {"email": "a@b.c", "api_token": SECRET}}
WIKI = {"kind": "confluence", "name": "wiki", "config": {"base_url": "https://x.atlassian.net", "spaces": ["ENG"]},
        "secret": {"email": "a@b.c", "api_token": SECRET}}
GIT = {"kind": "git", "name": "repo-a", "config": {"url": "https://git.example.com/a.git"}, "secret": {"token": SECRET}}
S3 = {"kind": "s3", "name": "docs", "config": {"bucket": "b"}, "secret": {"access_key_id": "AKIA1", "secret_access_key": SECRET}}
PUBLIC = "93.184.216.34"


class Fake:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, url):
        self.calls.append(url)
        if self.error:
            raise self.error
        return {"ok": 1}


@pytest.fixture
def env(monkeypatch):
    for k in ("KG_OIDC_ISSUER", "DATABASE_URL", "KG_CONTROL_ALLOW_HTTP", "KG_CONTROL_ALLOW_PRIVATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())


class FakeDagster:
    def add_partition(self, key):
        pass

    def remove_partition(self, key):
        pass


@pytest.fixture
def engine(env):
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)


@pytest.fixture
def app(engine):
    a = create_app(auth=Authenticator(), engine=engine)
    a.state.dagster = FakeDagster()
    a.state.prober = probe.Prober(resolver=lambda host: [PUBLIC], fetch=Fake())
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def make(client, body):
    r = client.post("/api/v1/sources", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def run_test(client, sid):
    r = client.post(f"/api/v1/sources/{sid}/test")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("ip,word", [
    ("127.0.0.1", "loopback"), ("::1", "loopback"), ("169.254.169.254", "link-local"), ("fe80::1", "link-local"),
    ("10.0.0.5", "private"), ("192.168.1.1", "private"), ("172.16.0.1", "private"), ("fd00::1", "private"),
    ("224.0.0.1", "multicast"), ("240.0.0.1", "reserved"), ("::ffff:127.0.0.1", "loopback"),
    ("::ffff:169.254.169.254", "link-local"), ("0.0.0.0", "unspecified"),
])
def test_guard_refuses_and_makes_no_request(app, client, ip, word):
    fake = Fake()
    app.state.prober = probe.Prober(resolver=lambda host: [ip], fetch=fake)
    out = run_test(client, make(client, JIRA))
    assert out["ok"] is False and "SSRF guard" in out["message"] and word in out["message"]
    assert fake.calls == []


def test_guard_checks_every_resolved_address(app, client):
    fake = Fake()
    app.state.prober = probe.Prober(resolver=lambda host: [PUBLIC, "127.0.0.1"], fetch=fake)
    assert run_test(client, make(client, JIRA))["ok"] is False and fake.calls == []


def test_allow_private_makes_the_request(app, client, monkeypatch):
    monkeypatch.setenv("KG_CONTROL_ALLOW_PRIVATE", "1")
    fake = Fake()
    app.state.prober = probe.Prober(resolver=lambda host: ["169.254.169.254"], fetch=fake)
    assert run_test(client, make(client, JIRA))["ok"] is True
    assert len(fake.calls) == 1


@pytest.mark.parametrize("body,path", [(JIRA, "/rest/api/3/myself"), (WIKI, "/wiki/rest/api/space")])
def test_atlassian_ok_and_401(app, client, body, path):
    fake = Fake()
    app.state.prober = probe.Prober(resolver=lambda h: [PUBLIC], fetch=fake)
    out = run_test(client, make(client, body))
    assert out["ok"] is True and isinstance(out["details"], dict)
    assert fake.calls[0].startswith("https://x.atlassian.net" + path)
    fake.error = RuntimeError(f"GET https://x.atlassian.net{path} failed: HTTP 401")
    out = run_test(client, make(client, {**body, "name": "other"}))
    assert out["ok"] is False and "401" in out["message"]


def test_atlassian_without_secret_is_not_ok(client):
    out = run_test(client, make(client, {k: v for k, v in JIRA.items() if k != "secret"}))
    assert out["ok"] is False and "secret" in out["message"]


def test_git_runner_gets_url_and_token_not_in_argv(app, client):
    seen = {}

    def runner(args, env, timeout):
        seen.update(args=args, env=env)
        return 0, "abc\trefs/heads/main\n", ""

    app.state.prober = probe.Prober(resolver=lambda h: [PUBLIC], git_runner=runner)
    out = run_test(client, make(client, GIT))
    assert out["ok"] is True
    assert "ls-remote" in seen["args"] and "https://git.example.com/a.git" in seen["args"]
    assert not any(SECRET in a for a in seen["args"])
    assert SECRET not in out["message"] and SECRET not in str(out["details"])


def test_git_failure_scrubs_secret_and_guard_blocks(app, client):
    app.state.prober = probe.Prober(resolver=lambda h: [PUBLIC],
                                    git_runner=lambda a, e, t: (128, "", f"fatal: auth failed for {SECRET}"))
    out = run_test(client, make(client, GIT))
    assert out["ok"] is False and SECRET not in str(out)
    calls = []
    app.state.prober = probe.Prober(resolver=lambda h: ["10.1.1.1"], git_runner=lambda a, e, t: calls.append(1))
    out = run_test(client, make(client, {**GIT, "name": "repo-b"}))
    assert out["ok"] is False and "SSRF guard" in out["message"] and calls == []


def test_s3_without_boto3(app, client, monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    app.state.prober = probe.Prober(resolver=lambda h: [PUBLIC])
    out = run_test(client, make(client, S3))
    assert out == {"ok": False, "message": "boto3 not installed", "details": {}}


def test_s3_with_fake_client(app, client):
    class C:
        def head_bucket(self, Bucket):
            assert Bucket == "b"

    app.state.prober = probe.Prober(resolver=lambda h: [PUBLIC], s3_client=lambda cfg, sec: C())
    assert run_test(client, make(client, S3))["ok"] is True


def test_s3_endpoint_is_guarded(app, client):
    called = []
    app.state.prober = probe.Prober(resolver=lambda h: ["127.0.0.1"], s3_client=lambda c, s: called.append(1))
    body = {**S3, "config": {"bucket": "b", "endpoint": "https://minio.internal:9000"}}
    out = run_test(client, make(client, body))
    assert out["ok"] is False and "SSRF guard" in out["message"] and called == []


def test_audit_written_without_secret_and_unknown_is_404(client, engine):
    sid = make(client, JIRA)
    out = run_test(client, sid)
    with engine.connect() as conn:
        rows = conn.execute(select(db.audit).where(db.audit.c.action == "source.test")).all()
    assert len(rows) == 1 and rows[0].target == sid
    assert rows[0].detail == {"kind": "jira", "ok": out["ok"]}
    assert SECRET not in str(rows[0]) and SECRET not in str(out)
    assert client.post("/api/v1/sources/src_nope/test").status_code == 404


def test_blocked_test_is_audited(app, client, engine):
    app.state.prober = probe.Prober(resolver=lambda h: ["127.0.0.1"], fetch=Fake())
    run_test(client, make(client, JIRA))
    with engine.connect() as conn:
        row = conn.execute(select(db.audit).where(db.audit.c.action == "source.test")).first()
    assert row.detail == {"kind": "jira", "ok": False}


def test_no_neo4j_in_probe():
    import inspect
    assert "neo4j" not in inspect.getsource(probe).lower()
