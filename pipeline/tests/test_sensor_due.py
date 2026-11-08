"""Sensor due_sources: scheduled launches of registry sources, cron check, run-status copy, unchanged defs."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import dagster as dg
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, insert, select
from sqlalchemy.pool import StaticPool

from kg_pipeline import definitions as d
from kg_pipeline import due_sources
from kg_pipeline.control import dagster_client as dc
from kg_pipeline.control import db

NOW = datetime(2026, 11, 8, 12, 30, 20, tzinfo=timezone.utc)
MINUTE = "202611081230"


# ---- cron check ----
@pytest.mark.parametrize("cron,when,expected", [
    ("* * * * *", NOW, True),
    ("30 12 * * *", NOW, True),
    ("31 12 * * *", NOW, False),
    ("*/15 * * * *", NOW, True),
    ("*/7 * * * *", NOW, False),
    ("0 * * * *", NOW, False),
    ("not a cron", NOW, False),
    ("* * * *", NOW, False),
])
def test_cron_is_due(cron, when, expected):
    assert due_sources.cron_is_due(cron, when) is expected


def test_cron_is_due_uses_utc_for_other_zones():
    plus2 = timezone(timedelta(hours=2))
    assert due_sources.cron_is_due("30 12 * * *", NOW.astimezone(plus2)) is True
    assert due_sources.cron_is_due("30 14 * * *", NOW.astimezone(plus2)) is False


# ---- database fixture ----
@pytest.fixture
def engine(monkeypatch, tmp_path):
    url = f"sqlite:///{tmp_path / 's.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())

    def make(*a, **k):
        return create_engine(url, poolclass=StaticPool, connect_args={"check_same_thread": False}, future=True)

    eng = make()
    db.create_all(eng)
    monkeypatch.setattr(db, "get_engine", make)
    return eng


def add_source(engine, sid, name, kind="git", schedule="* * * * *", enabled=True):
    cfg = {"git": {"url": f"https://h/{name}.git"}, "jira": {"base_url": "https://x", "projects": ["A"]},
           "confluence": {"base_url": "https://x", "spaces": ["E"]}, "s3": {"bucket": "b"}}[kind]
    with engine.begin() as conn:
        conn.execute(insert(db.sources).values(
            id=sid, kind=kind, name=name, config=cfg, secret=None, schedule=schedule, enabled=enabled, origin="ui",
            created_at=NOW, updated_at=NOW, created_by="t"))


def add_run(engine, sid, name, status, rid="run_aaaaaaaaaaaa", dagster_run_id=None):
    with engine.begin() as conn:
        conn.execute(insert(db.runs).values(
            id=rid, source_id=sid, source_name=name, kind="git", status=status, trigger="manual", mode="incremental",
            dagster_run_id=dagster_run_id))


def all_runs(engine):
    with engine.connect() as conn:
        return conn.execute(select(db.runs)).all()


# ---- requests ----
def test_only_enabled_due_source_without_active_run_is_requested(engine):
    add_source(engine, "src_000000000001", "due")
    add_source(engine, "src_000000000002", "off", enabled=False)
    add_source(engine, "src_000000000003", "busy")
    add_source(engine, "src_000000000004", "never", schedule=None)
    add_source(engine, "src_000000000005", "later", schedule="31 12 * * *")
    add_source(engine, "src_000000000006", "docs", kind="s3")
    add_run(engine, "src_000000000003", "busy", "running")

    reqs = due_sources.due_requests(NOW)

    assert [r.run_key for r in reqs] == [f"src_000000000001:{MINUTE}"]
    req = reqs[0]
    assert req.job_name == "git_job"
    assert req.partition_key == "due"
    rows = {r.source_name: r for r in all_runs(engine)}
    row = rows["due"]
    assert (row.trigger, row.status, row.requested_by, row.mode, row.kind) == (
        "schedule", "queued", "schedule", "incremental", "git")
    assert req.tags == {"kg/source": "due", "kg/source_id": "src_000000000001", "kg/run_id": row.id,
                        "kg/mode": "incremental", "dagster/partition": "due"}
    assert len(rows) == 2  # busy + due


def test_jira_and_confluence_use_their_jobs_without_partition(engine):
    add_source(engine, "src_000000000001", "jira-main", kind="jira")
    add_source(engine, "src_000000000002", "wiki", kind="confluence")
    reqs = {r.job_name: r for r in due_sources.due_requests(NOW)}
    assert set(reqs) == {"jira_job", "confluence_job"}
    for r in reqs.values():
        assert r.partition_key is None
        assert "dagster/partition" not in r.tags


def test_second_evaluation_in_same_minute_yields_nothing(engine):
    add_source(engine, "src_000000000001", "due")
    assert len(due_sources.due_requests(NOW)) == 1
    assert due_sources.due_requests(NOW + timedelta(seconds=30)) == []
    assert len(all_runs(engine)) == 1


def test_same_minute_after_quick_finish_is_not_launched_twice(engine):
    add_source(engine, "src_000000000001", "due")
    due_sources.due_requests(NOW)
    with engine.begin() as conn:
        conn.execute(db.runs.update().values(status="succeeded"))
    assert due_sources.due_requests(NOW + timedelta(seconds=30)) == []


def test_run_key_differs_per_minute_and_source(engine):
    add_source(engine, "src_000000000001", "a")
    add_source(engine, "src_000000000002", "b")
    k1 = [r.run_key for r in due_sources.due_requests(NOW)]
    with engine.begin() as conn:
        conn.execute(db.runs.update().values(status="succeeded"))
    k2 = [r.run_key for r in due_sources.due_requests(NOW + timedelta(minutes=1))]
    assert len(set(k1 + k2)) == 4


def test_tags_equal_those_of_post_run(engine):
    tags = dc.launch_tags("git", "n", "src_1", "run_1", "full")
    assert tags == {"kg/source": "n", "kg/source_id": "src_1", "kg/run_id": "run_1", "kg/mode": "full",
                    "dagster/partition": "n"}
    assert due_sources.launch_tags("git", "n", "src_1", "run_1", "full") == tags
    assert due_sources.launch_tags("jira", "n", "src_1", "run_1", "incremental") == dc.launch_tags(
        "jira", "n", "src_1", "run_1", "incremental")
    assert due_sources.JOB_BY_KIND == dc.JOB_BY_KIND


# ---- the sensor ----
def test_sensor_skips_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    res = d.due_sources(dg.build_sensor_context())
    assert isinstance(res, dg.SkipReason)


def test_sensor_yields_request_for_due_source(engine, monkeypatch):
    monkeypatch.setattr(d, "_now", lambda: NOW)
    add_source(engine, "src_000000000001", "due")
    res = list(d.due_sources(dg.build_sensor_context(instance=dg.DagsterInstance.ephemeral())))
    assert [r.run_key for r in res] == [f"src_000000000001:{MINUTE}"]


# ---- copy of the Dagster run status ----
class FakeInstance:
    def __init__(self, runs, events=None):
        self._runs = runs
        self._events = events or []

    def get_runs(self, filters=None, limit=None, **kw):
        wanted = filters.tags["kg/run_id"]
        return [r for r in self._runs if r.tags.get("kg/run_id") == wanted][:limit]

    def all_logs(self, run_id, of_type=None):
        return self._events


def drun(rid, status, tag):
    return SimpleNamespace(run_id=rid, status=dg.DagsterRunStatus[status], tags={"kg/run_id": tag})


def mat(key, **meta):
    return SimpleNamespace(dagster_event=SimpleNamespace(event_specific_data=SimpleNamespace(
        materialization=SimpleNamespace(asset_key=dg.AssetKey(key), metadata={
            k: dg.MetadataValue.json(v) if isinstance(v, dict) else dg.MetadataValue.int(v) if isinstance(v, int)
            else dg.MetadataValue.text(v) for k, v in meta.items()}))))


def test_sync_sets_dagster_run_id_status_and_stats(engine):
    add_source(engine, "src_000000000001", "due")
    add_run(engine, "src_000000000001", "due", "queued", rid="run_aaaaaaaaaaaa")
    inst = FakeInstance([drun("dag-1", "STARTED", "run_aaaaaaaaaaaa")])
    assert due_sources.sync_runs(inst) == 1
    row = all_runs(engine)[0]
    assert (row.dagster_run_id, row.status) == ("dag-1", "running")
    assert row.started_at is not None and row.ended_at is None

    inst = FakeInstance([drun("dag-1", "SUCCESS", "run_aaaaaaaaaaaa")],
                        [mat(["code_snapshot"], nodes=5, commit="abc"), mat(["x"], data={"a": 1})])
    due_sources.sync_runs(inst)
    row = all_runs(engine)[0]
    assert row.status == "succeeded" and row.ended_at is not None
    assert row.stats == {"code_snapshot": {"nodes": 5, "commit": "abc"}, "x": {"data": {"a": 1}}}


def test_sync_ignores_unknown_and_final_runs(engine):
    add_run(engine, None, "gone", "succeeded", rid="run_bbbbbbbbbbbb")
    add_run(engine, None, "nofind", "queued", rid="run_cccccccccccc")
    assert due_sources.sync_runs(FakeInstance([])) == 0
    assert {r.id: r.status for r in all_runs(engine)} == {"run_bbbbbbbbbbbb": "succeeded", "run_cccccccccccc": "queued"}


# ---- FIX 2: a queued row that never reached Dagster ----
def add_queued(engine, rid, created_at, dagster_run_id=None):
    with engine.begin() as conn:
        conn.execute(insert(db.runs).values(
            id=rid, source_id=None, source_name="x", kind="git", status="queued", trigger="schedule",
            mode="incremental", requested_by="schedule", created_at=created_at, dagster_run_id=dagster_run_id))


def test_queued_row_without_dagster_run_fails_after_timeout(engine):
    add_queued(engine, "run_old000000000", NOW - timedelta(minutes=11))
    add_queued(engine, "run_new000000000", NOW - timedelta(minutes=9))
    add_queued(engine, "run_null00000000", None)
    add_queued(engine, "run_withid0000000", NOW - timedelta(hours=1), dagster_run_id="dag-9")
    assert due_sources.sync_runs(FakeInstance([]), now=NOW) == 1
    rows = {r.id: r for r in all_runs(engine)}
    old = rows["run_old000000000"]
    assert old.status == "failed" and old.error == "launch did not reach Dagster" and old.ended_at is not None
    assert [rows[k].status for k in ("run_new000000000", "run_null00000000", "run_withid0000000")] == ["queued"] * 3


def test_queue_timeout_comes_from_env(engine, monkeypatch):
    monkeypatch.setenv("KG_CONTROL_QUEUE_TIMEOUT_S", "30")
    add_queued(engine, "run_old000000000", NOW - timedelta(seconds=31))
    assert due_sources.sync_runs(FakeInstance([]), now=NOW) == 1


def test_expired_source_can_be_scheduled_again(engine):
    add_source(engine, "src_000000000001", "due")
    due_sources.due_requests(NOW - timedelta(minutes=20))
    assert all_runs(engine)[0].created_at is not None
    assert due_sources.due_requests(NOW) == []  # still blocked by the stuck queued row
    due_sources.sync_runs(FakeInstance([]), now=NOW)
    assert len(due_sources.due_requests(NOW)) == 1


def test_create_all_adds_created_at_to_an_old_runs_table(tmp_path):
    from sqlalchemy import inspect, text
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}", future=True)
    db.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE runs DROP COLUMN created_at"))
    db.create_all(eng)
    db.create_all(eng)
    assert "created_at" in {c["name"] for c in inspect(eng).get_columns("runs")}


# ---- definitions unchanged ----
def test_existing_schedules_and_asset_keys_unchanged():
    repo = d.defs.get_repository_def()
    assert {"hourly_code", "sources_every_15_min", "daily_reconcile"} <= {s.name for s in repo.schedule_defs}
    sched = {s.name: s.cron_schedule for s in repo.schedule_defs}
    assert sched["hourly_code"] == "0 * * * *"
    assert sched["sources_every_15_min"] == "*/15 * * * *"
    keys = {k.to_user_string() for k in repo.assets_defs_by_key}
    assert {"code_snapshot", "git_history", "domain_model", "vector_index", "summaries", "service_catalog",
            "jira_issues", "confluence_pages", "source_permissions", "github_prs", "source_vectors",
            "doc_freshness", "people"} <= keys
    sensor = repo.get_sensor_def("due_sources")
    assert sensor.minimum_interval_seconds == 60


def test_sensor_skips_a_source_with_a_blocked_host(engine, monkeypatch, caplog):
    from kg_pipeline.control import probe
    add_source(engine, "src_000000000001", "internal")
    add_source(engine, "src_000000000002", "public")
    with engine.begin() as conn:
        conn.execute(db.sources.update().where(db.sources.c.id == "src_000000000001").values(
            config={"url": "https://internal.example/a.git"}))
        conn.execute(db.sources.update().where(db.sources.c.id == "src_000000000002").values(
            config={"url": "https://public.example/b.git"}))
    monkeypatch.setattr(probe, "resolve", lambda host: ["10.0.0.5"] if host.startswith("internal") else ["1.1.1.1"])
    with caplog.at_level("WARNING"):
        reqs = due_sources.due_requests(NOW)
    assert [r.run_key for r in reqs] == [f"src_000000000002:{MINUTE}"]
    assert "internal" in caplog.text and "SSRF guard" in caplog.text
    assert {r.source_name for r in all_runs(engine)} == {"public"}
