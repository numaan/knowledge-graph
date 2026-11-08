"""Git assets on dynamic partitions: repo URL, branch and token come from the sources registry at run time."""
import sys
from contextlib import contextmanager
from pathlib import Path

import dagster as dg
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from kg_pipeline import definitions as d
from kg_pipeline import sources_registry as reg
from kg_pipeline.control import db, secrets


class FakeGit:
    repo_name = staticmethod(lambda u: u.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))

    def __init__(self):
        self.calls = []

    def mirror(self, url, archive, token=None):
        self.calls.append(("mirror", url, token))
        return Path("/mirror")

    def head(self, mirror_path, branch):
        self.calls.append(("head", branch))
        return "abc123"

    @contextmanager
    def checkout(self, mirror_path, ref):
        self.calls.append(("checkout", ref))
        yield Path("/work")


class FakeWriter:
    def write_delta(self, graph):
        return {"nodes": 1, "edges": 1, "nodes_written": 1, "edges_written": 1, "stale_nodes_deleted": 0}


class FakeGraph:
    def stats(self):
        return {"counters": {}, "edges": {}}


@pytest.fixture
def fakes(monkeypatch):
    for k in ("DATABASE_URL", "GIT_REPOSITORIES", "GIT_BRANCH", "GIT_TOKEN", "KG_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    g = FakeGit()
    graph_calls = []
    monkeypatch.setattr(d, "git", g)
    monkeypatch.setattr(d, "build_graph", lambda work, name, scip_mode, scip_cache: graph_calls.append(
        {"name": name, "scip_cache": scip_cache}) or FakeGraph())
    monkeypatch.setattr(d, "_writer", contextmanager(lambda: (yield FakeWriter())))
    monkeypatch.setattr(d.links, "sync", lambda w, cfg: {})
    monkeypatch.setattr(d.links, "load_config", lambda p: None)
    g.graph_calls = graph_calls
    return g


def run_snapshot(key, tags=None):
    return d.code_snapshot(dg.build_asset_context(partition_key=key, run_tags=tags or {}))


@pytest.fixture
def registry_db(monkeypatch, tmp_path):
    """File SQLite database behind DATABASE_URL; one git row repo-a with a secret token."""
    url = f"sqlite:///{tmp_path / 'c.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("KG_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine(url, poolclass=StaticPool, connect_args={"check_same_thread": False}, future=True)
    db.create_all(engine)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(db.sources).values(
            id="src_000000000001", kind="git", name="repo-a",
            config={"url": "https://git.example.com/org/repo-a.git", "branch": "dev"},
            secret=secrets.encrypt({"token": "tok-A"}), schedule=None, enabled=True, origin="ui",
            created_at=now, updated_at=now, created_by="t"))
    monkeypatch.setattr(db, "get_engine", lambda *a, **k: create_engine(
        url, poolclass=StaticPool, connect_args={"check_same_thread": False}, future=True))
    return engine


def test_registry_row_gives_url_branch_and_token(fakes, registry_db):
    run_snapshot("repo-a")
    assert ("mirror", "https://git.example.com/org/repo-a.git", "tok-A") in fakes.calls
    assert ("head", "dev") in fakes.calls


def test_unknown_partition_fails_with_env_message(fakes, registry_db):
    with pytest.raises(dg.Failure, match="GIT_REPOSITORIES"):
        run_snapshot("nope")


def test_env_path_without_database(fakes, monkeypatch):
    monkeypatch.setenv("GIT_REPOSITORIES", "https://h/x/repo-e.git")
    monkeypatch.setenv("GIT_BRANCH", "trunk")
    monkeypatch.setenv("GIT_TOKEN", "env-tok")
    run_snapshot("repo-e")
    assert ("mirror", "https://h/x/repo-e.git", "env-tok") in fakes.calls
    assert ("head", "trunk") in fakes.calls
    with pytest.raises(dg.Failure, match="GIT_REPOSITORIES"):
        run_snapshot("unknown")


def test_db_row_without_secret_falls_back_to_env_token(fakes, registry_db, monkeypatch):
    from sqlalchemy import update
    with registry_db.begin() as conn:
        conn.execute(update(db.sources).values(secret=None))
    monkeypatch.setenv("GIT_TOKEN", "env-tok")
    run_snapshot("repo-a")
    assert ("mirror", "https://git.example.com/org/repo-a.git", "env-tok") in fakes.calls


def test_full_mode_ignores_scip_cache(fakes, monkeypatch):
    monkeypatch.setenv("GIT_REPOSITORIES", "https://h/x/repo-e.git")
    run_snapshot("repo-e")
    run_snapshot("repo-e", {"kg/mode": "incremental"})
    run_snapshot("repo-e", {"kg/mode": "full"})
    caches = [c["scip_cache"] for c in fakes.graph_calls]
    assert caches[0] is not None and caches[1] is not None and caches[2] is None


def test_full_mode_reads_whole_history(fakes, monkeypatch):
    monkeypatch.setenv("GIT_REPOSITORIES", "https://h/x/repo-e.git")
    seen = []
    monkeypatch.setattr(d.history, "sync", lambda w, mp, name, full=False: seen.append(full) or {"commits": 0})
    for tags in ({}, {"kg/mode": "full"}):
        d.git_history(dg.build_asset_context(partition_key="repo-e", run_tags=tags))
    assert seen == [False, True]


def test_other_assets_resolve_partition_through_registry(fakes, monkeypatch):
    monkeypatch.setenv("GIT_REPOSITORIES", "https://h/x/repo-e.git")
    monkeypatch.setattr(d.history, "sync", lambda w, mp, name, full=False: {"commits": 0})
    d.git_history(dg.build_asset_context(partition_key="repo-e"))
    with pytest.raises(dg.Failure, match="GIT_REPOSITORIES"):
        d.git_history(dg.build_asset_context(partition_key="other"))


def test_definitions_import_opens_no_database():
    import subprocess
    code = ("import os; os.environ['DATABASE_URL']='postgresql://u:p@127.0.0.1:1/x'; "
            "import kg_pipeline.definitions as d; print(len(d.defs.resolve_all_asset_specs()))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


def test_git_job_and_partitions_def():
    from kg_pipeline.control import dagster_client as dc
    assert d.git_job.name == dc.GIT_JOB
    assert isinstance(d.repo_partitions, dg.DynamicPartitionsDefinition)
    assert d.repo_partitions.name == dc.PARTITIONS_DEF
    job = d.defs.get_job_def(dc.GIT_JOB)
    keys = {k.to_user_string() for k in job.asset_layer.executable_asset_keys}
    assert keys == {"code_snapshot", "git_history", "domain_model", "vector_index", "summaries"}
    assert job.partitions_def == d.repo_partitions


def test_sync_partitions_registers_env_and_seeded_repos(monkeypatch):
    for k in ("DATABASE_URL", "KG_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_REPOSITORIES", "https://h/x/repo-e.git,https://h/x/repo-f.git")
    inst = dg.DagsterInstance.ephemeral()
    assert d.sync_partitions(inst) == ["repo-e", "repo-f"]
    assert d.sync_partitions(inst) == []
    assert sorted(inst.get_dynamic_partitions("repos")) == ["repo-e", "repo-f"]


def test_sync_partitions_seeds_database(monkeypatch, tmp_path):
    url = f"sqlite:///{tmp_path / 's.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("GIT_REPOSITORIES", "https://h/x/repo-e.git")
    seeded = []
    monkeypatch.setattr(reg, "seed_from_env", lambda engine=None: seeded.append(1) or 1)
    monkeypatch.setattr(reg, "get_sources", lambda kind: [reg.Source("repo-e", "git", {"url": "u"})])
    inst = dg.DagsterInstance.ephemeral()
    d.sync_partitions(inst)
    assert seeded == [1]
    assert inst.get_dynamic_partitions("repos") == ["repo-e"]
