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

    def mirror(self, url, archive, token=None, env_fallback=True, restricted=False):
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


def test_ui_row_without_secret_runs_without_credentials(fakes, registry_db, monkeypatch):
    from sqlalchemy import update
    with registry_db.begin() as conn:
        conn.execute(update(db.sources).values(secret=None))
    monkeypatch.setenv("GIT_TOKEN", "env-tok")
    run_snapshot("repo-a")
    assert ("mirror", "https://git.example.com/org/repo-a.git", None) in fakes.calls
    assert not any("env-tok" in str(c) for c in fakes.calls)


def test_env_row_without_secret_falls_back_to_env_token(fakes, registry_db, monkeypatch):
    from sqlalchemy import update
    with registry_db.begin() as conn:
        conn.execute(update(db.sources).values(secret=None, origin="env"))
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


# ---- T10: Jira and Confluence from the registry -------------------------------------------------------------------

class FakeAtlassian:
    made = []

    def __init__(self, base_url=None, email=None, token=None, fetch=None, prefix="JIRA", use_env=True):
        self.base, self.email, self.token, self.prefix, self.use_env = base_url, email, token, prefix, use_env
        FakeAtlassian.made.append(self)


@pytest.fixture
def atl(monkeypatch):
    for k in ("DATABASE_URL", "KG_SECRET_KEY", "JIRA_PROJECTS", "CONFLUENCE_SPACES", "JIRA_BASE_URL", "JIRA_EMAIL",
              "JIRA_API_TOKEN", "CONFLUENCE_BASE_URL", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    FakeAtlassian.made = []
    calls = {"jira": [], "confluence": [], "perm_jira": [], "perm_conf": []}
    monkeypatch.setattr(d, "Atlassian", FakeAtlassian)
    monkeypatch.setattr(d, "_writer", contextmanager(lambda: (yield FakeWriter())))
    monkeypatch.setattr(d.jira, "sync", lambda w, api, project, jql=None, full=False, **kw: calls["jira"].append(
        (api.base, api.email, api.token, project, full)) or {"issues": 1})
    monkeypatch.setattr(d.documents, "sync_confluence", lambda w, api, space, full=False: calls["confluence"].append(
        (api.base, api.email, api.token, space, full)) or {"documents": 1})
    monkeypatch.setattr(d.permissions, "sync_jira", lambda w, api, p: calls["perm_jira"].append(
        (api.base, api.token, p)) or {"p": p})
    monkeypatch.setattr(d.permissions, "sync_confluence", lambda w, api, s: calls["perm_conf"].append(
        (api.base, api.token, s)) or {"s": s})
    return calls


def add_rows(engine, *rows, origin="ui"):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for i, (kind, name, config, secret, enabled) in enumerate(rows):
            conn.execute(insert(db.sources).values(
                id=f"src_0000000001{i:02d}", kind=kind, name=name, config=config,
                secret=secrets.encrypt(secret) if secret else None, schedule=None, enabled=enabled, origin=origin,
                created_at=now, updated_at=now, created_by="t"))


def ctx(tags=None):
    return dg.build_asset_context(run_tags=tags or {})


def test_jira_projects_and_credentials_from_registry(atl, registry_db):
    add_rows(registry_db,
             ("jira", "site1", {"base_url": "https://a.atlassian.net", "projects": ["A", "B"]},
              {"email": "e@x", "api_token": "tk"}, True),
             ("jira", "off", {"base_url": "https://z.atlassian.net", "projects": ["Z"]}, {"email": "e", "api_token": "z"}, False))
    res = d.jira_issues(ctx())
    assert atl["jira"] == [("https://a.atlassian.net", "e@x", "tk", "A", False),
                           ("https://a.atlassian.net", "e@x", "tk", "B", False)]
    assert "tk" not in str(res.metadata)


def test_confluence_spaces_from_registry(atl, registry_db):
    add_rows(registry_db, ("confluence", "wiki", {"base_url": "https://a.atlassian.net", "spaces": ["ENG"]},
                           {"email": "e@x", "api_token": "tk"}, True))
    d.confluence_pages(ctx())
    assert atl["confluence"] == [("https://a.atlassian.net", "e@x", "tk", "ENG", False)]
    assert FakeAtlassian.made[0].prefix == "CONFLUENCE"


def test_no_sources_keeps_skipped_message(atl, registry_db):
    assert d.jira_issues(ctx()).metadata["skipped"] == "JIRA_PROJECTS is empty"
    assert d.confluence_pages(ctx()).metadata["skipped"] == "CONFLUENCE_SPACES is empty"
    assert atl["jira"] == [] and atl["confluence"] == []


def test_env_row_without_secret_falls_back_to_env_credentials(atl, registry_db):
    add_rows(registry_db, ("jira", "site1", {"base_url": "https://a.atlassian.net", "projects": ["A"]}, None, True),
             origin="env")
    d.jira_issues(ctx())
    api = FakeAtlassian.made[0]
    assert api.base == "https://a.atlassian.net" and api.email is None and api.token is None  # real class reads env
    assert api.use_env is True


def test_ui_row_without_secret_gets_no_env_credentials(atl, registry_db):
    add_rows(registry_db, ("jira", "site1", {"base_url": "https://a.atlassian.net", "projects": ["A"]}, None, True),
             ("confluence", "wiki", {"base_url": "https://a.atlassian.net", "spaces": ["E"]}, None, True))
    d.jira_issues(ctx())
    d.confluence_pages(ctx())
    assert [a.use_env for a in FakeAtlassian.made] == [False, False]


def test_atlassian_without_env_ignores_env_credentials(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "env@x")
    monkeypatch.setenv("JIRA_API_TOKEN", "envtk")
    monkeypatch.setenv("ATLASSIAN_EMAIL", "env@x")
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "envtk")
    from kg_pipeline.sources.atlassian import Atlassian
    assert Atlassian("https://a.atlassian.net", use_env=False)._auth is None
    assert Atlassian("https://a.atlassian.net", "e", "t", use_env=False)._auth is not None


def test_real_atlassian_env_fallback(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "env@x")
    monkeypatch.setenv("JIRA_API_TOKEN", "envtk")
    from kg_pipeline.sources.atlassian import Atlassian
    a = Atlassian(base_url="https://a.atlassian.net", email=None, token=None)
    assert a._auth is not None


def test_env_only_jira_and_confluence_without_database(atl, monkeypatch):
    monkeypatch.setenv("JIRA_PROJECTS", "P1, P2")
    monkeypatch.setenv("JIRA_BASE_URL", "https://env.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "env@x")
    monkeypatch.setenv("JIRA_API_TOKEN", "envtk")
    monkeypatch.setenv("CONFLUENCE_SPACES", "S1")
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://env.atlassian.net")
    d.jira_issues(ctx())
    d.confluence_pages(ctx())
    assert [c[3] for c in atl["jira"]] == ["P1", "P2"]
    assert atl["jira"][0][:3] == ("https://env.atlassian.net", "env@x", "envtk")
    assert [c[3] for c in atl["confluence"]] == ["S1"]


def test_full_mode_passes_full_flag(atl, monkeypatch):
    monkeypatch.setenv("JIRA_PROJECTS", "P1")
    monkeypatch.setenv("JIRA_BASE_URL", "https://h")
    monkeypatch.setenv("CONFLUENCE_SPACES", "S1")
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://h")
    for tags in ({}, {"kg/mode": "incremental"}, {"kg/mode": "full"}):
        d.jira_issues(ctx(tags))
        d.confluence_pages(ctx(tags))
    assert [c[4] for c in atl["jira"]] == [False, False, True]
    assert [c[4] for c in atl["confluence"]] == [False, False, True]


def test_permissions_follow_registry_sources(atl, registry_db):
    add_rows(registry_db,
             ("jira", "site1", {"base_url": "https://a.atlassian.net", "projects": ["A"]},
              {"email": "e", "api_token": "tk"}, True),
             ("confluence", "wiki", {"base_url": "https://w.atlassian.net", "spaces": ["ENG"]},
              {"email": "e", "api_token": "tk2"}, True))
    d.source_permissions(ctx())
    assert atl["perm_jira"] == [("https://a.atlassian.net", "tk", "A")]
    assert atl["perm_conf"] == [("https://w.atlassian.net", "tk2", "ENG")]


def test_confluence_full_ignores_version_cursor(monkeypatch):
    from kg_pipeline.sources import documents as docs
    page = {"id": "1", "title": "T", "version": {"number": 3}}
    monkeypatch.setattr(docs, "fetch_pages", lambda api, space: iter([page]))
    monkeypatch.setattr(docs, "_unchanged", lambda w, doc_id, version: True)
    written = []
    monkeypatch.setattr(docs, "_write_document", lambda w, doc, md: written.append(doc["id"]) or 1)
    monkeypatch.setattr("kg_pipeline.sources.history.link_references", lambda w: 0)

    class W:
        def link(self, *a, **k):
            pass

        def _run(self, *a, **k):
            class R:
                records = [{"n": 0}]
            return R()

    class Api:
        base = "https://h"

    assert docs.sync_confluence(W(), Api(), "ENG")["unchanged"] == 1 and written == []
    assert docs.sync_confluence(W(), Api(), "ENG", full=True)["documents"] == 1 and written == ["confluence:ENG:1"]


def test_jira_and_confluence_jobs():
    from kg_pipeline.control import dagster_client as dc
    assert d.jira_job.name == dc.JIRA_JOB and d.confluence_job.name == dc.CONFLUENCE_JOB
    for name, first in ((dc.JIRA_JOB, "jira_issues"), (dc.CONFLUENCE_JOB, "confluence_pages")):
        job = d.defs.get_job_def(name)
        assert {k.to_user_string() for k in job.asset_layer.executable_asset_keys} == {first, "source_vectors"}


# --- N1: the real git._git must not send GIT_TOKEN for a ui source -------------------------------------------------

def _captured_git(monkeypatch, tmp_path):
    from kg_pipeline.sources import git as real_git
    cmds = []
    monkeypatch.setattr(real_git.subprocess, "run", lambda cmd, **kw: cmds.append(cmd) or type("R", (), {"stdout": ""})())
    return real_git, cmds


def _auth(cmd):
    import base64
    heads = [a for a in cmd if a.startswith("http.extraHeader=")]
    return [base64.b64decode(h.rsplit(" ", 1)[1]).decode() for h in heads]


def _real_run(monkeypatch, tmp_path, origin, secret):
    real_git, cmds = _captured_git(monkeypatch, tmp_path)
    monkeypatch.setattr(d, "git", real_git)
    monkeypatch.setattr(d, "ARCHIVE", tmp_path / "arch")
    monkeypatch.setenv("GIT_TOKEN", "env-tok")
    return real_git, cmds


def test_real_git_ui_source_without_secret_sends_no_authorization(registry_db, monkeypatch, tmp_path):
    from sqlalchemy import update
    with registry_db.begin() as conn:
        conn.execute(update(db.sources).values(secret=None))
    real_git, cmds = _real_run(monkeypatch, tmp_path, "ui", None)
    url, branch, token = d._repo("repo-a")
    real_git.mirror(url, d.ARCHIVE, token=token, env_fallback=False, restricted=True)
    assert cmds and all(not _auth(c) for c in cmds)
    assert not any("env-tok" in str(c) for c in cmds)


def test_real_git_ui_source_with_own_token_sends_only_that_token(registry_db, monkeypatch, tmp_path):
    real_git, cmds = _real_run(monkeypatch, tmp_path, "ui", None)
    url, branch, token = d._repo("repo-a")
    real_git.mirror(url, d.ARCHIVE, token=token, env_fallback=False, restricted=True)
    assert [_auth(c) for c in cmds] == [["x-access-token:tok-A"]]


def test_real_git_env_source_sends_env_token(registry_db, monkeypatch, tmp_path):
    from sqlalchemy import update
    with registry_db.begin() as conn:
        conn.execute(update(db.sources).values(secret=None, origin="env"))
    real_git, cmds = _real_run(monkeypatch, tmp_path, "env", None)
    url, branch, token = d._repo("repo-a")
    real_git.mirror(url, d.ARCHIVE, token=token, env_fallback=False, restricted=True)
    assert [_auth(c) for c in cmds] == [["x-access-token:env-tok"]]


def test_real_git_env_only_mode_default_still_uses_env_token(monkeypatch, tmp_path):
    real_git, cmds = _captured_git(monkeypatch, tmp_path)
    monkeypatch.setenv("GIT_TOKEN", "env-tok")
    real_git.mirror("https://h/x/r.git", tmp_path / "a")
    assert [_auth(c) for c in cmds] == [["x-access-token:env-tok"]]


def test_service_catalog_mirror_gets_no_env_token_for_ui_source(fakes, registry_db, monkeypatch):
    from sqlalchemy import update
    with registry_db.begin() as conn:
        conn.execute(update(db.sources).values(secret=None))
    monkeypatch.setenv("GIT_TOKEN", "env-tok")
    seen = []
    from kg_pipeline import people_assets as pa
    class G:
        def mirror(self, url, archive, **kw):
            seen.append(kw)
            return Path("/m")
        def head(self, *a): return "x"
        @contextmanager
        def checkout(self, *a): yield Path("/w")
    monkeypatch.setattr(pa, "git", G())
    monkeypatch.setattr(pa, "_writer", contextmanager(lambda: (yield FakeWriter())))
    monkeypatch.setattr(pa.catalog, "sync", lambda w, work, name: {})
    asset = pa.service_catalog_asset(d.repo_partitions, d.code_snapshot, d._RegistryRepos(), "main", Path("/a"))
    asset(dg.build_asset_context(partition_key="repo-a"))
    assert seen == [{"token": None, "env_fallback": False, "restricted": True}]


def test_real_git_restricted_clone_blocks_redirects_and_other_protocols(registry_db, monkeypatch, tmp_path):
    from kg_pipeline.sources import git as real_git
    seen = []
    monkeypatch.setattr(real_git.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw.get("env"))) or type("R", (), {"stdout": ""})())
    real_git.mirror("https://h/x/r.git", tmp_path / "a", env_fallback=False, restricted=True)
    cmd, env = seen[0]
    assert "http.followRedirects=false" in cmd
    assert env["GIT_ALLOW_PROTOCOL"] == "http:https"


def test_real_git_unrestricted_clone_keeps_plain_behaviour(monkeypatch, tmp_path):
    from kg_pipeline.sources import git as real_git
    seen = []
    monkeypatch.setattr(real_git.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw.get("env"))) or type("R", (), {"stdout": ""})())
    real_git.mirror("ssh://h/x/r.git", tmp_path / "a")
    assert "http.followRedirects=false" not in seen[0][0] and seen[0][1] is None


# ---- T3: s3_documents asset and s3_job --------------------------------------------------------------------------

@pytest.fixture
def s3fx(monkeypatch):
    from kg_pipeline.sources import s3 as s3mod
    for k in ("DATABASE_URL", "KG_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    calls = {"client": [], "sync": [], "guard": []}
    monkeypatch.setattr(d, "_writer", contextmanager(lambda: (yield FakeWriter())))
    monkeypatch.setattr(d.s3, "build_client", lambda cfg, secret, ui_origin=False: calls["client"].append(
        (cfg["bucket"], secret, ui_origin)) or cfg["bucket"])

    def sync(w, client, name, cfg, full=False, force=False, **kw):
        calls["sync"].append((name, client, full))
        calls.setdefault("force", []).append(force)
        if name == "bad":
            raise RuntimeError("listing failed")
        return {**s3mod.new_stats(), "listed": 2, "new": 1, "unchanged": 1, "bytes": 5, "failed": 0}
    monkeypatch.setattr(d.s3, "sync", sync)
    monkeypatch.setattr(d.probe, "guard_source", lambda kind, cfg: calls["guard"].append((kind, cfg["bucket"])))
    return calls


def test_s3_documents_runs_every_enabled_source_with_its_own_secret(s3fx, registry_db):
    add_rows(registry_db,
             ("s3", "one", {"bucket": "b1", "endpoint": "https://m"}, {"access_key_id": "k1", "secret_access_key": "s1"}, True),
             ("s3", "two", {"bucket": "b2"}, None, True),
             ("s3", "off", {"bucket": "b3"}, {"access_key_id": "k3", "secret_access_key": "s3"}, False))
    res = d.s3_documents(ctx({"kg/mode": "full"}))
    assert s3fx["client"] == [("b1", {"access_key_id": "k1", "secret_access_key": "s1"}, True), ("b2", {}, True)]
    assert s3fx["sync"] == [("one", "b1", True), ("two", "b2", True)]   # secretless ui source: anonymous, full flag passed
    assert s3fx["guard"] == [("s3", "b1"), ("s3", "b2")]
    assert (res.metadata["listed"], res.metadata["new"], res.metadata["bytes"]) == (4, 2, 10)
    assert set(res.metadata["sources"].value) == {"one", "two"}
    assert "s1" not in str(res.metadata)


def test_s3_documents_full_run_forces_reconcile_incremental_keeps_the_guard(s3fx, registry_db):
    add_rows(registry_db, ("s3", "one", {"bucket": "b1"}, None, True))
    d.s3_documents(ctx({"kg/mode": "full"}))
    d.s3_documents(ctx({"kg/mode": "incremental"}))
    d.s3_documents(ctx())
    assert s3fx["force"] == [True, False, False]


def test_s3_documents_env_origin_is_not_ui_origin(s3fx, registry_db):
    add_rows(registry_db, ("s3", "e", {"bucket": "b"}, None, True), origin="env")
    d.s3_documents(ctx())
    assert s3fx["client"][0][2] is False


def test_s3_documents_without_sources_is_skipped(s3fx, registry_db):
    res = d.s3_documents(ctx())
    assert "skipped" in res.metadata and s3fx["sync"] == []


def test_s3_documents_blocked_endpoint_and_failed_source_fail_after_the_others_ran(s3fx, registry_db, monkeypatch):
    add_rows(registry_db, ("s3", "bad", {"bucket": "b1"}, None, True), ("s3", "good", {"bucket": "b2"}, None, True))
    with pytest.raises(dg.Failure, match="bad"):
        d.s3_documents(ctx())
    assert [c[0] for c in s3fx["sync"]] == ["bad", "good"]


def test_s3_documents_blocked_endpoint_is_not_synced(s3fx, registry_db, monkeypatch):
    add_rows(registry_db, ("s3", "zz", {"bucket": "b9", "endpoint": "https://10.0.0.1"}, None, True))
    from kg_pipeline.control import probe
    monkeypatch.setattr(d.probe, "guard_source", lambda kind, cfg: (_ for _ in ()).throw(probe.Blocked("private")))
    with pytest.raises(dg.Failure):
        d.s3_documents(ctx())
    assert s3fx["sync"] == []


def test_s3_job_and_sensor():
    from kg_pipeline.control import dagster_client as dc
    assert d.s3_job.name == dc.S3_JOB == "s3_job"
    job = d.defs.get_job_def(dc.S3_JOB)
    assert {k.to_user_string() for k in job.asset_layer.executable_asset_keys} == {"s3_documents", "source_vectors"}
    assert d.s3_job.name in {j.name for j in d.defs.jobs}
    sensor = d.defs.get_repository_def().get_sensor_def("due_sources")
    assert dc.S3_JOB in {t.job_name for t in sensor.targets}


def test_s3_documents_is_a_sources_asset():
    assert d.s3_documents.key.to_user_string() == "s3_documents" and d.s3_documents in d.SOURCES
