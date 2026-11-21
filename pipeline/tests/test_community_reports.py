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


# ---- T11: report generation, budget, cache, fallback, embedding ------------------------------------------------

class World:
    """Stateful fake graph: communities with members, reports written by the code, embedding hashes."""

    def __init__(self, n=5, marker_at=None):
        self.communities = {}
        for i in range(n):
            cid = f"community:p:0:m{i}"
            body = f"{MARKER} body" if i == marker_at else f"plain body {i}"
            self.communities[cid] = {
                "level": 0, "acl": ["jira:PREC"] if i == marker_at else ["repo:shop"], "hash": f"h{i}",
                "members": [{"id": f"m{i}", "labels": ["Symbol"], "name": f"Thing{i}", "text_masked": body},
                            {"id": f"k{i}", "labels": ["Symbol"], "name": f"Other{i}", "text_masked": None}],
                "edges": [(f"m{i}", f"k{i}", "CALLS")], "summaries": {}}
        self.reports, self.queries = {}, []

    def _run(self, query, **p):
        self.queries.append((query, p))
        ids = p.get("ids", [])
        sel = {i: self.communities[i] for i in ids if i in self.communities}
        if "$level" in query:
            lv = p.get("level")
            return Result([{"cid": i} for i, c in sorted(self.communities.items()) if lv is None or c["level"] == lv])
        if "MERGE (r:CommunityReport" in query:
            for row in p["rows"]:
                old = self.reports.get(row["id"], {})
                self.reports[row["id"]] = {**old, **row}
            return Result([])
        if "SET n.embedding" in query:
            for row in p["rows"]:
                self.reports[row["id"]]["embedding_hash"] = row["hash"]
            return Result([])
        if "RETURN r.id AS rid" in query:
            return Result([{"rid": r["id"], "cid": r["community_id"], "content_hash": r["content_hash"],
                            "model": r["model"], "embedding_hash": r.get("embedding_hash"), "title": r["title"],
                            "summary": r["summary"], "findings": r["findings"], "rank": r["rank"],
                            "acl": r["acl"], "level": r["level"]}
                           for r in self.reports.values() if r["community_id"] in ids])
        if query.lstrip().startswith("CREATE"):
            return Result([])
        if "SUMMARIZES" in query:
            return Result([{"cid": i, "mid": m, "text": t} for i, c in sel.items() for m, t in c["summaries"].items()])
        if "type(r)" in query:
            return Result([{"cid": i, "src": s, "dst": d, "type": t} for i, c in sel.items() for s, d, t in c["edges"]])
        if "IN_COMMUNITY" in query:
            return Result([{"cid": i, "mid": m["id"], "labels": m["labels"], "name": m["name"],
                            "text_masked": m["text_masked"]} for i, c in sel.items() for m in c["members"]])
        return Result([{"cid": i, "level": c["level"], "size": len(c["members"]), "acl": c["acl"], "repo": None,
                        "content_hash": c["hash"]} for i, c in sel.items()])


class StubLLM:
    name = "stub:model"

    def __init__(self, fail=False):
        self.prompts, self.fail = [], fail

    def json(self, system, user, schema):
        self.prompts.append(user)
        if self.fail:
            raise RuntimeError("llm down")
        return {"title": "LLM title", "summary": "LLM summary", "findings": ["f1", "f2"], "rank": 42}


class StubEmb:
    model = "stub-emb"
    dimensions = 8

    def __init__(self):
        self.texts = []

    def documents(self, texts):
        self.texts += texts
        return [[0.1] * self.dimensions for _ in texts]


def test_a_no_llm_gives_extractive_report_per_community(monkeypatch):
    w = World(3)
    stats = cr.build_reports(w, None, None, budget=10)
    assert len(w.reports) == 3 and all(r["model"] == "extractive" for r in w.reports.values())
    assert all(r["title"] and r["summary"] and r["findings"] for r in w.reports.values())
    assert stats["extractive"] == 3 and stats["llm_calls"] == 0 and stats["written"] == 3
    monkeypatch.setenv("COMMUNITY_LLM", "off")
    llm = StubLLM()
    w2 = World(2)
    cr.build_reports(w2, llm, None, budget=10)
    assert llm.prompts == [] and all(r["model"] == "extractive" for r in w2.reports.values())


def test_b_llm_only_for_changed_hash_second_run_is_free(tmp_path):
    w, llm, emb = World(3), StubLLM(), StubEmb()
    s1 = cr.build_reports(w, llm, emb, budget=10, cache=tmp_path)
    assert s1["llm_calls"] == 3 and s1["written"] == 3 and s1["embedded"] == 3
    assert all(r["model"] == "stub:model" and r["rank"] == 10.0 for r in w.reports.values())   # rank clamped
    s2 = cr.build_reports(w, llm, emb, budget=10, cache=tmp_path)
    assert len(llm.prompts) == 3 and len(emb.texts) == 3
    assert s2["llm_calls"] == 0 and s2["embedded"] == 0 and s2["written"] == 0
    w.communities["community:p:0:m1"]["hash"] = "changed"
    w.communities["community:p:0:m1"]["members"][0]["name"] = "Renamed1"     # the prompt changes, so no cache hit
    s3 = cr.build_reports(w, llm, emb, budget=10, cache=tmp_path)
    assert s3["llm_calls"] == 1 and s3["written"] == 1 and len(llm.prompts) == 4
    assert s3["embedded"] == 0                                  # same report text, same embedding hash


def test_c_budget_then_later_run_replaces_extractive():
    w, llm = World(5), StubLLM()
    s1 = cr.build_reports(w, llm, None, budget=2)
    models = sorted(r["model"] for r in w.reports.values())
    assert models == ["extractive"] * 3 + ["stub:model"] * 2
    assert s1["llm_calls"] == 2 and s1["extractive"] == 3 and s1["over_budget"] == 3
    s2 = cr.build_reports(w, llm, None, budget=1)
    assert s2["llm_calls"] == 1 and s2["written"] == 1 and s2["over_budget"] == 2
    assert sorted(r["model"] for r in w.reports.values()).count("extractive") == 2


def test_d_llm_failure_falls_back_to_extractive():
    w, llm = World(2), StubLLM(fail=True)
    stats = cr.build_reports(w, llm, None, budget=10)
    assert stats["llm_failed"] == 2 and stats["extractive"] == 2 and stats["llm_calls"] == 0
    assert all(r["model"] == "extractive" and r["title"] for r in w.reports.values())


def test_e_no_prompt_of_another_community_has_the_marker():
    w, llm = World(4, marker_at=2), StubLLM()
    cr.build_reports(w, llm, None, budget=10)
    with_marker = [p for p in llm.prompts if MARKER in p]
    assert len(with_marker) == 1 and "community:p:0:m2" in with_marker[0]     # sanity: own prompt has it
    assert len(llm.prompts) == 4
    for p in llm.prompts:
        if "community:p:0:m2" not in p:
            assert MARKER not in p
    other = [r for r in w.reports.values() if r["community_id"] != "community:p:0:m2"]
    assert other and all(MARKER not in repr(r) for r in other)


def test_f_embedder_called_only_for_changed_text():
    w, emb = World(3), StubEmb()
    s1 = cr.build_reports(w, None, emb, budget=0)
    assert s1["embedded"] == 3 and len(emb.texts) == 3
    assert any("CREATE VECTOR INDEX community_report_embedding" in q and "8" in q for q, _ in w.queries)
    cr.build_reports(w, None, emb, budget=0)
    assert len(emb.texts) == 3
    w.communities["community:p:0:m0"]["members"][0]["name"] = "Renamed"
    s3 = cr.build_reports(w, None, emb, budget=0)
    assert s3["embedded"] == 1 and len(emb.texts) == 4 and "Renamed" in emb.texts[-1]


def test_g_report_acl_equals_community_acl_and_links_to_it():
    w = World(3, marker_at=1)
    cr.build_reports(w, None, None, budget=0)
    for cid, c in w.communities.items():
        rep = w.reports[f"report:{cid}"]
        assert rep["acl"] == c["acl"] and rep["level"] == c["level"] and rep["community_id"] == cid
    upserts = [q for q, _ in w.queries if "MERGE (r:CommunityReport" in q]
    assert upserts and all("REPORTS_ON" in q for q in upserts)


def test_level_filter_and_stats_keys():
    w = World(3)
    w.communities["community:p:0:m2"]["level"] = 1
    stats = cr.build_reports(w, None, None, level=0, budget=0)
    assert len(w.reports) == 2
    for k in ("llm_calls", "llm_cache_hits", "extractive", "over_budget", "embedded", "written", "llm_failed"):
        assert k in stats


def test_cache_hit_is_free(tmp_path):
    llm = StubLLM()
    cr.build_reports(World(2), llm, None, budget=5, cache=tmp_path)
    s = cr.build_reports(World(2), llm, None, budget=0, cache=tmp_path)    # fresh graph, warm cache
    assert s["llm_cache_hits"] == 2 and s["llm_calls"] == 0 and len(llm.prompts) == 2
