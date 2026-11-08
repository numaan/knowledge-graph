"""Contract fixtures: real JSON of the control API, written to web/tests/fixtures/control/*.json.

The web tests (web/tests/control-contract.test.ts) check these files against web/src/lib/control-types.ts.
The test compares the fresh JSON with the committed files and fails on a difference.
Regenerate with: KG_UPDATE_FIXTURES=1 python -m pytest pipeline/tests/test_control_contract.py
"""
import itertools
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from kg_pipeline.control import audit as audit_mod
from kg_pipeline.control import db, routes_runs, routes_sources
from kg_pipeline.control.app import create_app
from kg_pipeline.control.auth import Authenticator

FIXTURES = Path(__file__).resolve().parents[2] / "web" / "tests" / "fixtures" / "control"
SECRET = "tok-SECRET-123456"
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


class FakeDagster:
    def __init__(self):
        self.n = 0
        self.status = "STARTED"
        self.logs = []
        self.stats = None

    def add_partition(self, key):
        pass

    def remove_partition(self, key):
        pass

    def launch(self, job_name, tags=None, run_config=None):
        self.n += 1
        return f"3f2a9c1e-0000-4000-8000-00000000000{self.n}"

    def get_status(self, run_id):
        return self.status

    def get_logs(self, run_id, limit=200):
        return self.logs

    def get_stats(self, run_id):
        return self.stats

    def terminate(self, run_id):
        pass


class FakeProber:
    def __init__(self):
        self.result = {"ok": True, "message": "branch main found on git.example.com",
                       "details": {"host": "git.example.com", "branch": "main"}}

    def test(self, kind, config, secret):
        return self.result


class Normaliser:
    """Replace every distinct timestamp by a fixed one, in order of first appearance."""

    def __init__(self):
        self.seen: dict[str, str] = {}

    def __call__(self, value):
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v) for v in value]
        if isinstance(value, str) and ISO.match(value):
            if value not in self.seen:
                self.seen[value] = f"2026-01-01T10:00:{len(self.seen):02d}+00:00"
            return self.seen[value]
        return value


def test_contract_fixtures(monkeypatch):
    for k in ("KG_OIDC_ISSUER", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DAGSTER_WEBSERVER_URL", "https://dagster.example.com")
    counters: dict[str, itertools.count] = defaultdict(lambda: itertools.count(1))

    def fixed_id(prefix):
        return f"{prefix}_{next(counters[prefix]):012x}"

    for mod in (routes_sources, routes_runs, audit_mod):
        if hasattr(mod, "new_id"):
            monkeypatch.setattr(mod, "new_id", fixed_id)
    monkeypatch.setattr(db, "new_id", fixed_id)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    app = create_app(auth=Authenticator(), engine=engine)
    dagster = app.state.dagster = FakeDagster()
    prober = app.state.prober = FakeProber()
    client = TestClient(app)
    out: dict[str, object] = {}
    norm = Normaliser()

    def ok(name, resp, code):
        assert resp.status_code == code, resp.text
        assert SECRET not in resp.text
        out[name] = resp.json()
        return resp.json()

    out["me"] = client.get("/api/v1/me").json()
    created = ok("source_create", client.post("/api/v1/sources", json={
        "kind": "git", "name": "repo-a", "config": {"url": "https://git.example.com/a.git", "branch": "main"},
        "secret": {"token": SECRET}, "schedule": "0 3 * * *"}), 201)
    sid = created["id"]
    ok("source_create_jira", client.post("/api/v1/sources", json={
        "kind": "jira", "name": "jira-main", "config": {"base_url": "https://x.atlassian.net", "projects": ["A"]}}), 201)
    ok("source_list", client.get("/api/v1/sources"), 200)
    ok("source_test", client.post(f"/api/v1/sources/{sid}/test"), 200)
    prober.result = {"ok": False, "message": "git ls-remote failed: authentication failed", "details": {"host": "git.example.com", "branch": "main"}}
    ok("source_test_failed", client.post(f"/api/v1/sources/{sid}/test"), 200)
    ok("source_patch", client.patch(f"/api/v1/sources/{sid}", json={
        "enabled": True, "secret": {"token": SECRET + "-2"}}), 200)

    ok("run_start", client.post(f"/api/v1/sources/{sid}/run", json={"mode": "full"}), 202)
    run_id = out["run_start"]["id"]
    ok("source_get", client.get(f"/api/v1/sources/{sid}"), 200)
    ok("run_running", client.get(f"/api/v1/runs/{run_id}"), 200)
    dagster.status = "SUCCESS"
    dagster.logs = ["2026-01-01 10:00:00 - RUN_START - Started execution of run", "2026-01-01 10:00:09 - RUN_SUCCESS - Finished"]
    dagster.stats = {"code_snapshot": {"nodes": 5, "commit": "abc1234"}}
    ok("run_detail", client.get(f"/api/v1/runs/{run_id}"), 200)
    dagster.status = "STARTED"
    jid = out["source_create_jira"]["id"]
    second = ok("run_start_queued", client.post(f"/api/v1/sources/{jid}/run"), 202)
    ok("run_cancel", client.post(f"/api/v1/runs/{second['id']}/cancel"), 200)
    ok("runs_list", client.get("/api/v1/runs"), 200)
    ok("audit", client.get("/api/v1/audit"), 200)

    err = client.get("/api/v1/runs/run_missing")
    assert err.status_code == 404
    out["error"] = err.json()
    assert client.delete(f"/api/v1/sources/{jid}").status_code == 204

    FIXTURES.mkdir(parents=True, exist_ok=True)
    update = os.environ.get("KG_UPDATE_FIXTURES") == "1"
    for name, value in out.items():
        value = norm(value)
        text = json.dumps(value, indent=2, sort_keys=True) + "\n"
        assert SECRET not in text
        path = FIXTURES / f"{name}.json"
        if update or not path.exists():
            path.write_text(text, encoding="utf-8", newline="\n")
        else:
            assert path.read_text(encoding="utf-8") == text, f"{path.name} drifted; rerun with KG_UPDATE_FIXTURES=1"
    # the fixture set must be exactly what this test writes
    assert {p.stem for p in FIXTURES.glob("*.json")} == set(out)
