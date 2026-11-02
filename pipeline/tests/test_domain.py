import os
import textwrap
from pathlib import Path

import pytest

from kg_pipeline.domain.extract import norm
from kg_pipeline.domain.roles import classify
from kg_pipeline.requirements import sections
from kg_pipeline.search import _rrf, lucene


def test_endpoint_roles_across_frameworks():
    cases = {
        'GetMapping("/loans/{id}")': ("GET", "/loans/{id}"),
        'HttpPost("orders")': ("POST", "orders"),
        'Delete(":id")': ("DELETE", ":id"),
        'router.get("/health")': ("GET", "/health"),
        'app.route("/x", methods=["POST"])': (None, "/x"),
    }
    for annotation, (verb, route) in cases.items():
        r = classify("Method", [annotation], [])
        assert r and r["role"] == "endpoint" and r["http_method"] == verb and r["route"] == route, annotation


def test_class_roles():
    assert classify("Class", ["Entity", 'Table(name="loan")'], [])["role"] == "entity"
    assert classify("Class", [], ["Base"])["role"] == "entity"          # SQLAlchemy declarative
    assert classify("Class", ['RestController'], [])["role"] == "controller"
    assert classify("Interface", [], ["JpaRepository"])["role"] == "repository"
    assert classify("Class", ["Injectable()"], [])["role"] == "service"
    assert classify("Class", [], ["BaseModel"])["role"] == "dto"
    assert classify("Class", ["Deprecated"], ["Object"]) is None
    assert classify("Function", ["staticmethod"], []) is None


def test_concept_names_normalise():
    assert norm("LoanApplication") == norm("loan applications") == "loan application"
    assert norm("Address") == "address"   # no singularising of -ss


def test_sections_split_on_headings_and_merge_small_ones():
    md = "# Spec\nintro\n## FR\n- FR-1 The user can save a scenario\n## NFR\n- NFR-1 Pages load in 2 s\n"
    parts = sections(md)
    assert len(parts) == 1 and "FR-1" in parts[0][1] and "NFR-1" in parts[0][1]
    big = "# A\n" + "x " * 5000 + "\n# B\n" + "y " * 5000
    assert [h for h, _ in sections(big)] == ["A", "B"]


def test_rrf_rewards_agreement():
    fused = _rrf(["a", "b", "c"], ["b", "c"], ["c"])
    assert [i for i, _ in fused][0] == "c"


def test_lucene_escapes_special_characters():
    assert lucene('save "scenario" (draft)') == "save OR scenario OR draft"
    assert lucene("a:b") == "*"


def test_file_level_code_gets_chunks():
    from kg_pipeline.code.treesitter import chunks, parse
    src = textwrap.dedent("""\
        import { create } from 'zustand';

        export interface State {
          items: string[];
        }

        export const useStore = create<State>()((set) => ({
          items: [],
          save: (x: string) => set((s) => ({ items: [...s.items, x] })),
        }));
        """)
    found = chunks(src, parse(src, "typescript"))
    file_chunks = [(s, e) for owner, s, e in found if owner is None]
    assert file_chunks, found
    assert any(s <= 7 <= e for s, e in file_chunks)          # the store body is covered
    assert not any(s <= 1 <= e for s, e in file_chunks)      # the import line alone gets no chunk


def test_verdict_must_match_parts_and_citations():
    from kg_pipeline.requirements import consistent
    parts = [{"part": "magnitude field", "found": True}, {"part": "direction field", "found": False}]
    assert consistent("supported", "", parts, True)[0] == "unresolved"
    assert "direction field" in consistent("supported", "", parts, True)[1]
    assert consistent("supported", "", parts[:1], True) == ("supported", "")
    assert consistent("contradicted", "x", [], False)[0] == "unresolved"
    assert consistent("contradicted", "", parts, True)[0] == "contradicted"


def test_citation_of_a_file_with_several_excerpts():
    from kg_pipeline.requirements import _match_citation
    ev = [{"symbol_id": "f#L18", "symbol": "scenarioStore.save", "path": "src/store/scenarioStore.ts"},
          {"symbol_id": "f#L39", "symbol": "scenarioStore.load", "path": "src/store/scenarioStore.ts"},
          {"symbol_id": "v#onSave", "symbol": "ScenariosView.onSave", "path": "src/views/ScenariosView.tsx"}]
    assert _match_citation("scenarioStore.ts", ev)["symbol_id"] == "f#L18"
    assert _match_citation("useScenarioStore.save", ev)["symbol_id"] == "f#L18"
    assert _match_citation("onSave", ev)["symbol_id"] == "v#onSave"


# ---- Jira -------------------------------------------------------------------------------------------
JIRA = Path(__file__).parent / "fixtures" / "jira"


def fake_jira():
    import json
    from urllib.parse import parse_qs, urlparse

    from kg_pipeline.sources.atlassian import Atlassian
    calls = []

    def fetch(url):
        u = urlparse(url)
        calls.append(url)
        if u.path.endswith("/versions"):
            return json.loads((JIRA / "versions.json").read_text())
        page = "search-page2.json" if parse_qs(u.query).get("nextPageToken") == ["page2"] else "search-page1.json"
        return json.loads((JIRA / page).read_text())
    return Atlassian("https://example.atlassian.net", "a@b.c", "t", fetch=fetch), calls


def test_jira_rows_edges_and_mentions():
    import json

    from kg_pipeline.sources.jira import edges, fetch_issues, mentions, ticket_row
    api, calls = fake_jira()
    issues = list(fetch_issues(api, 'project = "KGT"'))
    assert [i["key"] for i in issues] == ["KGT-1", "KGT-2", "KGT-3"] and len(calls) == 2   # two pages
    row = ticket_row(api, issues[1], None)
    assert row["type"] == "Story" and row["status_category"] == "indeterminate" and row["acl"] == ["jira:KGT"]
    assert "### Acceptance criteria" in row["text"] and "- Saving an existing scenario" in row["text"]
    assert "@Sam the API already supports PUT." in row["text"]                                   # comments
    e = edges(issues[1])
    assert e["CHILD_OF"] == [{"src": "jira:KGT-2", "dst": "jira:KGT-1"}]
    assert e["LINKS"] == [{"src": "jira:KGT-2", "dst": "jira:KGT-3", "type": "blocks"}]
    assert edges(issues[2])["LINKS"] == []                                                       # inward side skipped
    paths, names = mentions(row["text"] + ticket_row(api, issues[0], None)["text"] + " run_scenario")
    assert "frontend/src/store/scenarioStore.ts" in paths and {"ScenariosView", "run_scenario"} <= names
    assert json.dumps(row)                                                                       # serialisable


@pytest.mark.skipif(not os.environ.get("NEO4J_PASSWORD"), reason="NEO4J_PASSWORD not set")
def test_jira_sync_into_neo4j():
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter
    from kg_pipeline.sources.jira import sync
    api, _ = fake_jira()
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), "neo4j", os.environ["NEO4J_PASSWORD"]) as w:
        try:
            res = sync(w, api, "KGT")
            assert res["tickets"] == 3 and res["releases"] == 2 and res["child_of"] == 1 and res["links"] == 1
            n = w._run("MATCH (t:Ticket {project: 'KGT'})-[:IN_RELEASE]->(r:Release) RETURN count(*) AS c").records[0]["c"]
            assert n == 3
            assert w.state("jira:KGT:updated") == "2026-09-22 11:15"
            sync(w, api, "KGT")    # idempotent
            assert w._run("MATCH (:Ticket {project: 'KGT'})-[r:LINKS]->() RETURN count(r) AS c").records[0]["c"] == 1
        finally:
            w._run("MATCH (n:Source) WHERE n.id STARTS WITH 'jira:KGT' OR n.id STARTS WITH 'jira:release:KGT' DETACH DELETE n")
            w._run("MATCH (s:SyncState) WHERE s.key STARTS WITH 'jira:KGT' DELETE s")


# ---- git history ------------------------------------------------------------------------------------
def _git_repo(tmp_path):
    import subprocess
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "dev@example.com")
    run("config", "user.name", "Dev")
    src = tmp_path / "app.py"
    src.write_text("def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")
    run("add", ".")
    run("commit", "-q", "-m", "KGT-1: add alpha and beta")
    src.write_text("def alpha():\n    return 1\n\n\ndef beta():\n    return 3\n")
    (tmp_path / "old.txt").write_text("x\n")
    run("add", ".")
    run("commit", "-q", "-m", "Fix beta\n\nRefs KGT-2 and P14-02")
    return tmp_path


def test_git_history_parse_and_symbol_mapping(tmp_path):
    from kg_pipeline.sources.history import git_log, map_symbols, parse
    repo = _git_repo(tmp_path)
    commits = parse(git_log(repo, None, 10))
    assert [c["title"] for c in commits] == ["Fix beta", "KGT-1: add alpha and beta"]       # newest first
    assert commits[0]["keys"] == ["KGT-2", "P14-02"] and commits[1]["keys"] == ["KGT-1"]
    assert commits[0]["files"]["app.py"]["added"] == 1 and commits[0]["files"]["old.txt"]["change"] == "A"
    symbols = {"app.py": [{"id": "r:app.py#alpha", "name": "alpha", "start": 1, "end": 2},
                          {"id": "r:app.py#beta", "name": "beta", "start": 5, "end": 6}]}
    rows = map_symbols(commits, symbols)
    newest = {r["dst"]: r["confidence"] for r in rows if r["sha"] == commits[0]["sha"]}
    assert newest == {"r:app.py#beta": 0.9}                                                    # only beta changed
    older = {r["dst"] for r in rows if r["sha"] == commits[1]["sha"]}
    assert older == set() or older <= {"r:app.py#alpha", "r:app.py#beta"}                      # header heuristic only


# ---- documents and Confluence -------------------------------------------------------------------------
def test_split_sections_keeps_code_fences_and_cuts_long_text():
    from kg_pipeline.sources.documents import SECTION_CHARS, split_sections
    md = "# Guide\n\nIntro text with enough words here.\n\n## Setup\n\n```bash\n# not a heading\nmake all\n```\n\nRun the make target now please.\n"
    md += "\n## Long\n\n" + "\n\n".join(["word " * 100] * 10)
    secs = split_sections(md)
    assert secs[0][0] == "Guide" and secs[1][0] == "Guide > Setup" and "# not a heading" in secs[1][1]
    longs = [t for h, t in secs if h == "Guide > Long"]
    assert len(longs) > 1 and all(len(t) <= SECTION_CHARS for t in longs)


def fake_confluence():
    import json
    from urllib.parse import parse_qs, urlparse

    from kg_pipeline.sources.atlassian import Atlassian
    d = Path(__file__).parent / "fixtures" / "confluence"

    def fetch(url):
        u = urlparse(url)
        if u.path.endswith("/spaces"):
            return json.loads((d / "spaces.json").read_text())
        name = "pages-2.json" if parse_qs(u.query).get("cursor") == ["abc"] else "pages-1.json"
        return json.loads((d / name).read_text())
    return Atlassian("https://example.atlassian.net", "a@b.c", "t", fetch=fetch, prefix="CONFLUENCE")


@pytest.mark.skipif(not os.environ.get("NEO4J_PASSWORD"), reason="NEO4J_PASSWORD not set")
def test_confluence_and_folder_sync(tmp_path):
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter
    from kg_pipeline.sources.documents import sync_confluence, sync_folder
    (tmp_path / "P99-01-feature.md").write_text("# Feature P99-01\n\n## Goal\n\nUsers export scenarios as JSON files.\n")
    (tmp_path / "spec.pdf").write_bytes(b"%PDF-1.7 fake")
    fake_docling = lambda p: "# Spec\n\n## Rules\n\nA scenario name must be unique per topology." if p.suffix == ".pdf" else p.read_text()
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), "neo4j", os.environ["NEO4J_PASSWORD"]) as w:
        try:
            res = sync_confluence(w, fake_confluence(), "KGT")
            assert res["documents"] == 2 and res["sections"] >= 3
            assert sync_confluence(w, fake_confluence(), "KGT")["unchanged"] == 2           # version unchanged
            tree = w._run("MATCH (:Document {id: 'confluence:KGT:2'})-[:CHILD_OF]->(p:Document) RETURN p.title AS t").records
            assert tree[0]["t"] == "Scenario builder ADR"
            res = sync_folder(w, tmp_path, "kgt-docs", read=fake_docling)
            assert res["documents"] == 2 and res["failed"] == 0
            pdf = w._run("MATCH (s:Section)-[:PART_OF]->(:Document {id: 'doc:kgt-docs:spec.pdf'}) RETURN s.text AS t").records
            assert "unique per topology" in pdf[0]["t"]
            (tmp_path / "spec.pdf").unlink()
            assert sync_folder(w, tmp_path, "kgt-docs", read=fake_docling)["removed"] == 1
        finally:
            w._run("MATCH (n:Source) WHERE n.id STARTS WITH 'confluence:KGT:' OR n.id STARTS WITH 'doc:kgt-docs:' DETACH DELETE n")
