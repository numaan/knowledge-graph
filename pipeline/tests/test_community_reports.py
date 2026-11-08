"""Report input and extractive report of one community (T10): ACL-safe input, no raw text."""
from __future__ import annotations

import re

from kg_pipeline import communities as cm
from kg_pipeline import community_reports as cr

MARKER = "RESTRICTED-MARKER-7f3a"


class Rec(dict):
    def data(self):
        return dict(self)


class Result:
    def __init__(self, rows):
        self.records = [Rec(r) for r in rows]


# In-memory graph: community id -> members (dicts), edges and summaries. The fake answers each loader query
# from the `$ids` parameter only, like Neo4j would with the IN_COMMUNITY match.
COMMUNITIES = {
    "community:a:0:x1": {"level": 0, "acl": ["repo:bookshop"], "members": [
        {"id": "x1", "labels": ["Symbol", "Code"], "name": "OrderService", "text_masked": "Creates orders [email]",
         "text": "RAW-SECRET-TEXT order body"},
        {"id": "x2", "labels": ["Symbol", "Code"], "name": "OrderRepo", "text_masked": None, "text": "RAW-SECRET-TEXT repo"},
    ], "edges": [("x1", "x2", "CALLS")], "summaries": {"x1": "Service that creates and cancels orders."}},
    "community:b:0:r1": {"level": 0, "acl": ["jira:PREC"], "members": [
        {"id": "r1", "labels": ["Ticket"], "name": f"PREC-1 {MARKER}", "text_masked": f"{MARKER} masked body",
         "text": f"{MARKER} raw body"},
        {"id": "r2", "labels": ["Ticket"], "name": "PREC-2 audit", "text_masked": None, "text": None},
    ], "edges": [("r1", "r2", "REFERENCES")], "summaries": {"r1": f"Summary with {MARKER}"}},
}


def answer(query, params):
    ids = params.get("ids", [])
    sel = {i: COMMUNITIES[i] for i in ids if i in COMMUNITIES}
    if "(c:Community)" in query and "IN_COMMUNITY" not in query:
        return [{"cid": i, "level": c["level"], "size": len(c["members"]), "acl": c["acl"], "repo": None,
                 "content_hash": "h"}
                for i, c in sel.items()]
    if "SUMMARIZES" in query:
        return [{"cid": i, "mid": m, "text": t} for i, c in sel.items() for m, t in c["summaries"].items()]
    if "type(r)" in query:
        return [{"cid": i, "src": s, "dst": d, "type": t} for i, c in sel.items() for s, d, t in c["edges"]]
    if "IN_COMMUNITY" in query:
        return [{"cid": i, "mid": m["id"], "labels": m["labels"], "name": m["name"], "text_masked": m["text_masked"]}
                for i, c in sel.items() for m in c["members"]]
    return []


class FakeWriter:
    def __init__(self):
        self.queries = []

    def _run(self, query, **params):
        self.queries.append((query, params))
        return Result(answer(query, params))


def test_prompt_version_is_the_one_of_communities():
    assert cr.PROMPT_VERSION is cm.PROMPT_VERSION == "community-v1"


def test_loader_reads_members_only_through_in_community_and_never_raw_text():
    w = FakeWriter()
    data = cr.load_report_inputs(w, ["community:a:0:x1"])
    assert list(data) == ["community:a:0:x1"]
    got = data["community:a:0:x1"]
    assert got["community"]["acl"] == ["repo:bookshop"] and got["community"]["level"] == 0
    assert [m["id"] for m in got["members"]] == ["x1", "x2"]
    assert got["edges"] == [("x1", "x2", "CALLS", 1.0)]
    assert got["summaries"] == {"x1": "Service that creates and cancels orders."}
    for query, params in w.queries:
        assert params["ids"] == ["community:a:0:x1"]              # only the asked community
        assert "$ids" in query
        assert not re.search(r"\bm\.text\b(?!_)", query)          # raw text is never read
    member_q = [q for q, _ in w.queries if "text_masked" in q]
    assert member_q and all("IN_COMMUNITY" in q for q in member_q)
    for q, _ in w.queries:
        if "type(r)" in q:                                         # both ends in the same community
            assert q.count("IN_COMMUNITY") == 2
    assert "RAW-SECRET-TEXT" not in repr(data) and MARKER not in repr(data)


def test_loader_batches_and_returns_empty_for_no_ids():
    w = FakeWriter()
    assert cr.load_report_inputs(w, []) == {} and w.queries == []
    data = cr.load_report_inputs(w, list(COMMUNITIES))
    assert set(data) == set(COMMUNITIES)


def _inputs(cid):
    d = cr.load_report_inputs(FakeWriter(), [cid])[cid]
    return d["community"], d["members"], d["edges"], d["summaries"]


def test_context_uses_text_masked_and_summaries_not_raw_text():
    ctx = cr.report_context(*_inputs("community:a:0:x1"))
    assert "OrderService" in ctx and "Creates orders [email]" in ctx
    assert "Service that creates and cancels orders." in ctx
    assert "CALLS" in ctx
    assert "RAW-SECRET-TEXT" not in ctx
    # a member dict that carries raw text (a careless caller) still does not leak it
    community, members, edges, summaries = _inputs("community:a:0:x1")
    members[0]["text"] = "RAW-SECRET-TEXT again"
    assert "RAW-SECRET-TEXT" not in cr.report_context(community, members, edges, summaries)


def test_marker_never_reaches_the_report_text_of_another_community():
    w = FakeWriter()
    data = cr.load_report_inputs(w, list(COMMUNITIES))
    out = {}
    for cid, d in data.items():
        args = (d["community"], d["members"], d["edges"], d["summaries"])
        out[cid] = (cr.report_context(*args), cr.extractive_report(*args))
    # sanity: the marker is in its own community text (masked text and summary), so the check is real
    assert MARKER in out["community:b:0:r1"][0]
    other_ctx, other_rep = out["community:a:0:x1"]
    assert MARKER not in other_ctx and MARKER not in repr(other_rep)
    assert "RAW-SECRET-TEXT" not in repr(out) and f"{MARKER} raw body" not in repr(out)
    # the loader query of the unrestricted community never carried the restricted id
    assert all("community:b" not in repr(p) for q, p in w.queries if p.get("ids") == ["community:a:0:x1"])


def _community(n):
    members = [{"id": f"n{i}", "labels": ["Symbol"], "name": f"Name{i}", "text_masked": f"text {i}"} for i in range(n)]
    edges = [(f"n{i}", f"n{i + 1}", "CALLS", 1.0) for i in range(n - 1)]
    return {"id": f"community:z:0:n{0}", "level": 0, "acl": [], "size": n}, members, edges, {}


def test_extractive_report_one_node_and_fifty_nodes():
    for n in (1, 50):
        report = cr.extractive_report(*_community(n))
        assert report["title"].strip() and report["summary"].strip()
        assert isinstance(report["findings"], list) and report["findings"]
        assert all(isinstance(f, str) and f.strip() for f in report["findings"])
        assert isinstance(report["rank"], float) and 0.0 <= report["rank"] <= 10.0
        assert report["model"] == "extractive"
    small = cr.extractive_report(*_community(1))["rank"]
    big = cr.extractive_report(*_community(50))["rank"]
    assert big > small


def test_context_is_capped_and_lists_top_members_by_degree():
    community, members, edges, summaries = _community(400)
    hub = {"id": "hub", "labels": ["Symbol"], "name": "TheHub", "text_masked": "x"}
    members = members + [hub]
    edges = edges + [("hub", f"n{i}", "CALLS", 1.0) for i in range(300)]
    ctx = cr.report_context(community, members, edges, {m["id"]: "s" * 500 for m in members})
    assert len(ctx) <= cr.CONTEXT_CHARS
    assert "TheHub" in ctx and "Name399" not in ctx           # the hub is listed first; the tail is cut
    assert ctx.index("TheHub") < ctx.index("Name0")


def test_extractive_report_of_empty_member_list_is_still_valid():
    report = cr.extractive_report({"id": "community:e:0:q", "level": 0, "acl": []}, [], [], {})
    assert report["title"] and report["summary"] and report["findings"] and 0.0 <= report["rank"] <= 10.0
