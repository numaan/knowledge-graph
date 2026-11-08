"""Audit route, the six audit actions, and an API-wide check that no secret value leaks."""
import base64
import json
import logging
import time

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from kg_pipeline.control import dagster_client as dc
from kg_pipeline.control import db, probe
from kg_pipeline.control.app import create_app
from kg_pipeline.control.auth import Authenticator

PUBLIC = "93.184.216.34"
BASE = "/api/v1"

# Distinctive values. Quotes, backslashes and non-ASCII make the JSON-escaped form differ from the plain form.
S_GIT = 'GITTOKEN-"q"-\\b-é-7731'
S_JIRA_MAIL = "jira-mail-9921@leak.example"
S_JIRA_TOK = 'JIRATOKEN-"q"-\\b-8842'
S_WIKI_MAIL = "wiki-mail-5513@leak.example"
S_WIKI_TOK = 'WIKITOKEN-"q"-\\b-6624'
S_S3_KEY = "AKIALEAKKEY7745"
S_S3_SEC = 'S3SECRET-"q"-\\b-3356'
NEW_GIT = 'NEWGITTOKEN-"q"-\\b-1198'

SOURCES = {
    "git": {"kind": "git", "name": "repo-a", "config": {"url": "https://git.example.com/a.git"},
            "secret": {"token": S_GIT}},
    "jira": {"kind": "jira", "name": "jira-main",
             "config": {"base_url": "https://x.atlassian.net", "projects": ["A"]},
             "secret": {"email": S_JIRA_MAIL, "api_token": S_JIRA_TOK}},
    "confluence": {"kind": "confluence", "name": "wiki",
                   "config": {"base_url": "https://x.atlassian.net", "spaces": ["ENG"]},
                   "secret": {"email": S_WIKI_MAIL, "api_token": S_WIKI_TOK}},
    "s3": {"kind": "s3", "name": "docs", "config": {"bucket": "b"},
           "secret": {"access_key_id": S_S3_KEY, "secret_access_key": S_S3_SEC}},
}


def forbidden_forms() -> set[str]:
    plain = [S_GIT, S_JIRA_MAIL, S_JIRA_TOK, S_WIKI_MAIL, S_WIKI_TOK, S_S3_KEY, S_S3_SEC, NEW_GIT]
    forms = set()
    for v in plain:
        forms |= {v, json.dumps(v)[1:-1], json.dumps(v, ensure_ascii=False)[1:-1],
                  base64.b64encode(v.encode()).decode()}
    for pair in (f"{S_JIRA_MAIL}:{S_JIRA_TOK}", f"{S_WIKI_MAIL}:{S_WIKI_TOK}",
                 f"x-access-token:{S_GIT}", f"x-access-token:{NEW_GIT}"):
        forms.add(base64.b64encode(pair.encode()).decode())
    return forms


class Fake:
    """HTTP fetch that fails with a message that echoes the secrets."""
    def __call__(self, url):
        raise RuntimeError(f"GET {url} failed: HTTP 401 {S_JIRA_TOK} {S_WIKI_TOK}")


class FakeDagster:
    def __init__(self):
        self.launched = []
        self.status = "STARTED"
        self.logs = []
        self.launch_error = None
        self.partitions = set()

    def add_partition(self, key):
        self.partitions.add(key)

    def remove_partition(self, key):
        self.partitions.discard(key)

    def get_status(self, run_id):
        return self.status

    def get_logs(self, run_id, limit=200):
        return self.logs

    def terminate(self, run_id):
        pass

    def launch(self, job_name, tags=None, run_config=None):
        if self.launch_error:
            raise self.launch_error
        self.launched.append(job_name)
        return f"dagster-{len(self.launched)}"


def git_runner(args, env, timeout):
    """Fails like git behind a proxy that echoes the auth header: header (base64 of user:token) and decoded form."""
    header = env.get("GIT_CONFIG_VALUE_0", "")
    decoded = base64.b64decode(header.rsplit(" ", 1)[-1]).decode() if header else ""
    return 128, "", f"fatal: auth failed [{header}] [{decoded}] {decoded.split(':', 1)[-1]}"


def leaky_lines(*values):
    out = []
    for v in values:
        out += [f"plain {v} end", f"json {json.dumps(v)} end", f"b64 {base64.b64encode(v.encode()).decode()} end"]
    return out


@pytest.fixture
def env(monkeypatch):
    for k in ("KG_OIDC_ISSUER", "DATABASE_URL", "DAGSTER_WEBSERVER_URL", "KG_CONTROL_ALLOW_HTTP",
              "KG_CONTROL_ALLOW_PRIVATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())


@pytest.fixture
def engine(env):
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)


@pytest.fixture
def app(engine):
    a = create_app(auth=Authenticator(), engine=engine)
    a.state.dagster = FakeDagster()
    a.state.prober = probe.Prober(
        resolver=lambda h: [PUBLIC], fetch=Fake(),
        git_runner=git_runner,
        s3_client=lambda cfg, sec: type("C", (), {"head_bucket": lambda self, Bucket: (_ for _ in ()).throw(
            RuntimeError(f"denied {S_S3_KEY} {S_S3_SEC}"))})())
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def audit_items(client, limit=200):
    r = client.get(f"{BASE}/audit?limit={limit}")
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ---- audit route ----
def test_audit_empty_and_shape(client):
    assert client.get(f"{BASE}/audit").json() == {"items": []}
    client.post(f"{BASE}/sources", json=SOURCES["git"])
    item = audit_items(client)[0]
    assert set(item) == {"id", "at", "actor", "action", "target", "detail"}
    assert item["action"] == "source.create" and item["actor"] == "local" and item["target"].startswith("src_")
    assert item["id"].startswith("aud_") and item["at"]


def test_audit_limit_default_max_and_bad(client, engine):
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with engine.begin() as conn:
        for i in range(250):
            conn.execute(db.audit.insert().values(
                id=f"aud_{i:012d}", at=base + timedelta(seconds=i), actor="a", action="source.test", target=None))
    items = client.get(f"{BASE}/audit").json()["items"]
    assert len(items) == 50 and items[0]["id"] == "aud_000000000249" and items[1]["id"] == "aud_000000000248"
    assert len(client.get(f"{BASE}/audit?limit=500").json()["items"]) == 200
    assert len(client.get(f"{BASE}/audit?limit=3").json()["items"]) == 3
    for bad in ("abc", "0", "-1", ""):
        r = client.get(f"{BASE}/audit?limit={bad}")
        assert r.status_code == 400 and "error" in r.json()


def test_audit_needs_admin(monkeypatch, engine):
    issuer = "https://sso.example.com/realms/kg"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("KG_OIDC_ISSUER", issuer)
    auth = Authenticator.from_env(key_resolver=lambda tok: key.public_key())
    c = TestClient(create_app(auth=auth, engine=engine))

    def tok(groups):
        claims = {"iss": issuer, "aud": "kg-mcp", "exp": int(time.time()) + 300, "preferred_username": "bob", "groups": groups}
        return {"Authorization": "Bearer " + jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})}
    assert c.get(f"{BASE}/audit").status_code == 401
    assert c.get(f"{BASE}/audit", headers=tok(["staff"])).status_code == 403
    assert c.get(f"{BASE}/audit", headers=tok(["kg-admins"])).status_code == 200


def test_all_six_audit_actions_are_written(client):
    sid = client.post(f"{BASE}/sources", json=SOURCES["git"]).json()["id"]
    client.patch(f"{BASE}/sources/{sid}", json={"enabled": False})
    client.patch(f"{BASE}/sources/{sid}", json={"enabled": True})
    assert client.post(f"{BASE}/sources/{sid}/test").status_code == 200
    run = client.post(f"{BASE}/sources/{sid}/run").json()
    assert client.post(f"{BASE}/runs/{run['id']}/cancel").status_code == 200
    assert client.delete(f"{BASE}/sources/{sid}").status_code == 204
    items = audit_items(client)
    assert {i["action"] for i in items} == {
        "source.create", "source.update", "source.delete", "source.test", "run.start", "run.cancel"}
    assert items[0]["action"] == "source.delete"  # newest first
    assert all(i["actor"] == "local" for i in items)


# ---- API-wide secret leak ----
def test_no_secret_value_in_any_response_audit_or_log(client, app, engine, caplog):
    caplog.set_level(logging.DEBUG)
    bodies: list[str] = []

    def call(method, path, **kw):
        r = client.request(method, BASE + path, **kw)
        bodies.append(r.text)
        bodies.append(json.dumps(dict(r.headers)))
        return r

    ids = {}
    for kind, body in SOURCES.items():
        r = call("POST", "/sources", json=body)
        assert r.status_code == 201, r.text
        ids[kind] = r.json()["id"]
        assert r.json()["has_secret"] is True and "secret" not in r.json()
        # error paths that carry the secret in the request
        assert call("POST", "/sources", json=body).status_code == 409
        bad_key = {**body, "name": body["name"] + "-x",
                   "secret": {**body["secret"], "bogus": next(iter(body["secret"].values()))}}
        assert call("POST", "/sources", json=bad_key).status_code == 400
        bad_cfg = {**SOURCES[kind], "name": body["name"] + "-y", "config": {"nope": 1}}
        assert call("POST", "/sources", json=bad_cfg).status_code == 400
        assert call("POST", "/sources", json={**body, "name": body["name"] + "-z", "unknown": body["secret"]}).status_code == 400

    assert call("GET", "/sources").status_code == 200
    app.state.dagster.logs = []

    for kind, sid in ids.items():
        assert call("GET", f"/sources/{sid}").status_code == 200
        assert call("PATCH", f"/sources/{sid}", json={"schedule": "* * * * *"}).status_code == 200
        assert call("PATCH", f"/sources/{sid}", json={"bogus": S_GIT}).status_code == 400
        assert call("PATCH", f"/sources/{sid}", json={"secret": {"nope": S_GIT}}).status_code == 400
        assert call("POST", f"/sources/{sid}/test").status_code == 200

    # runs: detail with log lines that contain the stored secrets
    for kind in ("git", "jira", "confluence"):
        sid = ids[kind]
        r = call("POST", f"/sources/{sid}/run", json={"mode": "full"})
        assert r.status_code == 202, r.text
        run_id = r.json()["id"]
        assert call("POST", f"/sources/{sid}/run").status_code == 409
        with engine.connect() as conn:
            from kg_pipeline.control import repo, secrets
            stored = secrets.decrypt(repo.get_source(conn, sid).secret)
        app.state.dagster.logs = ["start"] + leaky_lines(*stored.values())
        d = call("GET", f"/runs/{run_id}")
        assert d.status_code == 200 and d.json()["log_tail"]
        assert call("GET", "/runs").status_code == 200
        assert call("GET", f"/runs?source_id={sid}&status=running").status_code == 200
        assert call("GET", "/runs?limit=zero").status_code == 400
        assert call("POST", f"/runs/{run_id}/cancel").status_code == 200
        assert call("POST", f"/runs/{run_id}/cancel").status_code == 409
        # launch failure whose message echoes the secret
        app.state.dagster.launch_error = dc.DagsterError(f"launch failed: {' '.join(stored.values())}")
        assert call("POST", f"/sources/{sid}/run").status_code == 502
        app.state.dagster.launch_error = dc.DagsterUnavailable("dagster is unreachable")
        assert call("POST", f"/sources/{sid}/run").status_code == 502
        app.state.dagster.launch_error = None
    assert call("POST", f"/sources/{ids['s3']}/run").status_code == 202
    assert call("GET", "/runs/run_000000000000").status_code == 404
    assert call("POST", "/runs/run_000000000000/cancel").status_code == 404
    assert call("POST", "/sources/src_000000000000/test").status_code == 404
    assert call("PATCH", "/sources/src_000000000000", json={"secret": {"token": S_GIT}}).status_code == 404

    # replace a secret, run again with logs that echo the new one, then clear it
    sid = ids["git"]
    with engine.begin() as conn:
        conn.execute(db.runs.update().values(status="succeeded"))
    r = call("PATCH", f"/sources/{sid}", json={"secret": {"token": NEW_GIT}})
    assert r.status_code == 200 and r.json()["secret_keys"] == ["token"]
    assert call("POST", f"/sources/{sid}/test").status_code == 200
    run_id = call("POST", f"/sources/{sid}/run").json()["id"]
    app.state.dagster.logs = leaky_lines(NEW_GIT)
    assert call("GET", f"/runs/{run_id}").status_code == 200
    assert call("PATCH", f"/sources/{sid}", json={"secret": {}}).json()["has_secret"] is False

    assert call("GET", "/audit").status_code == 200
    assert call("GET", "/audit?limit=bad").status_code == 400
    for sid in ids.values():
        assert call("DELETE", f"/sources/{sid}").status_code == 204
        assert call("DELETE", f"/sources/{sid}").status_code == 404
        assert call("GET", f"/sources/{sid}").status_code == 404

    # collect everything that was stored or logged
    with engine.connect() as conn:
        stored_text = json.dumps([[str(c) for c in row] for row in conn.execute(select(db.audit)).all()])
        stored_text += json.dumps([[str(c) for c in row] for row in conn.execute(select(db.runs)).all()])
    audit_text = json.dumps(audit_items(client))
    log_text = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''} {r.args}" for r in caplog.records)
    bodies += [stored_text, audit_text, log_text]

    forms = forbidden_forms()
    assert len(forms) > 20
    for form in forms:
        for text in bodies:
            assert form not in text, f"secret form {form!r} leaked"
    # the checks above ran against real output
    assert len(bodies) > 150
    assert {i["action"] for i in audit_items(client)} >= {
        "source.create", "source.update", "source.delete", "source.test", "run.start", "run.cancel"}
