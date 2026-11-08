"""Dagster side of GraphRAG communities: assets, job, schedule, member summary hashes."""
from contextlib import contextmanager

import pytest

from kg_pipeline import communities as c

dg = pytest.importorskip("dagster")

from kg_pipeline import definitions as d  # noqa: E402


def _keys(job):
    return {k.to_user_string() for k in job.asset_layer.executable_asset_keys}


def test_asset_keys_group_and_partitions():
    specs = {s.key.to_user_string(): s for s in d.defs.resolve_all_asset_specs()}
    for name in ("communities", "community_reports"):
        assert specs[name].group_name == "knowledge"
    assert d.communities.partitions_def is None
    assert d.community_reports.partitions_def is None
    assert {x.asset_key.to_user_string() for x in specs["community_reports"].deps} == {"communities"}


def test_job_and_schedule():
    assert d.GRAPHRAG_JOB == "graphrag_job"
    assert _keys(d.defs.get_job_def("graphrag_job")) == {"communities", "community_reports"}
    assert d.graphrag_daily.cron_schedule == "30 3 * * *"
    assert d.graphrag_daily.default_status == dg.DefaultScheduleStatus.STOPPED
    assert d.graphrag_daily.job.name == "graphrag_job"


def test_existing_definitions_unchanged():
    for a in (d.communities, d.community_reports):
        assert a not in d.CODE and a not in d.SOURCES
    assert d.hourly_code.name == "hourly_code"
    assert {j.name for j in d.due_sources.jobs} == {"git_job", "jira_job", "confluence_job", "s3_job"}
    assert not any(k in _keys(d.defs.get_job_def("git_job")) for k in ("communities", "community_reports"))
    assert d.sources_every_15_min.name == "sources_every_15_min"


# ---- member summary hashes ----------------------------------------------------------------------------------------

class _Rec:
    def __init__(self, row):
        self.row = row

    def data(self):
        return self.row


class _Res:
    def __init__(self, rows):
        self.records = [_Rec(r) for r in rows]


class HashWriter:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def _run(self, q, **p):
        self.queries.append(q)
        rows = sorted((r for r in self.rows if r["id"] > p["after"]), key=lambda r: r["id"])[:p["limit"]]
        return _Res(rows)


def test_load_member_hashes_reads_summary_hashes():
    w = HashWriter([{"id": "a", "hashes": ["h2", "h1"]}, {"id": "b", "hashes": ["x", None]}, {"id": "n", "hashes": []}])
    out = c.load_member_hashes(w)
    assert set(out) == {"a", "b"}
    assert "SUMMARIZES" in w.queries[0] and "content_hash" in w.queries[0]
    assert "text" not in w.queries[0].replace("text_masked", "")


def test_changed_member_summary_changes_community_hash():
    one = c.load_member_hashes(HashWriter([{"id": "a", "hashes": ["h1"]}]))
    two = c.load_member_hashes(HashWriter([{"id": "a", "hashes": ["h2"]}]))
    assert c.content_hash(["a", "b"], one, "v") != c.content_hash(["a", "b"], two, "v")
    same = c.load_member_hashes(HashWriter([{"id": "a", "hashes": ["h1"]}]))
    assert c.content_hash(["a", "b"], one, "v") == c.content_hash(["a", "b"], same, "v")


def test_build_communities_passes_member_hashes(monkeypatch):
    seen = {}
    monkeypatch.setattr(c, "load_partitions", lambda w: ({}, {}))
    monkeypatch.setattr(c, "load_existing", lambda w: {})
    monkeypatch.setattr(c, "load_member_hashes", lambda w: {"a": "h"})
    monkeypatch.setattr(c, "ensure_schema", lambda w, dim: None)
    monkeypatch.setattr(c, "build", lambda *a, **k: seen.update(k) or ([], {"skipped_too_large": 0}))
    c.build_communities(object())
    assert seen["member_hashes"] == {"a": "h"}


# ---- runtime ------------------------------------------------------------------------------------------------------

def test_communities_asset_runs(monkeypatch):
    w = object()
    monkeypatch.setattr(d, "_writer", contextmanager(lambda: (yield w)))
    monkeypatch.setattr(d.comm, "build_communities", lambda ww, **k: {"written": 2, "unchanged": 1, "partitions": 3,
                                                                       "communities_by_level": {0: 2}})
    res = d.communities(dg.build_asset_context())
    assert res.metadata["written"] == 2
    assert res.metadata["communities_by_level"].data == {"0": 2}


def test_community_reports_asset_passes_cache_llm_embedder(monkeypatch):
    w, got = object(), {}
    monkeypatch.setattr(d, "_writer", contextmanager(lambda: (yield w)))
    monkeypatch.setattr(d, "LLM", lambda: "LLM")
    monkeypatch.setattr(d, "Embedder", lambda: "EMB")
    monkeypatch.setenv("COMMUNITY_BUDGET", "7")
    monkeypatch.delenv("COMMUNITY_LLM", raising=False)
    monkeypatch.setattr(d.creports, "build_reports", lambda ww, **k: got.update(k) or {"total": 1, "written": 1})
    res = d.community_reports(dg.build_asset_context())
    assert got["llm"] == "LLM" and got["emb"] == "EMB" and got["budget"] == 7
    assert got["cache"] == d.ARCHIVE / "cache" / "llm"
    assert res.metadata["total"] == 1


def test_community_reports_llm_off(monkeypatch):
    got = {}
    monkeypatch.setattr(d, "_writer", contextmanager(lambda: (yield object())))
    monkeypatch.setattr(d, "LLM", lambda: pytest.fail("LLM must not be created"))
    monkeypatch.setattr(d, "Embedder", lambda: "EMB")
    monkeypatch.setenv("COMMUNITY_LLM", "off")
    monkeypatch.delenv("COMMUNITY_BUDGET", raising=False)
    monkeypatch.setattr(d.creports, "build_reports", lambda ww, **k: got.update(k) or {"total": 0})
    d.community_reports(dg.build_asset_context())
    assert got["llm"] is None and got["budget"] is None
