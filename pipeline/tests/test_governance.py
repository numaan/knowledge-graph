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
    from kg_pipeline.review import correction_for
    edge = {"id": "proposal:ab12", "kind": "wrong_fact", "target_id": "r:a.py#f", "edge_type": "REALISES",
            "other_id": "domain:r:invite", "text": "f does not invite", "author": "alice"}
    c = correction_for(edge, "sam", now="2026-11-04T10:00:00+00:00")
    assert c == {"id": "correction:ab12", "kind": "suppress_edge", "target_id": "r:a.py#f", "edge_type": "REALISES",
                 "other_id": "domain:r:invite", "text": "f does not invite", "author": "alice",
                 "created_at": "2026-11-04T10:00:00+00:00", "status": "active", "proposal_id": "proposal:ab12",
                 "approved_by": "sam", "approved_at": "2026-11-04T10:00:00+00:00"}
    node = correction_for({"id": "proposal:1", "kind": "wrong_fact", "target_id": "jira:PREC-1", "text": "old"}, "sam")
    assert node["kind"] == "mark_outdated" and "edge_type" not in node
    link = correction_for({"id": "proposal:2", "kind": "link", "target_id": "r:a.py#f", "edge_type": "REALISES",
                           "other_id": "domain:r:x"}, "sam")
    assert link["kind"] == "add_fact" and link["other_id"] == "domain:r:x"
    dec = correction_for({"id": "proposal:3", "kind": "decision", "text": "Use PUT", "links": ["a", "b"],
                          "target_id": "a"}, "sam", text="Use PUT for updates")
    assert dec["kind"] == "add_fact" and dec["text"] == "Use PUT for updates" and dec["links"] == ["a", "b"]
    fb = correction_for({"id": "proposal:4", "kind": "feedback", "feedback_kind": "outdated", "target_id": "x"}, "sam")
    assert fb["kind"] == "mark_outdated"
    assert correction_for({"id": "proposal:5", "kind": "decision", "text": "t"}, "sam", target="r:a.py")["target_id"] == "r:a.py"
    with pytest.raises(ValueError):
        correction_for({"id": "proposal:5", "kind": "decision", "text": "t"}, "sam")          # no target
    with pytest.raises(ValueError):
        correction_for(node | {"id": "proposal:6"}, "sam", kind="suppress_edge")              # not an edge
    with pytest.raises(ValueError):
        correction_for(edge, "sam", kind="delete_everything")


def test_review_cli_approve_and_reject():
    from kg_pipeline import review
    open_p = {"id": "proposal:ab12", "kind": "link", "target_id": "a", "edge_type": "CALLS", "other_id": "b",
              "status": "open", "author": "alice"}
    w = FakeWriter([{"p": open_p}])
    c = review.approve(w, "proposal:ab12", "sam")
    q, params = w.calls[-1]
    assert "MERGE (c:Correction {id: $c.id})" in q and "p.status = 'approved'" in q and params["c"] == c
    assert review.reject(FakeWriter([{"p": open_p}]), "proposal:ab12", "sam", "no")["status"] == "rejected"
    with pytest.raises(SystemExit):
        review.approve(FakeWriter([{"p": open_p | {"status": "approved"}}]), "proposal:ab12", "sam")
    with pytest.raises(SystemExit):
        review.reject(FakeWriter([]), "proposal:nope", "sam")


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
            assert sync_confluence(w, fake_atlassian("CONFLUENCE")[0], "KGT")["written"] == 1
            sec = w._run("MATCH (s:Section {id: 'confluence:KGT:3#s0'}) RETURN s.acl AS a").records[0]["a"]
            assert sec == ["confluence-group:eng"]

            # the MCP server writes proposals like this one
            w._run("CREATE (p:Proposal) SET p = $p", p={"id": "proposal:kgt1", "kind": "wrong_fact", "status": "open",
                   "target_id": "jira:KGT-1", "text": "outdated", "author": "alice", "created_at": "2026-11-04T10:00:00Z",
                   "content_hash": "kgt1"})
            w._run("CREATE (p:Proposal) SET p = $p", p={"id": "proposal:kgt2", "kind": "link", "status": "open",
                   "target_id": "jira:KGT-1", "edge_type": "LINKS", "other_id": "jira:KGT-2", "author": "bob",
                   "created_at": "2026-11-04T11:00:00Z", "content_hash": "kgt2"})
            assert [p["id"] for p in list_proposals(w) if p["id"].startswith("proposal:kgt")] == ["proposal:kgt2", "proposal:kgt1"]
            c = approve(w, "proposal:kgt1", "sam")
            assert c["kind"] == "mark_outdated"
            row = w._run("MATCH (p:Proposal {id: 'proposal:kgt1'}) MATCH (c:Correction {id: p.correction_id}) "
                         "RETURN p.status AS s, c.target_id AS t, c.status AS cs").records[0]
            assert (row["s"], row["t"], row["cs"]) == ("approved", "jira:KGT-1", "active")
            reject(w, "proposal:kgt2", "sam", "not related")
            assert not [p for p in list_proposals(w) if p["id"].startswith("proposal:kgt")]
        finally:
            w._run("MATCH (n:Source) WHERE n.id STARTS WITH 'jira:KGT' OR n.id STARTS WITH 'confluence:KGT:' DETACH DELETE n")
            w._run("MATCH (n) WHERE (n:Proposal OR n:Correction) AND (n.id ENDS WITH 'kgt1' OR n.id ENDS WITH 'kgt2') DETACH DELETE n")
