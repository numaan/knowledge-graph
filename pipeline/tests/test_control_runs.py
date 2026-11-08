"""Run launch: Dagster client wrapper, status mapping, POST /sources/{id}/run with a fake client."""
import pytest
import requests
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from kg_pipeline.control import dagster_client as dc
from kg_pipeline.control import db
from kg_pipeline.control.app import create_app
from kg_pipeline.control.auth import Authenticator

SECRET = "tok-SECRET-123456"
GIT = {"kind": "git", "name": "repo-a", "config": {"url": "https://git.example.com/a.git"}, "secret": {"token": SECRET}}
JIRA = {"kind": "jira", "name": "jira-main", "config": {"base_url": "https://x.atlassian.net", "projects": ["A"]},
        "secret": {"email": "a@b.c", "api_token": SECRET}}
WIKI = {"kind": "confluence", "name": "wiki", "config": {"base_url": "https://x.atlassian.net", "spaces": ["ENG"]}}
S3 = {"kind": "s3", "name": "docs", "config": {"bucket": "b"}, "secret": {"access_key_id": "AKIA1", "secret_access_key": SECRET}}


# ---- status mapping ----
@pytest.mark.parametrize("state,expected", [
    ("QUEUED", "queued"), ("NOT_STARTED", "queued"), ("STARTING", "running"), ("MANAGED", "running"),
    ("STARTED", "running"), ("SUCCESS", "succeeded"), ("FAILURE", "failed"), ("CANCELING", "canceled"),
    ("CANCELED", "canceled"), ("DagsterRunStatus.SUCCESS", "succeeded"), ("something-new", "running"),
])
def test_map_status(state, expected):
    assert dc.map_status(state) == expected


# ---- wrapper over the GraphQL client ----
class FakeGql:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result
        self.error = error

    def _record(self, name, *a, **kw):
        self.calls.append((name, a, kw))
        if self.error:
            raise self.error
        return self.result

    def submit_job_execution(self, *a, **kw):
        return self._record("submit", *a, **kw)

    def get_run_status(self, *a, **kw):
        return self._record("status", *a, **kw)

    def terminate_run(self, *a, **kw):
        return self._record("terminate", *a, **kw)

    def _execute(self, *a, **kw):
        return self._record("execute", *a, **kw)


def test_launch_passes_job_tags_and_config():
    g = FakeGql(result="rid-1")
    assert dc.DagsterClient(g).launch("git_job", {"a": "b"}, {"x": 1}) == "rid-1"
    assert g.calls == [("submit", ("git_job",), {"run_config": {"x": 1}, "tags": {"a": "b"}})]


def test_status_returns_state_name():
    from dagster import DagsterRunStatus
    assert dc.DagsterClient(FakeGql(result=DagsterRunStatus.STARTED)).get_status("r") == "STARTED"


def test_terminate_calls_client():
    g = FakeGql()
    dc.DagsterClient(g).terminate("r1")
    assert g.calls == [("terminate", ("r1",), {})]


def test_connection_error_is_unavailable():
    from dagster_graphql import DagsterGraphQLClientError
    err = DagsterGraphQLClientError("Error when connecting")
    err.__cause__ = requests.exceptions.ConnectionError("boom")
    with pytest.raises(dc.DagsterUnavailable):
        dc.DagsterClient(FakeGql(error=err)).launch("git_job")


def test_other_error_is_dagster_error_without_variables():
    from dagster_graphql import DagsterGraphQLClientError
    err = DagsterGraphQLClientError("JobNotFoundError", "no job with variables {'token': 'SECRETVALUE'}")
    with pytest.raises(dc.DagsterError) as ei:
        dc.DagsterClient(FakeGql(error=err)).launch("git_job")
    assert not isinstance(ei.value, dc.DagsterUnavailable)
    assert "SECRETVALUE" not in str(ei.value)


def test_add_and_remove_partition_use_dynamic_partition_mutations():
    g = FakeGql(result={"addDynamicPartition": {"__typename": "AddDynamicPartitionSuccess"}})
    dc.DagsterClient(g, "loc", "repo").add_partition("repo-a")
    name, (query, variables), _ = g.calls[0]
    assert "addDynamicPartition" in query
    assert variables == {"sel": {"repositoryLocationName": "loc", "repositoryName": "repo"},
                         "def": "repos", "key": "repo-a"}
    g = FakeGql(result={"deleteDynamicPartitions": {"__typename": "DeleteDynamicPartitionsSuccess"}})
    dc.DagsterClient(g, "loc", "repo").remove_partition("repo-a")
    assert "deleteDynamicPartitions" in g.calls[0][1][0]
    assert g.calls[0][1][1]["keys"] == ["repo-a"]


def test_add_partition_duplicate_is_ok_and_error_raises():
    ok = FakeGql(result={"addDynamicPartition": {"__typename": "DuplicateDynamicPartitionError"}})
    dc.DagsterClient(ok, "l", "r").add_partition("k")
    bad = FakeGql(result={"addDynamicPartition": {"__typename": "PythonError"}})
    with pytest.raises(dc.DagsterError):
        dc.DagsterClient(bad, "l", "r").add_partition("k")


def test_get_logs_keeps_last_200_over_pages():
    pages = [
        {"logsForRun": {"__typename": "EventConnection", "cursor": "c1", "hasMore": True,
                        "events": [{"message": f"m{i}", "timestamp": "1", "level": "INFO"} for i in range(150)]}},
        {"logsForRun": {"__typename": "EventConnection", "cursor": "c2", "hasMore": False,
                        "events": [{"message": f"n{i}", "timestamp": "2", "level": "INFO"} for i in range(150)]}},
    ]

    class Paged(FakeGql):
        def _execute(self, query, variables=None):
            self.calls.append(variables)
            return pages[len(self.calls) - 1]

    g = Paged()
    lines = dc.DagsterClient(g).get_logs("r1")
    assert len(lines) == 200 and lines[-1].endswith("n149") and lines[0].endswith("m100")
    assert g.calls[1]["cursor"] == "c1"


# ---- route ----
class FakeDagster:
    def __init__(self, launch_error=None):
        self.launched = []
        self.terminated = []
        self.launch_error = launch_error
        self.status = "STARTED"
        self.logs = []
        self.status_error = None
        self.logs_error = None

    def get_status(self, run_id):
        if self.status_error:
            raise self.status_error
        return self.status

    def get_logs(self, run_id, limit=200):
        if self.logs_error:
            raise self.logs_error
        return self.logs

    def terminate(self, run_id):
        self.terminated.append(run_id)

    def launch(self, job_name, tags=None, run_config=None):
        if self.launch_error:
            raise self.launch_error
        self.launched.append({"job": job_name, "tags": tags or {}, "config": run_config})
        return f"dagster-{len(self.launched)}"


@pytest.fixture
def env(monkeypatch):
    for k in ("KG_OIDC_ISSUER", "DATABASE_URL", "DAGSTER_WEBSERVER_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())


@pytest.fixture
def engine(env):
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)


@pytest.fixture
def app(engine):
    a = create_app(auth=Authenticator(), engine=engine)
    a.state.dagster = FakeDagster()
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def make(client, body):
    r = client.post("/api/v1/sources", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_run_git_returns_202_with_run(client, app, monkeypatch):
    monkeypatch.setenv("DAGSTER_WEBSERVER_URL", "https://dagster.example.com/")
    sid = make(client, GIT)
    r = client.post(f"/api/v1/sources/{sid}/run", json={})
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["status"] == "queued" and run["mode"] == "incremental" and run["trigger"] == "manual"
    assert run["source_id"] == sid and run["source_name"] == "repo-a" and run["kind"] == "git"
    assert run["requested_by"] == "local" and run["id"].startswith("run_")
    assert run["dagster_run_id"] == "dagster-1"
    assert run["dagster_url"] == "https://dagster.example.com/runs/dagster-1"
    launched = app.state.dagster.launched[0]
    assert launched["job"] == "git_job"
    assert launched["tags"]["dagster/partition"] == "repo-a"
    assert launched["tags"]["kg/mode"] == "incremental"
    assert SECRET not in r.text


def test_dagster_url_null_when_env_unset(client):
    sid = make(client, JIRA)
    assert client.post(f"/api/v1/sources/{sid}/run").json()["dagster_url"] is None


def test_body_is_optional_and_mode_full_sets_tag(client, app):
    sid = make(client, WIKI)
    r = client.post(f"/api/v1/sources/{sid}/run", json={"mode": "full"})
    assert r.status_code == 202 and r.json()["mode"] == "full"
    launched = app.state.dagster.launched[0]
    assert launched["job"] == "confluence_job" and launched["tags"]["kg/mode"] == "full"
    assert "dagster/partition" not in launched["tags"]


def test_jira_job_name(client, app):
    client.post(f"/api/v1/sources/{make(client, JIRA)}/run")
    assert app.state.dagster.launched[0]["job"] == "jira_job"


def test_bad_mode_is_400(client):
    sid = make(client, JIRA)
    assert client.post(f"/api/v1/sources/{sid}/run", json={"mode": "bogus"}).status_code == 400
    assert client.post(f"/api/v1/sources/{sid}/run", json={"other": 1}).status_code == 400


def test_active_run_gives_409_and_no_second_launch(client, app):
    sid = make(client, GIT)
    assert client.post(f"/api/v1/sources/{sid}/run").status_code == 202
    r = client.post(f"/api/v1/sources/{sid}/run")
    assert r.status_code == 409 and "error" in r.json()
    assert len(app.state.dagster.launched) == 1


def test_finished_run_allows_new_run(client, engine):
    sid = make(client, GIT)
    client.post(f"/api/v1/sources/{sid}/run")
    with engine.begin() as conn:
        conn.execute(db.runs.update().values(status="succeeded"))
    assert client.post(f"/api/v1/sources/{sid}/run").status_code == 202


def test_disabled_source_gives_409(client, app):
    sid = make(client, GIT)
    client.patch(f"/api/v1/sources/{sid}", json={"enabled": False})
    assert client.post(f"/api/v1/sources/{sid}/run").status_code == 409
    assert app.state.dagster.launched == []


def test_s3_gives_501(client, app):
    sid = make(client, S3)
    r = client.post(f"/api/v1/sources/{sid}/run")
    assert r.status_code == 501 and "s3_documents asset not available yet" in r.json()["error"]
    assert app.state.dagster.launched == []


def test_unknown_source_is_404(client):
    assert client.post("/api/v1/sources/src_000000000000/run").status_code == 404


def test_dagster_unreachable_gives_502_and_run_failed(client, app, engine):
    app.state.dagster = FakeDagster(launch_error=dc.DagsterUnavailable("dagster is unreachable"))
    sid = make(client, GIT)
    r = client.post(f"/api/v1/sources/{sid}/run")
    assert r.status_code == 502 and SECRET not in r.text
    with engine.connect() as conn:
        row = conn.execute(select(db.runs)).one()
    assert row.status == "failed" and row.error and SECRET not in row.error and row.dagster_run_id is None
    # a failed run is not active
    app.state.dagster = FakeDagster()
    assert client.post(f"/api/v1/sources/{sid}/run").status_code == 202


def test_run_writes_audit(client, engine):
    sid = make(client, GIT)
    run = client.post(f"/api/v1/sources/{sid}/run", json={"mode": "full"}).json()
    with engine.connect() as conn:
        rows = conn.execute(select(db.audit).where(db.audit.c.action == "run.start")).all()
    assert len(rows) == 1 and rows[0].actor == "local" and rows[0].target == run["id"]
    assert rows[0].detail["mode"] == "full" and SECRET not in str(rows[0].detail)


def test_source_last_run_shows_the_run(client):
    sid = make(client, GIT)
    run = client.post(f"/api/v1/sources/{sid}/run").json()
    assert client.get(f"/api/v1/sources/{sid}").json()["last_run"]["id"] == run["id"]


# ---- T6: list, detail, cancel ----
def start_run(client, sid, **kw):
    return client.post(f"/api/v1/sources/{sid}/run", json=kw).json()


def test_runs_routes_exist(client):
    assert client.get("/api/v1/runs").json() == {"items": []}
    assert client.get("/api/v1/runs/run_000000000000").status_code == 404
    assert client.post("/api/v1/runs/run_000000000000/cancel").status_code == 404


def test_list_newest_first_filters_and_limits(client, engine):
    from datetime import datetime, timedelta, timezone
    a, b = make(client, GIT), make(client, JIRA)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with engine.begin() as conn:
        for i in range(60):
            conn.execute(db.runs.insert().values(
                id=f"run_{i:012d}", source_id=a if i % 2 == 0 else b, source_name="x", kind="git",
                status="succeeded" if i < 50 else "failed", trigger="manual", mode="incremental",
                started_at=base + timedelta(minutes=i)))
    items = client.get("/api/v1/runs").json()["items"]
    assert len(items) == 50 and items[0]["id"] == "run_000000000059" and items[1]["id"] == "run_000000000058"
    assert len(client.get("/api/v1/runs?limit=500").json()["items"]) == 60
    assert len(client.get("/api/v1/runs?limit=3").json()["items"]) == 3
    assert {i["source_id"] for i in client.get(f"/api/v1/runs?source_id={a}").json()["items"]} == {a}
    assert len(client.get("/api/v1/runs?status=failed").json()["items"]) == 10
    for bad in ("abc", "0", "-1"):
        assert client.get(f"/api/v1/runs?limit={bad}").status_code == 400


def test_detail_refresh_sets_started_then_final(client, app):
    run = start_run(client, make(client, GIT))
    app.state.dagster.status = "STARTED"
    d = client.get(f"/api/v1/runs/{run['id']}").json()
    assert d["status"] == "running" and d["started_at"] and d["ended_at"] is None and d["log_tail"] == []
    first = d["started_at"]
    app.state.dagster.status = "SUCCESS"
    d = client.get(f"/api/v1/runs/{run['id']}").json()
    assert d["status"] == "succeeded" and d["started_at"] == first and d["ended_at"]
    assert d["stats"] is None


def test_detail_copies_stats_when_client_can_read(client, app):
    app.state.dagster.get_stats = lambda rid: {"chunks": 3}
    run = start_run(client, make(client, GIT))
    app.state.dagster.status = "FAILURE"
    d = client.get(f"/api/v1/runs/{run['id']}").json()
    assert d["status"] == "failed" and d["stats"] == {"chunks": 3}


def test_list_refreshes_active_runs(client, app):
    run = start_run(client, make(client, GIT))
    app.state.dagster.status = "SUCCESS"
    assert client.get("/api/v1/runs").json()["items"][0]["status"] == "succeeded"
    assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] == "succeeded"


def test_log_tail_capped_and_scrubbed(client, app):
    run = start_run(client, make(client, GIT))
    app.state.dagster.logs = [f"line {i}" for i in range(300)] + [f"auth {SECRET} used"]
    r = client.get(f"/api/v1/runs/{run['id']}")
    tail = r.json()["log_tail"]
    assert len(tail) == 200 and tail[-1] == "auth *** used" and SECRET not in r.text


def test_dagster_unreachable_returns_stored_run(client, app):
    run = start_run(client, make(client, GIT))
    app.state.dagster.status_error = dc.DagsterUnavailable("dagster is unreachable")
    app.state.dagster.logs_error = dc.DagsterUnavailable("dagster is unreachable")
    r = client.get(f"/api/v1/runs/{run['id']}")
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["log_tail"] == []
    assert client.get("/api/v1/runs").status_code == 200


def test_cancel_terminates_audits_and_frees_source(client, app, engine):
    sid = make(client, GIT)
    run = start_run(client, sid)
    r = client.post(f"/api/v1/runs/{run['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "canceled" and r.json()["ended_at"]
    assert app.state.dagster.terminated == ["dagster-1"]
    with engine.connect() as conn:
        rows = conn.execute(select(db.audit).where(db.audit.c.action == "run.cancel")).all()
    assert len(rows) == 1 and rows[0].target == run["id"] and rows[0].actor == "local"
    assert client.post(f"/api/v1/runs/{run['id']}/cancel").status_code == 409
    assert client.post(f"/api/v1/sources/{sid}/run").status_code == 202


def test_cancel_dagster_down_gives_502_and_keeps_status(client, app):
    run = start_run(client, make(client, GIT))

    def boom(rid):
        raise dc.DagsterUnavailable("dagster is unreachable")
    app.state.dagster.terminate = boom
    assert client.post(f"/api/v1/runs/{run['id']}/cancel").status_code == 502
    assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] in ("queued", "running")
