"""Knowledge layer: summaries, freshness and corrections (mocked LLM, recorded Neo4j calls)."""
import copy
import json
import os
from pathlib import Path

import pytest

from kg_pipeline import corrections, freshness, summaries

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "knowledge" / "repo.json").read_text())


class FakeLLM:
    name = "fake:model"

    def __init__(self, fail: bool = False, answer=None):
        self.calls, self.fail, self.answer = [], fail, answer

    def json(self, system, user, schema):
        self.calls.append(user)
        if self.fail:
            raise RuntimeError("model down")
        if self.answer is not None:
            return self.answer(user)
        return {"summary": f"LLM summary #{len(self.calls)} of {user.splitlines()[1]}"}


class Rec(dict):
    def data(self):
        return dict(self)


class Result:
    def __init__(self, rows):
        self.records = [Rec(r) for r in rows]


class FakeWriter:
    """Records every query; answer(query, params) returns rows for reads."""

    def __init__(self, answer=None):
        self.queries, self.answer = [], answer or (lambda q, p: [])

    def _run(self, query, **params):
        self.queries.append((query, params))
        return Result(self.answer(query, params))

    def find(self, text):
        return [(q, p) for q, p in self.queries if text in q]


def _existing(rows):
    return {r["id"]: {"content_hash": r["content_hash"], "model": r["model"], "text": r["text"]} for r in rows}


# ---- summaries ---------------------------------------------------------------------------------------------
def test_docstrings_from_python_block_and_line_comments():
    doc = summaries.docstring
    assert doc('def save(self):\n    """Insert or update one scenario.\n\n    Details."""\n    pass') == \
        "Insert or update one scenario."
    assert doc('"""Module docstring\nover two lines."""\nimport os') == "Module docstring over two lines."
    assert doc("/**\n * Loads the scenario list.\n * @param id\n */\nexport function load(id) {}") == \
        "Loads the scenario list. @param id"
    assert doc("// Package store keeps scenarios.\n// Second line.\npackage store") == \
        "Package store keeps scenarios. Second line."
    assert doc("# Copyright 2026 Example Corp. Licensed under MIT.\nimport os") == ""
    assert doc("def f():\n    return 1") == "" and doc(None) == ""


def test_summaries_build_bottom_up_with_llm(tmp_path):
    llm = FakeLLM()
    rows, stats = summaries.build("kgt", FIXTURE, {}, llm, budget=100, cache=tmp_path)
    levels = {lv: [r for r in rows if r["level"] == lv] for lv in summaries.LEVELS}
    assert [len(levels[lv]) for lv in summaries.LEVELS] == [3, 2, 1, 1]
    assert stats["llm_calls"] == 7 and stats["written"] == 7 and all(r["model"] == "fake:model" for r in rows)
    module = next(r for r in levels["module"] if r["path"] == "app/scenarios")
    assert module["id"] == "summary:module:kgt:app/scenarios" and module["target_id"] == "kgt:folder:app/scenarios"
    assert levels["repo"][0]["target_id"] == "kgt" and levels["domain"][0]["target_id"] == "kgt:capability:app/scenarios"
    file_prompt = next(c for c in llm.calls if c.startswith("Repository: kgt\nFile: app/scenarios/store.py"))
    assert "ScenarioStore [repository]" in file_prompt and "Insert or update one scenario." in file_prompt
    module_prompt = next(c for c in llm.calls if "Module (folder): app/scenarios" in c)
    assert "Business capability: Scenario management" in module_prompt and "LLM summary" in module_prompt
    domain_prompt = next(c for c in llm.calls if c.startswith("Repository: kgt\nCapability:"))
    assert "unique per topology" in domain_prompt


def test_summaries_regenerate_only_changed_chain(tmp_path):
    rows, _ = summaries.build("kgt", FIXTURE, {}, FakeLLM(), cache=tmp_path / "a")
    llm = FakeLLM()
    again, stats = summaries.build("kgt", FIXTURE, _existing(rows), llm, cache=tmp_path / "b")
    assert llm.calls == [] and stats["written"] == 0 and stats["unchanged"] == 7

    changed = copy.deepcopy(FIXTURE)
    changed["files"][2]["symbols"].append({"qualname": "unslug", "kind": "Function", "signature": "def unslug(s)"})
    llm = FakeLLM()
    rows2, stats = summaries.build("kgt", changed, _existing(rows), llm, cache=tmp_path / "b")
    written = {r["id"] for r in rows2 if r["changed"]}
    assert written == {"summary:file:kgt:app/util.py", "summary:module:kgt:app", "summary:repo:kgt"}
    assert len(llm.calls) == 3      # the scenarios folder and the domain summary are not asked again


def test_summaries_budget_cache_and_extractive_upgrade(tmp_path):
    llm = FakeLLM()
    rows, stats = summaries.build("kgt", FIXTURE, {}, llm, budget=2, cache=tmp_path)
    assert len(llm.calls) == 2 and stats["over_budget"] == 5 and stats["extractive"] == 5
    extractive = [r for r in rows if r["model"] == summaries.EXTRACTIVE]
    assert len(extractive) == 5 and all(r["text"] for r in extractive)
    assert any("Folder app/scenarios with 2 files." in r["text"] for r in extractive)

    # The next run with budget replaces the extractive texts; the two LLM answers come from the cache.
    llm = FakeLLM()
    rows2, stats = summaries.build("kgt", FIXTURE, _existing(rows), llm, budget=100, cache=tmp_path)
    assert all(r["model"] == "fake:model" for r in rows2)
    assert stats["unchanged"] == 2 and stats["llm_calls"] == 5

    # A new run without existing summaries is served from the cache: no LLM call, no budget used.
    llm = FakeLLM()
    _, stats = summaries.build("kgt", FIXTURE, {}, llm, budget=0, cache=tmp_path)
    assert llm.calls == [] and stats["llm_cache_hits"] == 7


def test_summaries_without_llm_and_with_a_failing_llm(tmp_path):
    rows, stats = summaries.build("kgt", FIXTURE, {}, None)
    assert stats.get("llm_calls", 0) == 0 and {r["model"] for r in rows} == {summaries.EXTRACTIVE}
    store = next(r for r in rows if r["path"] == "app/scenarios/store.py")
    assert store["text"].startswith("app/scenarios/store.py (python, 40 lines). Persistence of scenarios")
    assert "class ScenarioStore (repository)" in store["text"]
    routes = next(r for r in rows if r["path"] == "app/scenarios/routes.py")
    assert "Endpoints: PUT /scenarios/{id}." in routes["text"]
    domain = next(r for r in rows if r["level"] == "domain")
    assert "Concepts: Scenario." in domain["text"] and "unique per topology" in domain["text"]
    _, again = summaries.build("kgt", FIXTURE, _existing(rows), None)
    assert again["written"] == 0
    rows, stats = summaries.build("kgt", FIXTURE, {}, FakeLLM(fail=True), cache=tmp_path)
    assert stats["llm_failed"] == 7 and {r["model"] for r in rows} == {summaries.EXTRACTIVE}


def test_folders_and_summary_writes():
    fs = summaries.folders("kgt", FIXTURE["files"])
    assert [f["id"] for f in fs] == ["kgt:folder:app", "kgt:folder:app/scenarios"]
    assert fs[1]["files"] == ["kgt:app/scenarios/routes.py", "kgt:app/scenarios/store.py"]
    rows, _ = summaries.build("kgt", FIXTURE, {}, None)
    w = FakeWriter(lambda q, p: [{"id": "kgt:folder:app", "h": fs[0]["content_hash"]}] if "RETURN d.id" in q
                   else [{"n": 1}] if "count(*)" in q else [])
    out = summaries.write(w, "kgt", rows, fs)
    assert out == {"folders_written": 1, "summaries_written": 7, "summaries_removed": 1}
    writes = w.find("MERGE (s:Summary")
    assert {q.split("MATCH (t:")[1].split(" ")[0] for q, _ in writes} == {"Code", "Folder", "Domain"}
    assert sum(len(p["rows"]) for _, p in writes) == 7


# ---- freshness ---------------------------------------------------------------------------------------------
SECTIONS = [
    {"id": "doc:d:spec.md#s0", "doc_id": "doc:d:spec.md", "title": "Spec > Save", "updated": None,
     "mentions": [{"id": "kgt:app/scenarios/store.py#ScenarioStore.save", "name": "ScenarioStore.save"}]},
    {"id": "doc:d:spec.md#s1", "doc_id": "doc:d:spec.md", "title": "Spec > Slug", "updated": None,
     "mentions": [{"id": "kgt:app/util.py#slug", "name": "slug"}]},
    {"id": "confluence:KGT:9#s0", "doc_id": "confluence:KGT:9", "title": "ADR", "updated": "2026-10-20T08:00:00Z",
     "mentions": [{"id": "kgt:app/util.py#slug", "name": "slug"}]},
]
DOCS = [{"id": "doc:d:spec.md", "updated": "2026-10-01T10:00:00+02:00"},
        {"id": "confluence:KGT:9", "updated": "2026-10-20T08:00:00.000Z"},
        {"id": "doc:d:P14-02-export.md", "updated": "2026-09-01T00:00:00"}]
CODE_CHANGES = {
    "kgt:app/scenarios/store.py#ScenarioStore.save": [
        {"sha": "a" * 40, "title": "Save drafts", "date": "2026-10-15T09:00:00+00:00"},
        {"sha": "b" * 40, "title": "Fix save", "date": "2026-10-03T09:00:00+00:00"},
        {"sha": "c" * 40, "title": "Old", "date": "2026-09-01T09:00:00+00:00"}],
    "kgt:app/util.py#slug": [{"sha": "d" * 40, "title": "Slug", "date": "2026-10-10T09:00:00+00:00"}],
}
DOC_CHANGES = {"doc:d:P14-02-export.md": [{"sha": "e" * 40, "title": "P14-02 export", "date": "2026-09-05T12:00:00+00:00"}]}


def test_parse_time_accepts_iso_variants():
    t = freshness.parse_time
    assert t("2026-10-01T10:00:00+02:00") == t("2026-10-01T08:00:00Z") == t("2026-10-01T08:00:00")
    assert t(None) is None and t("") is None and t("not a date") is None


def test_freshness_marks_sections_and_documents():
    stale = freshness.assess(SECTIONS, DOCS, CODE_CHANGES, DOC_CHANGES)
    assert set(stale) == {"doc:d:spec.md#s0", "doc:d:spec.md#s1", "doc:d:spec.md", "doc:d:P14-02-export.md"}
    reason = stale["doc:d:spec.md#s0"]["reason"]
    assert reason.startswith("ScenarioStore.save changed in commit aaaaaaaaaa on 2026-10-15 (Save drafts) and 1 later")
    assert reason.endswith("after the document was updated on 2026-10-01")
    assert stale["doc:d:spec.md#s0"]["changed"] == ["kgt:app/scenarios/store.py#ScenarioStore.save"]
    assert "2 stale section(s): Spec > Save, Spec > Slug" in stale["doc:d:spec.md"]["reason"]
    assert "feature commit eeeeeeeeee" in stale["doc:d:P14-02-export.md"]["reason"]
    assert "confluence:KGT:9#s0" not in stale             # the ADR was updated after the slug change


def test_freshness_flags_keep_human_corrections():
    stale = freshness.assess(SECTIONS, DOCS, CODE_CHANGES, DOC_CHANGES)
    current = {"doc:d:spec.md#s1": {"id": "doc:d:spec.md#s1", "stale_by": "correction", "reason": "human"},
               "doc:d:spec.md#s0": {"id": "doc:d:spec.md#s0", "stale_by": "freshness",
                                    "reason": stale["doc:d:spec.md#s0"]["reason"]},
               "doc:d:old.md": {"id": "doc:d:old.md", "stale_by": "freshness", "reason": "x"}}
    w = FakeWriter()
    out = freshness.write_flags(w, stale, current)
    written = {r["id"] for r in w.find("SET n.stale = true")[0][1]["rows"]}
    assert written == {"doc:d:spec.md", "doc:d:P14-02-export.md"}       # unchanged and human flags are not written
    assert w.find("REMOVE n.stale")[0][1]["ids"] == ["doc:d:old.md"]
    assert out == {"stale": 4, "flags_written": 2, "flags_cleared": 1, "kept_by_correction": 1}


def test_conflict_check_budget_threshold_and_cache(tmp_path):
    pairs = [{"src": f"s{i}", "dst": f"x{i}", "name": f"f{i}", "section": f"The limit is {i}.",
              "code": f"LIMIT = {i + 1}"} for i in range(3)]
    verdicts = {"0": {"conflict": True, "reason": "doc says 0, code says 1", "confidence": 0.9},
                "1": {"conflict": True, "reason": "unsure", "confidence": 0.3}}
    llm = FakeLLM(answer=lambda user: verdicts.get(user.split("The limit is ")[1][0],
                                                     {"conflict": False, "reason": "", "confidence": 0.8}))
    results, stats = freshness.check_conflicts(pairs, llm, budget=2, cache=tmp_path)
    assert stats["llm_calls"] == 2 and stats["over_budget"] == 1 and stats["conflicts"] == 1
    assert [(r["src"], r["conflict"]) for r in results] == [("s0", True), ("s1", False)]   # 0.3 is below the threshold
    results, stats = freshness.check_conflicts(pairs, llm, budget=1, cache=tmp_path)
    assert stats["llm_cache_hits"] == 2 and stats["llm_calls"] == 1 and len(results) == 3
    w = FakeWriter(lambda q, p: [{"n": 2}] if "count(*)" in q else [])
    out = freshness.write_conflicts(w, results, pairs)
    assert out == {"conflicts_written": 1, "conflicts_removed": 4}
    assert w.find("MERGE (s)-[r:CONFLICTS_WITH]")[0][1]["rows"][0]["reason"] == "doc says 0, code says 1"


# ---- corrections -------------------------------------------------------------------------------------------
CORRECTIONS = [
    {"id": "c1", "kind": "suppress_edge", "target_id": "kgt:a.py#a", "edge_type": "CALLS", "other_id": "kgt:b.py#b",
     "status": "approved", "created_at": "2026-11-01"},
    {"id": "c2", "kind": "add_fact", "target_id": "kgt:capability:app", "edge_type": "COVERS", "other_id": "kgt:b.py",
     "text": "b.py belongs here", "author": "sam", "status": "approved", "created_at": "2026-11-02"},
    {"id": "c3", "kind": "mark_outdated", "target_id": "doc:d:spec.md", "text": "Replaced by ADR 7", "status": "approved",
     "created_at": "2026-11-03"},
    {"id": "c4", "kind": "add_fact", "target_id": "x", "edge_type": "CALLS", "other_id": "y", "status": "proposed"},
    {"id": "c5", "kind": "add_fact", "target_id": "x", "edge_type": "CALLS", "other_id": "y", "status": "rejected",
     "applied_at": "2026-11-03T00:00:00Z"},
    {"id": "c6", "kind": "add_fact", "target_id": "x", "edge_type": "calls]->() DELETE n//", "other_id": "y",
     "status": "approved"},
    {"id": "c7", "kind": "rename", "target_id": "x", "status": "approved"},
]


def test_correction_plan_validates_and_groups():
    p = corrections.plan(CORRECTIONS)
    assert [c["id"] for c in p["suppress"]["CALLS"]] == ["c1"] and [c["id"] for c in p["add"]["COVERS"]] == ["c2"]
    assert [c["id"] for c in p["outdated"]] == ["c3"] and [c["id"] for c in p["undo"]] == ["c5"]
    assert {i["id"] for i in p["invalid"]} == {"c6", "c7"}                  # unsafe edge type, unknown kind
    assert "other_id" in corrections.problem({"kind": "suppress_edge", "target_id": "a", "edge_type": "CALLS"})


def test_correction_apply_queries():
    def answer(q, p):
        if "MATCH (c:Correction) WHERE" in q:
            return CORRECTIONS
        if "RETURN row.id AS id, count(*) AS n" in q:
            return [{"id": r["id"], "n": 1} for r in p["rows"]]
        return []
    w = FakeWriter(answer)
    out = corrections.apply(w)
    assert out == {"approved": 5, "applied": 3, "suppressed_edges": 1, "added_facts": 1, "outdated_marks": 1,
                   "not_found": 0, "undone": 1, "invalid": 2}
    suppress = w.find("MATCH (a)-[r:CALLS]->(b) WHERE coalesce(r.method, '') <> 'human'")
    assert suppress and suppress[0][1]["rows"][0]["other_id"] == "kgt:b.py#b"
    add = w.find("MERGE (a)-[r:COVERS]->(b) SET r.method = 'human', r.confidence = 1.0")
    assert add[0][1]["rows"][0]["author"] == "sam"
    assert w.find("SET n.stale = true")[0][1]["rows"][0]["text"] == "Replaced by ADR 7"
    assert w.find("[r:CALLS {correction_id: row.id}]")[0][1]["rows"][0]["id"] == "c5"     # undo of the rejected fact
    assert not any("calls]" in q for q, _ in w.queries)                                    # unsafe type never reaches Cypher
    order = [i for i, (q, _) in enumerate(w.queries) if "DELETE r RETURN" in q or "MERGE (a)-[r:" in q]
    assert "DELETE" in w.queries[order[0]][0]                                              # suppress before add
    errors = w.find("SET c.error = row.error")[0][1]["rows"]
    assert {e["id"] for e in errors} == {"c6", "c7"}


# ---- Neo4j -------------------------------------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("NEO4J_PASSWORD"), reason="NEO4J_PASSWORD not set")
def test_knowledge_layer_in_neo4j(tmp_path):
    from kg_pipeline.code.ingest import build_graph
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text('"""Entry point."""\nfrom .b import b\n\n\ndef a():\n    """Call b."""\n    return b()\n')
    (src / "pkg" / "b.py").write_text("def b():\n    return 1\n")
    repo = "it-knowledge"
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), "neo4j", os.environ["NEO4J_PASSWORD"]) as w:
        try:
            w.write_delta(build_graph(src, repo, scip_mode="off"))
            llm = FakeLLM()
            s = summaries.summarize(w, repo, llm, None, budget=10, cache=tmp_path / "cache")
            assert s["total"] == 4 and s["llm_calls"] == 4      # 2 files, 1 folder, 1 repository
            assert summaries.summarize(w, repo, llm, None, cache=tmp_path / "cache")["written"] == 0
            got = w._run("MATCH (s:Summary {repo: $r})-[:SUMMARIZES]->(t) RETURN s.level AS l, labels(t) AS t", r=repo).records
            assert sorted(r["l"] for r in got) == ["file", "file", "module", "repo"]

            w.upsert(("Document",), [{"id": "doc:it-k:spec.md", "title": "Spec", "updated": "2026-01-01T00:00:00Z",
                                      "acl": ["docs:it-k"], "source": "files"}])
            w.upsert(("Section",), [{"id": "doc:it-k:spec.md#s0", "title": "Spec", "text": "a calls b", "acl": ["docs:it-k"],
                                     "source": "files"}])
            w.link("PART_OF", [{"src": "doc:it-k:spec.md#s0", "dst": "doc:it-k:spec.md"}])
            w.link("MENTIONS", [{"src": "doc:it-k:spec.md#s0", "dst": f"{repo}:pkg/b.py#b", "method": "text",
                                 "confidence": 0.6}], dst="Code")
            w.upsert(("Commit",), [{"id": "git:it-k:1", "sha": "1" * 40, "title": "Change b", "date": "2026-06-01T00:00:00Z",
                                    "repo": repo, "acl": [f"repo:{repo}"], "source": "git"}])
            w.link("MODIFIES", [{"src": "git:it-k:1", "dst": f"{repo}:pkg/b.py#b", "method": "git", "confidence": 1.0}], dst="Code")
            f = freshness.run(w)
            assert f["stale"] == 2
            assert w._run("MATCH (s:Section {id: 'doc:it-k:spec.md#s0'}) RETURN s.stale AS s").records[0]["s"] is True

            a, b = f"{repo}:pkg/a.py#a", f"{repo}:pkg/b.py#b"
            w._run("UNWIND $rows AS row CREATE (c:Correction) SET c = row", rows=[
                {"id": "it-k-c1", "kind": "suppress_edge", "target_id": a, "edge_type": "CALLS", "other_id": b, "status": "approved"},
                {"id": "it-k-c2", "kind": "add_fact", "target_id": b, "edge_type": "USES_TYPE", "other_id": a, "status": "approved"},
                {"id": "it-k-c3", "kind": "mark_outdated", "target_id": f"{repo}:pkg/a.py", "text": "old", "status": "approved"}])
            res = corrections.apply(w)
            assert res["suppressed_edges"] == 1 and res["added_facts"] == 1 and res["outdated_marks"] == 1
            w.write_delta(build_graph(src, repo, scip_mode="off"))           # re-ingestion brings the edge back ...
            corrections.apply(w)                                             # ... and the overlay removes it again
            assert not w._run("MATCH (:Code {id: $a})-[r:CALLS]->(:Code {id: $b}) RETURN r", a=a, b=b).records
            human = w._run("MATCH (:Code {id: $b})-[r:USES_TYPE]->(:Code {id: $a}) RETURN r.method AS m", a=a, b=b).records
            assert human[0]["m"] == "human"
            w._run("MATCH (c:Correction {id: 'it-k-c2'}) SET c.status = 'rejected'")
            assert corrections.apply(w)["undone"] == 1
            assert not w._run("MATCH (:Code {id: $b})-[r:USES_TYPE]->(:Code {id: $a}) RETURN r", a=a, b=b).records
        finally:
            w._run("MATCH (n:Code {repo: $r}) DETACH DELETE n", r=repo)
            w._run("MATCH (n) WHERE (n:Summary OR n:Folder) AND n.repo = $r DETACH DELETE n", r=repo)
            w._run("MATCH (n:Source) WHERE n.id STARTS WITH 'doc:it-k:' OR n.id STARTS WITH 'git:it-k:' DETACH DELETE n")
            w._run("MATCH (c:Correction) WHERE c.id STARTS WITH 'it-k-' DELETE c")
