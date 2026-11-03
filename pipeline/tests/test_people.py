import json
import os
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

FIX = Path(__file__).parent / "fixtures"
GH = FIX / "github"
CATALOG = FIX / "catalog" / "repo"


# ---- GitHub ---------------------------------------------------------------------------------------------
def fake_github(rate_limit_once: bool = False):
    from kg_pipeline.sources.github import GitHub
    calls, slept, state = [], [], {"limited": rate_limit_once}

    def fetch(url, headers):
        calls.append(url)
        u = urlparse(url)
        if state["limited"]:
            state["limited"] = False
            return 403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1005"}, None
        parts = u.path.split("/")          # /repos/acme/shop/pulls[/42/commits]
        if parts[-1] == "pulls":
            if parse_qs(u.query).get("page") == ["2"]:
                return 200, {}, json.loads((GH / "pulls-2.json").read_text())
            nxt = f'<https://api.github.com{u.path}?per_page=100&state=all&page=2>; rel="next", <x>; rel="last"'
            return 200, {"link": nxt}, json.loads((GH / "pulls-1.json").read_text())
        f = GH / f"pr{parts[-2]}-{parts[-1]}.json"
        return 200, {}, json.loads(f.read_text()) if f.exists() else []
    api = GitHub(token="t", fetch=fetch, sleep=slept.append, clock=lambda: 1000.0)
    return api, calls, slept


def test_github_pagination_incremental_and_rate_limit():
    from kg_pipeline.sources.github import GitHubForge
    api, calls, slept = fake_github(rate_limit_once=True)
    changes = list(GitHubForge(api, "acme/shop").changes(None))
    assert [c["number"] for c in changes] == [42, 41, 40]                    # second page followed
    assert slept == [5.0]                                                     # waited until X-RateLimit-Reset
    assert "Bearer t" not in "".join(calls)                                   # token only in headers
    api, _, _ = fake_github()
    since = list(GitHubForge(api, "acme/shop").changes("2026-09-01T00:00:00Z"))
    assert [c["number"] for c in since] == [42, 41]                           # stops at the cursor
    pr = since[0]
    assert pr["state"] == "merged" and pr["merge_sha"].startswith("m42") and len(pr["commits"]) == 2
    assert pr["comments"][1]["outdated"] and pr["comments"][1]["line"] == 3
    with pytest.raises(ValueError):
        GitHubForge(api, "acme/shop/../x")


def test_github_rate_limit_too_long_fails():
    from kg_pipeline.sources.github import GitHub
    api = GitHub(token="t", fetch=lambda u, h: (429, {"retry-after": "3600"}, None), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="rate limit"):
        api.request("https://api.github.com/x")
    api = GitHub(token="t", fetch=lambda u, h: (404, {}, None), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="HTTP 404"):
        api.request("https://api.github.com/x")


def test_github_rows_and_comment_targets():
    from kg_pipeline.people import email_hash
    from kg_pipeline.sources.github import GitHubForge, comment_targets, rows
    api, _, _ = fake_github()
    change = next(GitHubForge(api, "acme/shop").changes(None))
    r = rows(change, "acme/shop", "shop")
    pr = r["pr"][0]
    assert pr["id"] == "github:acme/shop#42" and pr["ticket_keys"] == ["KGT-2"] and pr["acl"] == ["repo:shop"]
    assert pr["commit_shas"][-1].startswith("m42") and len(pr["commit_shas"]) == 3          # commits + merge commit
    assert f"alice-gh|{email_hash('alice@example.com')}|Alice Smith" in pr["commit_identities"]
    assert "alice@example.com" not in json.dumps(r)                                          # no plain emails
    assert {m["dst"] for m in r["MODIFIES"]} == {"shop:services/cart/src/store.py", "shop:services/cart/src/test_store.py"}
    assert [m["change"] for m in r["MODIFIES"]] == ["M", "A"]
    assert len(r["reviews"]) == 2 and len(r["PART_OF"]) == 4
    symbols = {"services/cart/src/store.py": [
        {"id": "shop:services/cart/src/store.py#Store", "start": 1, "end": 40},
        {"id": "shop:services/cart/src/store.py#Store.save", "start": 10, "end": 20}]}
    on = comment_targets(r["comments"], symbols, "shop")
    assert {(o["src"].rsplit("/", 1)[1], o["dst"].rsplit("#", 1)[-1]) for o in on if o["method"] == "github-line"} == \
        {("7001", "Store.save")}                                                             # smallest symbol; outdated skipped
    assert sum(o["method"] == "github" for o in on) == 2


def test_github_repositories_from_env():
    from kg_pipeline.sources.github import repositories
    got = repositories("acme/shop=shop-main, acme/api", ["https://github.com/acme/web.git", "https://gitlab.com/a/b.git",
                                                        "git@github.com:acme/shop.git"])
    assert got == {"acme/shop": "shop-main", "acme/api": "api", "acme/web": "web"}


# ---- catalog --------------------------------------------------------------------------------------------
def test_entity_refs_and_codeowners():
    from kg_pipeline.sources.catalog import owners_of, parse_codeowners, ref, team_handle
    assert ref("team-a", "group") == "group:default/team-a"
    assert ref("api:other/Pay", "api") == "api:other/pay"
    rules = parse_codeowners((CATALOG / ".github" / "CODEOWNERS").read_text())
    assert owners_of("services/cart/src/store.py", rules) == (["@acme/team-checkout"], "/services/cart/")
    assert owners_of("services/cart/README.md", rules)[0] == ["@acme/docs"]                 # last match wins
    assert owners_of("lib/x.py", rules)[0] == ["@acme/platform"]
    assert owners_of("other/services/cart/x.py", rules)[0] == ["@acme/platform"]            # anchored to the root
    unanchored = parse_codeowners("docs/ @a/b\n*.py @c\n**/logs @d\n")
    assert owners_of("docs/a/b.txt", unanchored)[0] == ["@a/b"] and owners_of("x/y/z.py", unanchored)[0] == ["@c"]
    assert owners_of("a/b/logs/1.txt", unanchored)[0] == ["@d"] and owners_of("adocs/x", unanchored)[0] == []
    assert team_handle("@acme/Team-A") == "team-a" and team_handle("@carol") is None and team_handle("d@x.com") is None


def test_catalog_build():
    from kg_pipeline.people import email_hash
    from kg_pipeline.sources.catalog import build
    paths = ["services/cart/src/store.py", "services/cart/README.md", "services/payments/pay.py", "lib/x.py"]
    out = build(CATALOG, "shop", paths)
    nodes = {n["id"]: n for n in out["nodes"]}
    edges = {(e["type"], e["src"], e["dst"]) for e in out["edges"]}
    cart, team = "catalog:component:default/cart", "catalog:group:default/team-checkout"
    assert nodes[cart]["label"] == "Service" and nodes[cart]["system"] == "system:default/shop"
    assert nodes["catalog:api:default/cart-api"]["label"] == "API" and nodes[team]["label"] == "Team"
    assert {("OWNS", team, cart), ("PART_OF", cart, "catalog:system:default/shop"),
            ("PROVIDES_API", cart, "catalog:api:default/cart-api"),
            ("CONSUMES_API", cart, "catalog:api:default/payments-api"),
            ("DEPENDS_ON", cart, "catalog:component:default/payments"),
            ("OWNS", team, "catalog:system:default/shop"), ("OWNS", team, "catalog:api:default/cart-api"),
            ("PART_OF", team, "catalog:group:default/engineering"),
            ("IMPLEMENTED_IN", cart, "shop:services/cart/src/store.py"),
            ("OWNS", team, "shop:services/cart/src/store.py"),                         # CODEOWNERS team
            ("OWNS", "catalog:group:default/platform", "shop:lib/x.py")} <= edges
    assert not any(e[0] == "DEPENDS_ON" and "resource" in e[2] for e in edges)
    alice = nodes["catalog:user:default/alice"]
    assert alice["email_hash"] == email_hash("alice@example.com") and alice["github"] == "alice-gh"
    assert alice["member_of"] == [team] and nodes["catalog:user:default/dave"]["member_of"] == [team]
    assert nodes["catalog:user:default/dave"]["owns"] == ["catalog:component:default/payments"]
    worker = nodes["catalog:component:default/payments-worker"]
    assert worker["owner"] == "@carol" and worker["owner_source"] == "codeowners"           # fallback to CODEOWNERS
    assert "Alice@Example.com" not in json.dumps(out)
    assert json.loads(out["codeowners"]["rules"][1]) == ["/services/cart/", ["@acme/team-checkout"]]


# ---- people ---------------------------------------------------------------------------------------------
def test_identity_helpers():
    from kg_pipeline.people import email_hash, git_identity, jira_identity
    assert email_hash(" Alice@Example.COM ") == email_hash("alice@example.com") and email_hash("nobody") is None
    assert git_identity("A", "123+Alice-GH@users.noreply.github.com")["author_github"] == "alice-gh"
    assert git_identity("A", "a@b.c")["author_github"] is None
    ji = jira_identity({"assignee": {"accountId": "acc-1", "displayName": "Bob", "emailAddress": "bob@x.io"}, "reporter": None})
    assert ji["assignee_id"] == "acc-1" and ji["assignee_email_hash"] == email_hash("bob@x.io") and ji["reporter_id"] is None


def test_git_history_stores_email_hash_only(tmp_path):
    from kg_pipeline.people import email_hash
    from kg_pipeline.sources.history import git_log, parse
    from test_domain import _git_repo
    commits = parse(git_log(_git_repo(tmp_path), None, 10))
    assert commits[0]["author"] == "Dev" and commits[0]["author_email_hash"] == email_hash("dev@example.com")
    assert commits[0]["title"] == "Fix beta" and "dev@example.com" not in json.dumps(commits)


def _mentions():
    from kg_pipeline.people import email_hash as eh
    m = lambda keys, name, system, edge=None, acl=("repo:shop",): {"keys": keys, "name": name, "system": system,
                                                                  "edge": edge, "acl": list(acl)}
    return [
        m([f"email:{eh('alice@example.com')}"], "Alice Smith", "git", ("AUTHORED", "git:shop:c1", "Source", {})),
        m(["github:alice-gh"], "alice-gh", "github", ("AUTHORED", "github:acme/shop#42", "Source", {})),
        m(["github:alice-gh", f"email:{eh('alice@example.com')}"], "Alice Smith", "github"),      # PR commit identity
        m(["atlassian:acc-alice"], "Alice S.", "jira", ("ASSIGNED", "jira:KGT-2", "Source", {}), acl=("jira:KGT",)),
        m(["github:bob"], "bob", "github", ("REVIEWED", "github:acme/shop#42", "Source", {"state": "APPROVED"})),
        m([f"email:{eh('bob.jones@corp.io')}"], "Bob Jones", "git", ("AUTHORED", "git:shop:c2", "Source", {})),
        m(["atlassian:acc-bob"], "Bob Jones", "jira", ("REPORTED", "jira:KGT-2", "Source", {}), acl=("jira:KGT",)),
        m(["github:carol"], "carol", "github", ("AUTHORED", "github:acme/shop#41", "Source", {})),
        m([f"email:{eh('cw@corp.io')}"], "Carol W.", "git", ("AUTHORED", "git:shop:c3", "Source", {})),
    ]


def test_people_merge_rules():
    from kg_pipeline.people import MERGE_CONFIDENCE, NAME_ONLY_CONFIDENCE, load_aliases, resolve
    people, edges = resolve(_mentions(), [])
    by_gh = {g: p for p in people for g in p["github"]}
    # rule 1: the GitHub commit joins login and email; the Jira account stays apart without an alias
    assert by_gh["alice-gh"]["email_hashes"] and not by_gh["alice-gh"]["atlassian"]
    # rule 3: "Bob Jones" in git and Jira shares no key: a hint, never a merge
    bobs = [p for p in people if p["name"] == "Bob Jones"]
    assert len(bobs) == 2 and NAME_ONLY_CONFIDENCE < MERGE_CONFIDENCE
    hint = [e for e in edges if e["type"] == "MAYBE_SAME"]
    assert len(hint) == 1 and {hint[0]["src"], hint[0]["dst"]} == {b["id"] for b in bobs} and hint[0]["confidence"] == 0.6
    assert len(people) == 7

    # rule 2: aliases join the Jira account to Alice and the git name "Carol W." to the login carol
    people, edges = resolve(_mentions(), load_aliases(FIX / "people-aliases.yaml"))
    alice = next(p for p in people if p["id"] == "person:alice")
    assert alice["name"] == "Alice Smith" and alice["atlassian"] == ["acc-alice"] and alice["merge_rule"] == "alias"
    assert alice["acl"] == ["jira:KGT", "repo:shop"] and set(alice["systems"]) == {"git", "github", "jira"}
    assert {(e["type"], e["dst"]) for e in edges if e["src"] == "person:alice"} == {
        ("AUTHORED", "git:shop:c1"), ("AUTHORED", "github:acme/shop#42"), ("ASSIGNED", "jira:KGT-2")}
    carol = next(p for p in people if "carol" in p["github"])
    assert carol["name"] == "Carol White" and carol["email_hashes"]
    assert len(people) == 5
    assert all("@" not in json.dumps(p) for p in people)                          # no plain emails anywhere


# ---- Neo4j end to end -------------------------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("NEO4J_PASSWORD"), reason="NEO4J_PASSWORD not set")
def test_github_catalog_people_into_neo4j(tmp_path):
    from kg_pipeline.code.model import CodeGraph
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter
    from kg_pipeline.people import sync as people_sync
    from kg_pipeline.sources import catalog, github
    repo = "it-people"
    g = CodeGraph(repo)
    g.add_node(repo, ("Code", "Repository"), name=repo)
    for p in ["services/cart/src/store.py", "services/payments/pay.py", "lib/x.py"]:
        g.add_node(f"{repo}:{p}", ("Code", "File"), path=p)
        g.add_edge("CONTAINS", repo, f"{repo}:{p}", "parser")
    g.add_node(f"{repo}:services/cart/src/store.py#save", ("Code", "Symbol", "Function"), name="save", qualname="save",
               path="services/cart/src/store.py", start_line=10, end_line=20, kind="Function")
    g.add_edge("DEFINES", f"{repo}:services/cart/src/store.py", f"{repo}:services/cart/src/store.py#save", "parser")
    shutil.copytree(CATALOG, tmp_path / "repo")
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    with Neo4jWriter(uri, "neo4j", os.environ["NEO4J_PASSWORD"]) as w:
        try:
            w.write_delta(g)
            w.upsert(("Commit",), [{"id": f"git:{repo}:c1a0000000000000000000000000000000000001", "repo": repo,
                                    "sha": "c1a0000000000000000000000000000000000001", "author": "Alice Smith",
                                    "author_email_hash": __import__("kg_pipeline.people").people.email_hash("alice@example.com"),
                                    "title": "Upsert by id", "acl": [f"repo:{repo}"], "source": "git"}])
            w.upsert(("Ticket",), [{"id": "jira:KGP-2", "key": "KGT-2", "title": "Save", "acl": ["jira:KGP"],
                                    "assignee_id": "acc-alice", "assignee_name": "Alice S.", "source": "jira"}])
            api, _, _ = fake_github()
            res = github.sync(w, github.GitHubForge(api, "acme/shop"), "acme/shop", repo)
            assert res["pull_requests"] == 3 and res["contains"] == 1 and res["references"] >= 1
            assert w.state("github:acme/shop:updated") == "2026-10-02T12:00:00Z"
            on = w._run("MATCH (:ReviewComment)-[:ON]->(s:Symbol) RETURN s.name AS n").records
            assert [r["n"] for r in on] == ["save"]
            assert github.sync(w, github.GitHubForge(fake_github()[0], "acme/shop"), "acme/shop", repo)["pull_requests"] == 0

            res = catalog.sync(w, tmp_path / "repo", repo)
            assert res["service"] == 3 and res["team"] >= 3
            assert catalog.sync(w, tmp_path / "repo", repo) == {"repo": repo, "unchanged": True}

            res = people_sync(w, FIX / "people-aliases.yaml")
            assert res["written"] == res["people"] > 0
            assert people_sync(w, FIX / "people-aliases.yaml")["written"] == 0              # incremental
            alice = w._run("""MATCH (p:Person {id: 'person:alice'})-[r]->(x)
                              RETURN type(r) AS t, x.id AS id ORDER BY t, id""").records
            got = {(r["t"], r["id"]) for r in alice}
            assert {("AUTHORED", "github:acme/shop#42"), ("ASSIGNED", "jira:KGP-2"),
                    ("AUTHORED", f"git:{repo}:c1a0000000000000000000000000000000000001"),
                    ("MEMBER_OF", "catalog:group:default/team-checkout")} <= got
            owns = w._run("MATCH (p:Person)-[:OWNS]->(f:File {repo: $r}) RETURN p.name AS n, f.path AS f", r=repo).records
            assert {(r["n"], r["f"]) for r in owns} >= {("Carol White", "services/payments/pay.py")}
            assert w._run("MATCH (p:Person) WHERE any(k IN keys(p) WHERE toString(p[k]) CONTAINS '@example.com') "
                          "RETURN count(p) AS c").records[0]["c"] == 0
        finally:
            w._run("MATCH (n:Code {repo: $r}) DETACH DELETE n", r=repo)
            w._run("MATCH (n:Source) WHERE n.repo = $r OR n.id IN ['jira:KGP-2', 'codeowners:' + $r] "
                   "OR n.id STARTS WITH 'github:acme/shop' OR n:Person DETACH DELETE n", r=repo)
            w._run("MATCH (s:SyncState) WHERE s.key STARTS WITH 'github:acme/shop' DELETE s")
