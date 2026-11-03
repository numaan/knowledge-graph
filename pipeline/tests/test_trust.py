"""Data trust: reconciliation of deletes and moves, provenance and duplicate documents."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from kg_pipeline import dedupe, provenance, reconcile
from kg_pipeline.sources.atlassian import Atlassian, NotFound

FIX = Path(__file__).parent / "fixtures"
TRUST = FIX / "trust"


class Rec(dict):
    def data(self):
        return dict(self)


class FakeWriter:
    """Answers read queries by substring and records every call; enough for code that uses _run and state."""

    def __init__(self, reads: dict[str, list[dict]] | None = None):
        self.reads, self.calls, self.states = reads or {}, [], {}

    def _run(self, query, **params):
        self.calls.append((query, params))
        for part, rows in self.reads.items():
            if part in query:
                return SimpleNamespace(records=[Rec(r) for r in rows])
        if "DETACH DELETE" in query:
            return SimpleNamespace(records=[Rec(n=len(params.get("ids", [])))])
        return SimpleNamespace(records=[])

    def state(self, key, value=None):
        if value is not None:
            self.states[key] = value
        return self.states.get(key)

    def deleted(self) -> list[str]:
        return [i for q, p in self.calls if "DETACH DELETE" in q for i in p.get("ids", [])]


def fake_atlassian(prefix="JIRA"):
    issues = json.loads((TRUST / "jira-issues.json").read_text())
    pages = json.loads((TRUST / "confluence-pages.json").read_text())

    def fetch(url):
        u = urlparse(url)
        if u.path.endswith("/search/jql"):
            assert parse_qs(u.query)["fields"] == ["key"]
            return json.loads((TRUST / "jira-keys.json").read_text())
        if u.path.endswith("/versions"):
            return json.loads((FIX / "jira" / "versions.json").read_text())
        if "/rest/api/3/issue/" in u.path:
            key = u.path.rsplit("/", 1)[1]
            if key not in issues:
                raise NotFound(f"GET {u.path} failed: HTTP 404")
            return issues[key]
        if u.path.endswith("/wiki/api/v2/spaces"):
            return json.loads((FIX / "confluence" / "spaces.json").read_text())
        if u.path.endswith("/wiki/api/v2/spaces/99"):
            return json.loads((TRUST / "confluence-space-99.json").read_text())
        if u.path.endswith("/pages"):
            assert parse_qs(u.query)["status"] == ["current"]
            return json.loads((TRUST / "confluence-ids.json").read_text())
        if "/wiki/api/v2/pages/" in u.path:
            pid = u.path.rsplit("/", 1)[1]
            if pid not in pages:
                raise NotFound(f"GET {u.path} failed: HTTP 404")
            return pages[pid]
        raise AssertionError(url)
    return Atlassian("https://example.atlassian.net", "a@b.c", "t", fetch=fetch, prefix=prefix)


# ---- reconciliation ----------------------------------------------------------------------------------
def test_jira_reconcile_removes_deleted_and_moved_issues_and_releases():
    w = FakeWriter({
        "MATCH (t:Ticket {project: $p})": [{"key": f"KGT-{i}", "id": f"jira:KGT-{i}"} for i in (1, 2, 3, 4, 5)],
        "MATCH (r:Release {project: $p})": [{"id": "jira:release:KGT:10100"}, {"id": "jira:release:KGT:99999"}],
    })
    res = reconcile.jira(w, fake_atlassian(), "KGT")
    assert res["deleted"] == ["KGT-4"] and res["moved"] == {"KGT-2": "OTH-9"} and res["kept"] == ["KGT-5"]
    assert set(w.deleted()) == {"jira:KGT-4", "jira:KGT-2", "jira:release:KGT:99999"}
    assert json.loads(w.states["reconcile:jira:KGT"])["moved"] == {"KGT-2": "OTH-9"}


def test_reconcile_refuses_a_mass_delete():
    w = FakeWriter({"MATCH (t:Ticket {project: $p})": [{"key": f"KGT-{i}", "id": f"jira:KGT-{i}"} for i in range(1, 60)]})
    with pytest.raises(reconcile.TooManyRemovals):
        reconcile.jira(w, fake_atlassian(), "KGT")
    assert w.deleted() == []


def test_confluence_reconcile_classifies_missing_pages():
    w = FakeWriter({"MATCH (d:Document {collection: $c})": [{"id": f"confluence:KGT:{i}"} for i in range(1, 7)]})
    res = reconcile.confluence(w, fake_atlassian("CONFLUENCE"), "KGT")
    assert res["archived"] == ["2"] and res["deleted"] == ["3", "5"] and res["moved"] == {"4": "OTHER"}
    assert res["kept"] == ["6"]
    assert sorted(w.deleted()) == ["confluence:KGT:2", "confluence:KGT:3", "confluence:KGT:4", "confluence:KGT:5"]


def test_folder_reconcile_keeps_moved_files_and_removes_deleted(tmp_path):
    import hashlib
    (tmp_path / "guides").mkdir()
    (tmp_path / "guides" / "setup.md").write_text("# Setup\n\nRun make.\n")
    version = hashlib.sha256((tmp_path / "guides" / "setup.md").read_bytes()).hexdigest()[:16]
    w = FakeWriter({"MATCH (d:Document {collection: $name, source: 'files'})": [
        {"id": "doc:d:setup.md", "path": "setup.md", "version": version},       # moved to guides/
        {"id": "doc:d:gone.md", "path": "gone.md", "version": "0" * 16}]})       # deleted
    res = reconcile.folder(w, tmp_path, "d", url_base="https://git.example.com/docs/blob/main")
    assert res["moved"] == {"setup.md": "guides/setup.md"} and res["gone"] == ["gone.md"]
    move = next(p for q, p in w.calls if "SET d.id = $new" in q)
    assert move["new"] == "doc:d:guides/setup.md" and move["url"] == "https://git.example.com/docs/blob/main/guides/setup.md"
    assert w.deleted() == ["doc:d:gone.md"]


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_git_reconcile_removes_rewritten_commits_and_resets_cursor(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")
    (tmp_path / "a.txt").write_text("1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "one")
    keep = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "a.txt").write_text("2\n")
    _git(tmp_path, "commit", "-q", "-am", "two")
    dropped = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "reset", "-q", "--hard", keep)         # history rewritten: "two" is gone
    w = FakeWriter({"MATCH (c:Commit {repo: $repo})": [{"sha": keep, "id": f"git:r:{keep}"},
                                                        {"sha": dropped, "id": f"git:r:{dropped}"}]})
    w.states["git:r:head"] = dropped
    res = reconcile.git_history(w, tmp_path, "r")
    assert res["rewritten"] == [dropped] and res["cursor_reset"] and w.deleted() == [f"git:r:{dropped}"]


# ---- provenance --------------------------------------------------------------------------------------
@pytest.mark.parametrize("remote,web", [
    ("git@github.com:acme/app.git", "https://github.com/acme/app"),
    ("https://x-access-token:ghp_secret@github.com/acme/app.git", "https://github.com/acme/app"),
    ("ssh://git@gitlab.example.com:2222/team/app.git", "https://gitlab.example.com/team/app"),
    ("/srv/mirrors/app.git", None),
])
def test_web_remote_drops_credentials(remote, web):
    assert provenance.web_remote(remote) == web


def test_commit_urls():
    assert provenance.commit_url("https://github.com/a/b", "abc") == "https://github.com/a/b/commit/abc"
    assert provenance.commit_url("https://gitlab.com/a/b", "abc") == "https://gitlab.com/a/b/-/commit/abc"
    assert provenance.commit_url("https://bitbucket.org/a/b", "abc") == "https://bitbucket.org/a/b/commits/abc"
    assert provenance.commit_url(None, "abc") is None


def test_ticket_and_release_provenance():
    from kg_pipeline.sources.jira import FIELDS, ticket_row
    api = fake_atlassian()
    issue = json.loads((FIX / "jira" / "search-page1.json").read_text())["issues"][0]
    issue = {**issue, "fields": {**issue["fields"], "reporter": {"displayName": "Sam Lee"}}}
    row = ticket_row(api, issue, None)
    assert "reporter" in FIELDS and row["author"] == "Sam Lee"
    assert row["url"] == f"https://example.atlassian.net/browse/{issue['key']}"
    assert row["source_updated_at"] == issue["fields"]["updated"]


def test_file_provenance_from_git_and_mtime(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Doc Writer")
    (tmp_path / "a.md").write_text("# A\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "docs")
    meta = provenance.file_meta(tmp_path / "a.md")
    assert meta["author"] == "Doc Writer" and meta["source_updated_at"][:4].isdigit()
    (tmp_path / "b.md").write_text("# B\n")                      # not committed: mtime, no author
    assert provenance.file_meta(tmp_path / "b.md")["author"] is None
    assert provenance.file_url(tmp_path / "a.md", "a.md", None).startswith("file:///")
    assert provenance.file_url(tmp_path / "a.md", "my doc.md", "https://h/x/") == "https://h/x/my%20doc.md"


def test_upsert_removes_secrets_before_the_write(monkeypatch):
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter
    w = Neo4jWriter.__new__(Neo4jWriter)
    sent = []
    monkeypatch.setattr(Neo4jWriter, "ensure_schema", lambda self: None)
    monkeypatch.setattr(Neo4jWriter, "_run", lambda self, q, **p: sent.append(p))
    token = "ghp_" + "Z9y8X7w6V5" * 4
    w.upsert(("Ticket",), [{"id": "jira:X-1", "title": "t", "text": f"mail ann@acme.io token {token}", "acl": []}])
    props = sent[0]["rows"][0]["props"]
    assert token not in json.dumps(sent) and props["text_masked"] == "mail [EMAIL] token [SECRET:github_token]"
    assert props["text"] == "mail ann@acme.io token [SECRET:github_token]" and props["pii"] == ["email", "github_token"]


# ---- duplicates --------------------------------------------------------------------------------------
BASE = ("Scenarios are saved through the scenario store and the API, not in browser storage. "
        "On save the client upserts by id, and the list shows the newest scenarios first. "
        "A scenario name must be unique per topology, and the API rejects a duplicate name with a clear error. "
        "Drafts are kept for seven days and then removed by the nightly cleanup job.")


def _items(texts: dict[str, tuple[str, str]]) -> dict[str, dict]:
    return {i: {"sketch": dedupe.sketch(t), "hash": dedupe.norm_hash(t), "source": src, "chars": len(t)}
            for i, (src, t) in texts.items()}


def test_exact_and_near_duplicates_get_one_canonical():
    items = _items({
        "doc:d:adr.md#s0": ("files", "**" + BASE.upper().replace(". ", ".\n\n") + "**"),   # same text, other markup
        "confluence:ENG:1#s0": ("confluence", BASE),
        "confluence:OPS:7#s0": ("confluence", BASE.replace("seven days", "ten days")),     # near copy
        "doc:d:other.md#s0": ("files", "Releases are cut every second Tuesday after the change board approves "
                                        "the list of tickets and the release manager tags the build in git."),
    })
    found = dedupe.pairs(items, 0.8)
    assert found[("confluence:ENG:1#s0", "doc:d:adr.md#s0")] == ("hash", 1.0)
    assert found[("confluence:ENG:1#s0", "confluence:OPS:7#s0")][0] == "minhash"
    assert not any("other.md" in a + b for a, b in found)
    canon = dedupe.clusters(items, found)
    assert canon["doc:d:adr.md#s0"] == ("confluence:ENG:1#s0", "hash", 1.0)
    assert canon["confluence:OPS:7#s0"][0] == "confluence:ENG:1#s0" and canon["confluence:OPS:7#s0"][2] >= 0.8
    assert "confluence:ENG:1#s0" not in canon and "doc:d:other.md#s0" not in canon


def test_document_sketch_is_the_union_of_its_sections():
    a, b = BASE[:180], BASE[180:]
    assert dedupe.union([dedupe.sketch(a), dedupe.sketch(b)]) == dedupe.union([dedupe.sketch(b), dedupe.sketch(a)])
    assert dedupe.jaccard(dedupe.sketch(BASE), dedupe.sketch(BASE)) == 1.0
    assert dedupe.jaccard(dedupe.sketch(a), dedupe.sketch(b)) < 0.2


def test_detect_writes_only_changed_edges():
    sec = lambda doc, i, src, text: {"id": f"{doc}#s{i}", "doc": doc, "source": src, "sketch": dedupe.sketch(text),
                                     "hash": dedupe.norm_hash(text), "words": len(dedupe.words(text)), "order": i,
                                     "chars": len(text)}
    rows = [sec("confluence:ENG:1", 0, "confluence", BASE), sec("doc:d:adr.md", 0, "files", BASE)]
    h = lambda *p: __import__("hashlib").sha256(json.dumps(p).encode()).hexdigest()[:20]
    w = FakeWriter({
        "WHERE s.dup_sketch IS NULL": [],
        "MATCH (s:Section)-[:PART_OF]->(d:Document)": rows,
        "[r:DUPLICATE_OF]->(b:Source) RETURN": [
            {"a": "doc:d:adr.md#s0", "b": "confluence:ENG:1#s0", "h": h("hash", 1.0)},      # unchanged
            {"a": "doc:d:old.md", "b": "confluence:ENG:1", "h": "x"}],                       # no longer holds
    })
    res = dedupe.detect(w)
    assert res["duplicates"] == 2 and res["edges_written"] == 1 and res["edges_deleted"] == 1   # the document edge
    written = next(p for q, p in w.calls if "MERGE (a)-[r:DUPLICATE_OF]->(b)" in q)["rows"]
    assert written == [{"src": "doc:d:adr.md", "dst": "confluence:ENG:1",
                        "props": {"method": "hash", "confidence": 1.0, "content_hash": h("hash", 1.0)}}]


# ---- Neo4j -------------------------------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("NEO4J_PASSWORD"), reason="NEO4J_PASSWORD not set")
def test_reconcile_and_duplicates_in_neo4j(tmp_path):
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter
    from kg_pipeline.sources.documents import sync_folder
    (tmp_path / "a.md").write_text("# ADR\n\n## Decision\n\n" + BASE + "\n")
    (tmp_path / "copy.md").write_text("# ADR copy\n\n## Decision\n\n" + BASE.replace("seven", "ten") + "\n")
    (tmp_path / "b.md").write_text("# Other\n\nReleases are cut every second Tuesday by the release manager.\n")
    read = lambda p: p.read_text()
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), "neo4j", os.environ["NEO4J_PASSWORD"]) as w:
        try:
            assert sync_folder(w, tmp_path, "kgt-trust", read=read)["documents"] == 3
            doc = w._run("MATCH (d:Document {id: 'doc:kgt-trust:a.md'}) RETURN d").records[0]["d"]
            assert doc["url"].startswith("file:///") and doc["source_updated_at"] and doc["ingested_at"]
            res = dedupe.detect(w)
            assert res["edges_written"] >= 2
            assert dedupe.detect(w)["edges_written"] == 0                                  # incremental
            dup = w._run("MATCH (:Document {collection: 'kgt-trust'})-[r:DUPLICATE_OF]->(c) RETURN c.id AS c").records
            assert [r["c"] for r in dup] == ["doc:kgt-trust:a.md"]                          # longer text wins
            (tmp_path / "sub").mkdir()
            (tmp_path / "b.md").rename(tmp_path / "sub" / "b.md")
            (tmp_path / "copy.md").unlink()
            res = reconcile.folder(w, tmp_path, "kgt-trust")
            assert res["moved"] == {"b.md": "sub/b.md"} and res["removed"] == 1
            assert sync_folder(w, tmp_path, "kgt-trust", read=read)["unchanged"] == 2      # move kept the nodes
        finally:
            w._run("MATCH (n:Source) WHERE n.id STARTS WITH 'doc:kgt-trust:' DETACH DELETE n")
            w._run("MATCH (s:SyncState) WHERE s.key STARTS WITH 'reconcile:docs:kgt-trust' DELETE s")
