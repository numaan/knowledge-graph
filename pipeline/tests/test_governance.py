import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from kg_pipeline.sources.atlassian import Atlassian

FIX = Path(__file__).parent / "fixtures" / "permissions"
CONF = Path(__file__).parent / "fixtures" / "confluence"


def fake_atlassian(prefix="JIRA", fail: tuple[str, ...] = ()):
    """Atlassian client over the recorded Cloud responses; paths in fail answer HTTP 403."""
    calls = []

    def fetch(url):
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        calls.append(u.path)
        if any(f in u.path for f in fail):
            raise RuntimeError(f"GET {u.path} failed: HTTP 403")
        p = u.path
        name = {
            "/rest/api/3/project/KGT/role": "jira-roles.json",
            "/rest/api/3/project/KGT/role/10002": "jira-role-10002.json",
            "/rest/api/3/project/KGT/role/10003": "jira-role-10003.json",
            "/rest/api/3/project/KGT/permissionscheme": "jira-permissionscheme.json",
            "/rest/api/3/permissionscheme/10100/permission": "jira-permissions.json",
            "/rest/api/3/project/KGT/issuesecuritylevelscheme": "jira-security-scheme.json",
            "/rest/api/3/search/jql": "jira-search-levels.json",
            "/wiki/api/v2/spaces/98/permissions": "confluence-space-permissions.json",
            "/wiki/rest/api/group/by-id": f"confluence-group-{q.get('id')}.json",
        }.get(p)
        if p == "/rest/api/3/issuesecurityschemes/level/member":
            name = "jira-level-members-2.json" if q.get("startAt") == "2" else "jira-level-members-1.json"
        if p == "/wiki/rest/api/content":
            name = "confluence-content-2.json" if q.get("start") == "2" else "confluence-content-1.json"
        if p == "/wiki/api/v2/spaces":
            return json.loads((CONF / "spaces.json").read_text())
        if name is None:
            raise RuntimeError(f"GET {p} failed: HTTP 404")
        return json.loads((FIX / name).read_text())
    return Atlassian("https://example.atlassian.net", "a@b.c", "t", fetch=fetch, prefix=prefix), calls


def test_jira_project_and_security_level_acls(monkeypatch):
    from kg_pipeline.sources.permissions import jira_acls
    monkeypatch.delenv("KG_ACL_DEFAULT", raising=False)
    api, calls = fake_atlassian()
    base, restricted = jira_acls(api, "KGT")
    # BROWSE_PROJECTS: role Developers (group KGT-Devs) and group Product-Owners; the user holder is skipped
    assert base == ["jira-group:kgt-devs", "jira-group:product-owners", "jira:KGT"]
    # level 10300: group Security + role Administrators (jira-admins); no jira:KGT for restricted issues
    assert restricted["jira:KGT-3"] == ["jira-group:jira-admins", "jira-group:security", "jira:KGT:level:10300"]
    # level 10301 holds only "reporter": just the level alias, which nobody gets unless mapped
    assert restricted["jira:KGT-2"] == ["jira:KGT:level:10301"]
    assert "jira:KGT-1" not in restricted
    assert calls.count("/rest/api/3/issuesecurityschemes/level/member") == 2          # two pages


@pytest.mark.parametrize("mode,expected", [("deny", ["acl:deny"]), ("project", ["jira:KGT"])])
def test_jira_unknown_permissions_use_the_default(monkeypatch, mode, expected):
    from kg_pipeline.sources.permissions import jira_acls
    monkeypatch.setenv("KG_ACL_DEFAULT", mode)
    base, restricted = jira_acls(fake_atlassian(fail=("/permissionscheme",))[0], "KGT")
    assert base == expected and restricted["jira:KGT-3"][-1] == "jira:KGT:level:10300"
    # the security levels cannot be read: restricted issues get the default, never the project ACL
    base, restricted = jira_acls(fake_atlassian(fail=("/level/member",))[0], "KGT")
    assert "jira:KGT" in base and restricted == {"jira:KGT-3": expected, "jira:KGT-2": expected}
    # restricted issues cannot be listed: the whole project is unknown
    base, restricted = jira_acls(fake_atlassian(fail=("/search/jql",))[0], "KGT")
    assert base == expected and restricted == {}


def test_jira_invalid_default_and_project(monkeypatch):
    from kg_pipeline.sources.permissions import jira_acls
    monkeypatch.setenv("KG_ACL_DEFAULT", "allow")
    with pytest.raises(ValueError):
        jira_acls(fake_atlassian(fail=("/permissionscheme",))[0], "KGT")
    with pytest.raises(ValueError):
        jira_acls(fake_atlassian()[0], 'KGT" OR project = "HR')


def test_confluence_space_and_page_restrictions(monkeypatch):
    from kg_pipeline.sources.permissions import confluence_acls
    monkeypatch.delenv("KG_ACL_DEFAULT", raising=False)
    api, calls = fake_atlassian("CONFLUENCE")
    space, pages = confluence_acls(api, "KGT")
    assert space == ["confluence-group:confluence-users", "confluence-group:kgt-readers", "confluence:KGT"]
    assert pages["confluence:KGT:1"] == space                                         # no restriction
    assert pages["confluence:KGT:2"] == ["confluence-group:eng", "confluence-group:hr"]
    assert pages["confluence:KGT:3"] == ["confluence-group:eng"]                       # own and ancestor: intersection
    assert pages["confluence:KGT:4"] == ["acl:deny"]                                   # users only
    assert pages["confluence:KGT:5"] == ["confluence-group:eng", "confluence-group:hr"]  # inherits from page 2
    assert calls.count("/wiki/rest/api/content") == 2                                 # v1 next link under /wiki
    assert calls.count("/wiki/rest/api/group/by-id") == 2                             # group names cached


def test_confluence_unknown_restrictions_deny(monkeypatch):
    from kg_pipeline.sources.permissions import confluence_acls
    monkeypatch.setenv("KG_ACL_DEFAULT", "deny")
    assert confluence_acls(fake_atlassian("CONFLUENCE", fail=("/rest/api/content",))[0], "KGT") == (["acl:deny"], {})
    monkeypatch.setenv("KG_ACL_DEFAULT", "project")
    space, pages = confluence_acls(fake_atlassian("CONFLUENCE", fail=("/permissions",))[0], "KGT")
    assert space == ["confluence:KGT"] and pages["confluence:KGT:3"] == ["confluence-group:eng"]


class FakeWriter:
    """Records _run calls; returns the given records for MATCH ... RETURN queries."""

    def __init__(self, records=()):
        self.records, self.calls = list(records), []

    def _run(self, query, **params):
        self.calls.append((query, params))
        return type("R", (), {"records": self.records if "RETURN" in query else []})()


def test_permission_sync_writes_only_changed_acls(monkeypatch):
    from kg_pipeline.sources.permissions import sync_confluence, sync_jira
    monkeypatch.delenv("KG_ACL_DEFAULT", raising=False)
    base = ["jira-group:kgt-devs", "jira-group:product-owners", "jira:KGT"]
    w = FakeWriter([{"id": "jira:KGT-1", "acl": list(reversed(base))},     # same set: unchanged
                    {"id": "jira:KGT-3", "acl": ["jira:KGT"]},              # becomes restricted
                    {"id": "jira:release:KGT:1", "acl": ["jira:KGT"]}])
    res = sync_jira(w, fake_atlassian()[0], "KGT")
    assert res["written"] == 2 and res["restricted"] == 2
    rows = {r["id"]: r["acl"] for r in w.calls[-1][1]["rows"]}
    assert rows == {"jira:KGT-3": ["jira-group:jira-admins", "jira-group:security", "jira:KGT:level:10300"],
                    "jira:release:KGT:1": base}
    w = FakeWriter([{"id": "jira:KGT-1", "acl": base}])
    assert sync_jira(w, fake_atlassian()[0], "KGT")["written"] == 0 and len(w.calls) == 1     # nothing to write

    w = FakeWriter([{"id": "confluence:KGT:3", "acl": ["confluence:KGT"]}])
    res = sync_confluence(w, fake_atlassian("CONFLUENCE")[0], "KGT")
    assert res["written"] == 1 and "Section" in w.calls[-1][0]                       # sections follow the page
    assert sync_confluence(FakeWriter([{"id": "confluence:KGT:3", "acl": []}]),
                           fake_atlassian("CONFLUENCE")[0], "KGT", dry_run=True)["acls"]["confluence:KGT:3"] == ["confluence-group:eng"]


# ---- review ---------------------------------------------------------------------------------------

def test_corrections_from_proposals():
    from kg_pipeline.review import corrections_for, decision_acl

    def one(p, **kw):
        decision, rows = corrections_for(p, "sam", **kw)
        assert decision is None and len(rows) == 1
        return rows[0]
    edge = {"id": "proposal:ab12", "kind": "wrong_fact", "target_id": "r:a.py#f", "edge_type": "REALISES",
            "other_id": "domain:r:invite", "text": "f does not invite", "author": "alice"}
    assert one(edge, now="2026-11-04T10:00:00+00:00") == {
        "id": "correction:ab12", "kind": "suppress_edge", "target_id": "r:a.py#f", "edge_type": "REALISES",
        "other_id": "domain:r:invite", "text": "f does not invite", "author": "alice",
        "created_at": "2026-11-04T10:00:00+00:00", "status": "approved", "proposal_id": "proposal:ab12",
        "approved_by": "sam", "approved_at": "2026-11-04T10:00:00+00:00"}
    node = one({"id": "proposal:1", "kind": "wrong_fact", "target_id": "jira:PREC-1", "text": "old"})
    assert node["kind"] == "mark_outdated" and "edge_type" not in node
    link = one({"id": "proposal:2", "kind": "link", "target_id": "r:a.py#f", "edge_type": "REALISES", "other_id": "domain:r:x"})
    assert link["kind"] == "add_fact" and link["other_id"] == "domain:r:x"
    assert one({"id": "proposal:4", "kind": "feedback", "feedback_kind": "outdated", "target_id": "x"})["kind"] == "mark_outdated"
    assert one({"id": "proposal:4", "kind": "feedback", "feedback_kind": "missing", "target_id": "x"},
               kind="mark_outdated", text="gone")["text"] == "gone"

    decision, rows = corrections_for({"id": "proposal:3", "kind": "decision", "title": "PUT", "text": "Use PUT",
                                      "links": ["a", "b"], "author": "alice"}, "sam", text="Use PUT for updates")
    assert decision["id"] == "decision:3" and decision["text"] == "Use PUT for updates" and decision["title"] == "PUT"
    assert [(r["id"], r["target_id"], r["edge_type"], r["other_id"]) for r in rows] == [
        ("correction:3:0", "decision:3", "ABOUT", "a"), ("correction:3:1", "decision:3", "ABOUT", "b")]
    assert corrections_for({"id": "proposal:5", "kind": "decision", "text": "t"}, "sam", target="r:a.py")[1][0]["other_id"] == "r:a.py"

    for p, kw in [({"id": "proposal:5", "kind": "decision", "text": "t"}, {}),                  # no target
                  (node | {"id": "proposal:6"}, {"kind": "suppress_edge"}),                     # not an edge
                  (node | {"id": "proposal:6"}, {"kind": "add_fact"}),                          # add_fact needs an edge
                  ({"id": "proposal:7", "kind": "feedback", "feedback_kind": "helpful", "target_id": "x"}, {}),
                  (edge, {"kind": "delete_everything"})]:
        with pytest.raises(ValueError):
            corrections_for(p, "sam", **kw)

    answer = {"id": "proposal:8", "kind": "feedback", "feedback_kind": "wrong", "subject": "answer",
              "question": "Who approves invites?", "text": "wrong owner"}
    with pytest.raises(ValueError, match="answer feedback on 'Who approves invites\?' names no node"):
        corrections_for(answer, "sam")
    assert one(answer, target="r:a.py#f")["target_id"] == "r:a.py#f"
    from kg_pipeline.review import _table
    assert "answer to: Who approves invites?" in _table([answer])

    assert decision_acl([{"acl": ["jira:PREC"]}, {"repo": "precept"}]) == ["jira:PREC", "repo:precept"]
    assert decision_acl([{"acl": ["jira:PREC"]}, {"acl": None, "repo": None}]) is None          # one public node


def test_review_cli_approve_and_reject():
    from kg_pipeline import review
    open_p = {"id": "proposal:ab12", "kind": "link", "target_id": "a", "edge_type": "CALLS", "other_id": "b",
              "status": "open", "author": "alice"}
    w = FakeWriter([{"p": open_p}])
    c = review.approve(w, "proposal:ab12", "sam")
    q, params = w.calls[-1]
    assert "MERGE (c:Correction {id: row.id})" in q and "p.status = 'approved'" in q and params["rows"] == [c]
    assert review.reject(FakeWriter([{"p": open_p}]), "proposal:ab12", "sam", "no")["status"] == "rejected"
    with pytest.raises(SystemExit):
        review.approve(FakeWriter([{"p": open_p | {"status": "approved"}}]), "proposal:ab12", "sam")
    with pytest.raises(SystemExit):
        review.reject(FakeWriter([]), "proposal:nope", "sam")


def test_upsert_keeps_the_acl_written_by_the_permission_sync(monkeypatch):
    """kg-jira / kg-docs write the coarse project or space acl; a node that kg-permissions narrowed keeps its acl."""
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter
    w = Neo4jWriter.__new__(Neo4jWriter)
    sent = []
    monkeypatch.setattr(Neo4jWriter, "ensure_schema", lambda self: None)
    monkeypatch.setattr(Neo4jWriter, "_run", lambda self, q, **p: sent.append((q, p)))
    w.upsert(("Ticket",), [{"id": "jira:PREC-1", "title": "t", "acl": ["jira:PREC"]}])
    q, p = sent[0]
    assert p["rows"][0]["props"]["acl"] == ["jira:PREC"]
    # the old acl and its sync stamp are read before SET n = row.props wipes them ...
    head, _, tail = q.partition("SET n = row.props")
    assert "n.acl_synced_at AS acl_synced_at" in head and "n.acl AS synced_acl" in head
    assert "n.embedding AS embedding" in head                                    # embeddings still kept
    # ... the stamp is restored, and the row's acl is used only for nodes the permission sync never wrote
    assert "n.acl_synced_at = acl_synced_at" in tail
    assert tail.strip().endswith("n.acl = CASE WHEN acl_synced_at IS NULL THEN row.props.acl ELSE synced_acl END")


def test_document_sections_take_the_documents_stored_acl():
    """A Confluence page restricted by kg-permissions: re-ingest must not give its sections the space acl."""
    from kg_pipeline.sources import documents
    restricted = ["confluence-group:eng"]
    upserts = []

    class W(FakeWriter):
        def upsert(self, labels, rows):
            upserts.append((labels, rows))

        def link(self, *a, **k):
            return 0

    doc = {"id": "confluence:ENG:7", "title": "Secret", "source": "confluence", "acl": ["confluence:ENG"]}
    # MATCH (d:Document {id}) RETURN d.acl answers what upsert kept: the permission-synced acl
    md = "# Secret\n\nthe plan for the next quarter\n\n## More\n\nwho is going to do which part"
    assert documents._write_document(W([{"acl": restricted}]), doc, md) == 2
    assert upserts[0] == (("Document",), [doc])
    assert upserts[1][0] == ("Section",) and all(r["acl"] == restricted for r in upserts[1][1])
    upserts.clear()     # a document never synced: sections get the ingest acl
    documents._write_document(W([{"acl": None}]), doc, md)
    assert upserts[1][1] and all(r["acl"] == ["confluence:ENG"] for r in upserts[1][1])


@pytest.mark.skipif(not os.environ.get("NEO4J_PASSWORD"), reason="NEO4J_PASSWORD not set")
def test_permissions_and_review_in_neo4j(monkeypatch):
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter
    from kg_pipeline.review import approve, list_proposals, reject
    from kg_pipeline.sources.permissions import sync_confluence, sync_jira
    monkeypatch.delenv("KG_ACL_DEFAULT", raising=False)
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), "neo4j", os.environ["NEO4J_PASSWORD"]) as w:
        try:
            w.upsert(("Ticket",), [{"id": f"jira:KGT-{i}", "key": f"KGT-{i}", "project": "KGT", "acl": ["jira:KGT"]} for i in (1, 2, 3)])
            w.upsert(("Document",), [{"id": "confluence:KGT:3", "collection": "confluence:KGT", "acl": ["confluence:KGT"]}])
            w.upsert(("Section",), [{"id": "confluence:KGT:3#s0", "acl": ["confluence:KGT"]}])
            w.link("PART_OF", [{"src": "confluence:KGT:3#s0", "dst": "confluence:KGT:3"}])
            assert sync_jira(w, fake_atlassian()[0], "KGT")["written"] == 3
            assert sync_jira(w, fake_atlassian()[0], "KGT")["written"] == 0                     # incremental
            acl = w._run("MATCH (t:Ticket {id: 'jira:KGT-3'}) RETURN t.acl AS a").records[0]["a"]
            assert "jira:KGT" not in acl and "jira-group:security" in acl
            w.upsert(("Ticket",), [{"id": "jira:KGT-3", "key": "KGT-3", "project": "KGT", "acl": ["jira:KGT"]}])
            assert w._run("MATCH (t:Ticket {id: 'jira:KGT-3'}) RETURN t.acl AS a").records[0]["a"] == acl  # kg-jira re-run
            assert sync_confluence(w, fake_atlassian("CONFLUENCE")[0], "KGT")["written"] == 1
            sec = w._run("MATCH (s:Section {id: 'confluence:KGT:3#s0'}) RETURN s.acl AS a").records[0]["a"]
            assert sec == ["confluence-group:eng"]

            # the MCP server writes proposals like these
            for p in ({"id": "proposal:kgt1", "kind": "wrong_fact", "target_id": "jira:KGT-1", "text": "outdated",
                       "author": "alice", "created_at": "2026-11-04T10:00:00Z"},
                      {"id": "proposal:kgt2", "kind": "link", "target_id": "jira:KGT-1", "edge_type": "LINKS",
                       "other_id": "jira:KGT-2", "author": "bob", "created_at": "2026-11-04T11:00:00Z"},
                      {"id": "proposal:kgt3", "kind": "decision", "text": "Keep KGT-1 open", "author": "alice",
                       "links": ["jira:KGT-1", "confluence:KGT:3#s0"], "created_at": "2026-11-04T12:00:00Z"}):
                w._run("CREATE (p:Proposal) SET p = $p", p=p | {"status": "open", "content_hash": p["id"]})
            assert [p["id"] for p in list_proposals(w) if p["id"].startswith("proposal:kgt")] == [
                "proposal:kgt3", "proposal:kgt2", "proposal:kgt1"]
            assert approve(w, "proposal:kgt1", "sam")["kind"] == "mark_outdated"
            row = w._run("MATCH (p:Proposal {id: 'proposal:kgt1'}) MATCH (c:Correction {id: p.correction_ids[0]}) "
                         "RETURN p.status AS s, c.target_id AS t, c.status AS cs").records[0]
            assert (row["s"], row["t"], row["cs"]) == ("approved", "jira:KGT-1", "approved")
            res = approve(w, "proposal:kgt3", "sam")
            assert len(res["corrections"]) == 2
            d = w._run("MATCH (d:Source:Decision {id: 'decision:kgt3'}) RETURN d.acl AS acl").records[0]["acl"]
            kgt1 = w._run("MATCH (t:Ticket {id: 'jira:KGT-1'}) RETURN t.acl AS a").records[0]["a"]
            assert d == sorted(set(kgt1) | {"confluence-group:eng"})                          # visible to readers of either
            reject(w, "proposal:kgt2", "sam", "not related")
            assert not [p for p in list_proposals(w) if p["id"].startswith("proposal:kgt")]
        finally:
            w._run("MATCH (n:Source) WHERE n.id STARTS WITH 'jira:KGT' OR n.id STARTS WITH 'confluence:KGT:' "
                   "OR n.id = 'decision:kgt3' DETACH DELETE n")
            w._run("MATCH (n) WHERE (n:Proposal OR n:Correction) AND n.id CONTAINS ':kgt' DETACH DELETE n")
