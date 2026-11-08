"""Community detection: the pure-Python Louvain fallback."""

import random

from kg_pipeline.leiden_fallback import louvain


def _clique(adj, names, w=1.0):
    for a in names:
        for b in names:
            if a != b:
                adj.setdefault(a, {})[b] = w


def _two_cliques():
    adj = {}
    left = [f"a{i}" for i in range(6)]
    right = [f"b{i}" for i in range(6)]
    _clique(adj, left)
    _clique(adj, right)
    adj["a0"]["b0"] = 0.1
    adj["b0"]["a0"] = 0.1
    return adj, left, right


def test_two_cliques_split():
    adj, left, right = _two_cliques()
    part = louvain(adj, 1.0, 42)
    assert set(part) == set(adj)
    assert len({part[n] for n in left}) == 1
    assert len({part[n] for n in right}) == 1
    assert part[left[0]] != part[right[0]]


def test_same_seed_same_output():
    adj, _, _ = _two_cliques()
    assert louvain(adj, 1.0, 7) == louvain(adj, 1.0, 7)


def test_insertion_order_does_not_matter():
    adj, _, _ = _two_cliques()
    keys = list(adj)
    random.Random(1).shuffle(keys)
    shuffled = {k: dict(reversed(list(adj[k].items()))) for k in keys}
    assert louvain(adj, 1.0, 42) == louvain(shuffled, 1.0, 42)


def test_resolution_monotonic():
    adj, _, _ = _two_cliques()
    low = len(set(louvain(adj, 0.5, 42).values()))
    high = len(set(louvain(adj, 2.0, 42).values()))
    assert high >= low


def test_empty_and_isolated():
    assert louvain({}, 1.0, 42) == {}
    part = louvain({"x": {}, "y": {}, "z": {}}, 1.0, 42)
    assert set(part) == {"x", "y", "z"}
    assert len(set(part.values())) == 3


def test_self_loop_ignored():
    adj, _, _ = _two_cliques()
    base = louvain(adj, 1.0, 42)
    adj["a1"]["a1"] = 50.0
    assert louvain(adj, 1.0, 42) == base


def test_communities_are_connected():
    adj = {}
    _clique(adj, ["p0", "p1", "p2"])
    _clique(adj, ["q0", "q1", "q2"])
    part = louvain(adj, 0.0, 42)  # disconnected cliques never share a community
    assert part["p0"] != part["q0"]


def test_split_disconnected_directly():
    from kg_pipeline.leiden_fallback import _split_disconnected

    # nodes 0-1 and 2-3 are linked inside pairs only, yet all four share community 7
    g = [{1: 1.0}, {0: 1.0}, {3: 1.0}, {2: 1.0}]
    out = _split_disconnected(g, [7, 7, 7, 7])
    assert out[0] == out[1]
    assert out[2] == out[3]
    assert out[0] != out[2]


# ---- detect() ----

import logging  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402

from kg_pipeline import communities  # noqa: E402
from kg_pipeline.communities import detect  # noqa: E402


def _blocks():
    """Four 5-node cliques; A-B and C-D are tied tightly, B-C weakly."""
    nodes, edges = [], []
    names = {}
    for tag in "ABCD":
        names[tag] = [f"{tag}{i}" for i in range(5)]
        nodes += names[tag]
        edges += [(a, b, 1.0) for i, a in enumerate(names[tag]) for b in names[tag][i + 1:]]
    edges += [("A0", "B0", 0.6), ("A1", "B1", 0.6), ("C0", "D0", 0.6), ("C1", "D1", 0.6), ("B2", "C2", 0.1)]
    return nodes, edges, names


def _check_shape(d, nodes, names):
    assert len(d.levels) >= 2
    for lvl in d.levels:
        assert set(lvl) == set(nodes)  # one community per node per level
    assert len(d.child_of) == len(d.levels) - 1
    for k, mapping in enumerate(d.child_of):
        for n in nodes:  # level-k ids nest in their parents
            assert mapping[d.levels[k][n]] == d.levels[k + 1][n]
    fine = d.levels[0]
    assert len({fine[n] for n in names["A"]}) == 1
    assert fine[names["A"][0]] != fine[names["B"][0]]  # clusters differ at the finest level
    top = d.levels[1]
    assert top[names["A"][0]] == top[names["B"][0]]
    assert top[names["A"][0]] != top[names["C"][0]]


def test_detect_levels_fallback():
    nodes, edges, names = _blocks()
    d = detect(nodes, edges, resolution=1.0, levels=2, seed=42, backend="fallback")
    _check_shape(d, nodes, names)
    assert detect(nodes, edges, 1.0, 2, 42, "fallback") == d
    assert detect(list(reversed(nodes)), list(reversed(edges)), 1.0, 2, 42, "fallback") == d


def test_detect_resolution_monotonic():
    nodes, edges, _ = _blocks()
    low = len(set(detect(nodes, edges, 0.2, 1, 42, "fallback").levels[0].values()))
    high = len(set(detect(nodes, edges, 3.0, 1, 42, "fallback").levels[0].values()))
    assert high >= low


def test_detect_isolated_and_empty():
    d = detect(["x", "y"], [], levels=2, backend="fallback")
    assert len(d.levels) == 1
    assert len(set(d.levels[0].values())) == 2
    assert detect([], [], backend="fallback").levels == []


def test_detect_library_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "graspologic_native", None)
    with pytest.raises(ImportError, match="graphrag"):
        detect(["a", "b"], [("a", "b", 1.0)], backend="library")
    # auto falls back silently
    assert detect(["a", "b"], [("a", "b", 1.0)], backend="auto").levels


def test_detect_library():
    pytest.importorskip("graspologic_native")
    nodes, edges, names = _blocks()
    d = detect(nodes, edges, 1.0, 2, 42, "library")
    assert len(d.levels) >= 1
    for lvl in d.levels:
        assert set(lvl) == set(nodes)
    assert detect(nodes, edges, 1.0, 2, 42, "library") == d
    iso = detect(nodes + ["lonely"], edges, 1.0, 1, 42, "library")
    assert iso.levels[0]["lonely"] not in {iso.levels[0][n] for n in nodes}


def test_detect_caps(monkeypatch, caplog):
    nodes, edges, _ = _blocks()
    monkeypatch.setenv("KG_COMMUNITY_MAX_NODES", "5")
    with caplog.at_level(logging.WARNING):
        d = detect(nodes, edges, backend="fallback")
    assert d.skipped_too_large and d.levels == []
    assert "skipped" in caplog.text
    monkeypatch.delenv("KG_COMMUNITY_MAX_NODES")
    monkeypatch.setenv("KG_COMMUNITY_MAX_EDGES", "3")
    assert detect(nodes, edges, backend="fallback").skipped_too_large


def test_detect_no_repeated_level():
    nodes, edges = ["a", "b", "c"], [("a", "b", 1.0), ("b", "c", 1.0), ("a", "c", 1.0)]
    d = detect(nodes, edges, levels=4, backend="fallback")
    assert len(d.levels) == 1  # one community: nothing to merge
    two = [("a", "b", 1.0), ("c", "d", 1.0)]
    d = detect(list("abcd"), two, levels=4, backend="fallback")
    assert len(d.levels) == 1  # the coarse level would equal the fine one
    for i in range(len(d.levels) - 1):
        assert len(set(d.levels[i].values())) != len(set(d.levels[i + 1].values()))


def test_env_defaults(monkeypatch):
    for k in ("RESOLUTION", "LEVELS", "SEED", "MAX_NODES", "MAX_EDGES", "BACKEND"):
        monkeypatch.delenv(f"KG_COMMUNITY_{k}", raising=False)
    assert (communities.resolution_default(), communities.levels_default(), communities.seed_default()) == (1.0, 2, 42)
    assert (communities.max_nodes(), communities.max_edges(), communities.backend_default()) == (200000, 2000000, "auto")
    monkeypatch.setenv("KG_COMMUNITY_LEVELS", "9")
    assert communities.levels_default() == 4
    monkeypatch.setenv("KG_COMMUNITY_LEVELS", "0")
    assert communities.levels_default() == 1


from kg_pipeline.communities import partition_hash, partition_key  # noqa: E402


@pytest.mark.parametrize("node,key", [
    ({"acl": ["jira:PREC"]}, ("jira:PREC",)),
    ({"acl": ["confluence:ENG"]}, ("confluence:ENG",)),
    ({"acl": ["jira:PREC", "confluence:ENG"]}, ("confluence:ENG", "jira:PREC")),
    ({"acl": ["jira:PREC", "confluence:ENG", "jira:PREC"]}, ("confluence:ENG", "jira:PREC")),
    ({"repo": "bookshop"}, ("repo:bookshop",)),
    ({"repo": "bookshop", "acl": None}, ("repo:bookshop",)),
    ({"repo": "bookshop", "acl": []}, ("repo:bookshop",)),
    ({"repo": "bookshop", "acl": ["jira:PREC"]}, ("jira:PREC",)),
    ({}, ()),
    ({"repo": None, "acl": []}, ()),
    ({"repo": ""}, ()),
    ({"acl": ["*"]}, ("*",)),
    ({"acl": ["*", "jira:PREC"]}, ("*", "jira:PREC")),
    ({"repo": "bookshop", "acl": ["*"]}, ("*",)),
    ({"acl": [], "repo": None}, ()),
])
def test_partition_key_table(node, key):
    assert partition_key(node) == key


def test_partition_key_is_tuple_of_str():
    assert isinstance(partition_key({"acl": ["b", "a"]}), tuple)


def test_partition_hash_stable_and_distinct():
    keys = [(), ("repo:bookshop",), ("repo:other",), ("jira:PREC",), ("confluence:ENG", "jira:PREC")]
    hashes = [partition_hash(k) for k in keys]
    assert len(set(hashes)) == len(keys)  # repo: and public partitions never merge
    assert hashes == [partition_hash(k) for k in keys]
    assert partition_hash(("a", "b")) != partition_hash(("a,b",))
    assert all(h and ":" not in h for h in hashes)


from kg_pipeline.communities import EDGE_WEIGHTS, project  # noqa: E402


def _n(id, label, **kw):
    return {"id": id, "labels": [label], **kw}


def _r(src, typ, dst):
    return {"src": src, "type": typ, "dst": dst}


def _graph():
    nodes = [
        _n("b:f1", "File", repo="bookshop"), _n("b:s1", "Symbol", repo="bookshop"),
        _n("o:f1", "File", repo="other"),
        _n("t1", "Ticket", acl=["jira:PREC"]), _n("t2", "Ticket", acl=["jira:PREC"]),
        _n("d1", "Document", acl=["confluence:ENG"]),
        _n("m1", "Document", acl=["jira:PREC", "confluence:ENG"]),
        _n("c1", "DomainConcept"), _n("c2", "BusinessRule"),
        _n("k1", "Chunk", acl=["confluence:ENG"]), _n("p1", "Person"), _n("fo", "Folder"),
        _n("cm", "Commit", repo="bookshop"), _n("cr", "Community"), _n("rp", "CommunityReport"),
        _n("sm", "Summary", repo="bookshop"),
    ]
    rels = [
        _r("b:f1", "DEFINES", "b:s1"),
        _r("b:f1", "IMPORTS", "o:f1"),      # crosses repos
        _r("t1", "CHILD_OF", "t2"),
        _r("t1", "MENTIONS", "c1"),         # crosses to public
        _r("d1", "MENTIONS", "m1"),         # crosses acl sets
        _r("c1", "USES_CONCEPT", "c2"),
        _r("k1", "PART_OF", "d1"), _r("k1", "MENTIONS", "c1"),
        _r("p1", "CALLS", "b:f1"), _r("fo", "CONTAINS", "b:f1"), _r("cm", "REFERENCES", "t1"),
        _r("b:f1", "CALLS", "gone"),
    ]
    return nodes, rels


def test_project_partitions_have_no_cross_edges_and_cover_every_member_once():
    nodes, rels = _graph()
    out = project(nodes, rels, [])
    seen = [n for ns, _ in out.values() for n in ns]
    assert sorted(seen) == sorted(["b:f1", "b:s1", "o:f1", "t1", "t2", "d1", "m1", "c1", "c2"])
    assert len(seen) == len(set(seen))
    assert set(out) == {("repo:bookshop",), ("repo:other",), ("jira:PREC",), ("confluence:ENG",),
                        ("confluence:ENG", "jira:PREC"), ()}
    for key, (ns, edges) in out.items():
        for u, v, w in edges:
            assert u in ns and v in ns and u < v and w > 0
    assert out[("repo:bookshop",)][1] == [("b:f1", "b:s1", EDGE_WEIGHTS["DEFINES"])]
    assert out[("jira:PREC",)][1] == [("t1", "t2", EDGE_WEIGHTS["CHILD_OF"])]
    assert out[()][1] == [("c1", "c2", EDGE_WEIGHTS["USES_CONCEPT"])]


def test_project_calls_and_reverse_calls_sum_once():
    nodes = [_n("a", "Symbol", repo="r"), _n("b", "Symbol", repo="r")]
    out = project(nodes, [_r("a", "CALLS", "b"), _r("b", "CALLS", "a")], [])
    edges = out[("repo:r",)][1]
    assert edges == [("a", "b", 2.0)]  # one entry per pair: detect() does not sum it again
    det = detect(["a", "b"], edges, backend="fallback")
    assert det.levels[0]["a"] == det.levels[0]["b"]
    assert communities._clean_edges(set(), edges) == {("a", "b"): 2.0}


def test_project_cochange_weights():
    nodes = [_n("a", "File", repo="r"), _n("b", "File", repo="r"), _n("c", "File", repo="r")]
    three = [["a", "b"]] * 3
    assert project(nodes, [], three)[("repo:r",)][1] == [("a", "b", pytest.approx(0.6))]
    six = [["a", "b", "c"]] * 3 + [["b", "a"]] * 3
    edges = dict(((u, v), w) for u, v, w in project(nodes, [], six)[("repo:r",)][1])
    assert edges[("a", "b")] == 1.0
    assert edges[("a", "c")] == pytest.approx(0.6)
    # co-change adds to a structural edge of the same pair
    both = project(nodes, [_r("a", "CALLS", "b")], three)[("repo:r",)][1]
    assert both == [("a", "b", pytest.approx(1.6))]


def test_project_cochange_optional_and_ignores_foreign_ids():
    nodes = [_n("a", "File", repo="r"), _n("b", "File", repo="s")]
    assert project(nodes, [], None)[("repo:r",)] == (["a"], [])
    out = project(nodes, [], [["a", "b", "zzz", "a"]])
    assert out[("repo:r",)][1] == [] and out[("repo:s",)][1] == []


def test_project_chunk_folds_into_owner():
    nodes = [_n("d", "Document", acl=["x"]), _n("c", "Concept"), _n("k", "Chunk", acl=["x"])]
    nodes[1]["labels"] = ["DomainConcept"]
    rels = [_r("k", "PART_OF", "d"), _r("k", "MENTIONS", "c")]
    out = project(nodes, rels, [])
    assert out[("x",)] == (["d"], [])  # concept is public: crosses partitions, dropped
    nodes[1]["acl"] = ["x"]
    out = project(nodes, rels, [])
    assert out[("x",)][1] == [("c", "d", EDGE_WEIGHTS["MENTIONS"])]


def test_edge_weights_table():
    assert EDGE_WEIGHTS["CALLS"] == 1.0 and EDGE_WEIGHTS["IMPORTS"] == 0.5
    assert EDGE_WEIGHTS["DEFINES"] == EDGE_WEIGHTS["CONTAINS"] == 0.3
    assert EDGE_WEIGHTS["MENTIONS"] == 0.7
    assert "MODIFIES" not in EDGE_WEIGHTS  # co-change comes from the cochange rows
    for t in ("CALLS", "IMPORTS", "EXTENDS", "IMPLEMENTS", "OVERRIDES", "USES_TYPE", "DEFINES", "CONTAINS",
              "REALISES", "IMPLEMENTED_BY", "COVERS", "USES_CONCEPT", "MENTIONS", "PART_OF", "CHILD_OF"):
        assert t in EDGE_WEIGHTS


# ---- load_partitions (Neo4j loader, faked) ---------------------------------------------------------------
import re  # noqa: E402


class _Rec(dict):
    def data(self):
        return dict(self)


class _Result:
    def __init__(self, rows):
        self.records = [_Rec(r) for r in rows]


class LoaderWriter:
    """Interprets the three Cypher shapes of load_partitions over an in-memory graph."""

    def __init__(self, nodes, rels, commits=()):
        self.nodes, self.rels, self.commits = sorted(nodes, key=lambda n: n["id"]), rels, sorted(commits)
        self.queries = []

    def _run(self, query, **params):
        self.queries.append((query, params))
        if "RETURN n.id AS id" in query:
            skip = set(params["skip"])
            page = [n for n in self.nodes if n["id"] > params["after"] and not skip & set(n["labels"])]
            return _Result([{"id": n["id"], "labels": n["labels"], "acl": n.get("acl"), "repo": n.get("repo")}
                            for n in page[:params["limit"]]])
        if "MODIFIES" in query:
            page = [c for c in self.commits if c[0] > params["after"]][:params["limit"]]
            return _Result([{"id": cid, "files": files[:50]} for cid, files in page if len(files[:50]) > 1])
        typ = re.search(r"\[r:(\w+)\]", query).group(1)
        ids = set(params["ids"])
        return _Result([{"src": r["src"], "type": r["type"], "dst": r["dst"]}
                        for r in self.rels if r["type"] == typ and r["src"] in ids])

    def edge_queries(self):
        return [(q, p) for q, p in self.queries if "[r:" in q]


def _loader_graph():
    nodes = [_n(f"a{i}", "Symbol", repo="big") for i in range(6)]
    nodes += [_n("x1", "Symbol", repo="small"), _n("x2", "Symbol", repo="small"), _n("cm", "Commit", repo="small"),
              _n("d", "Document", acl=["g"]), _n("k", "Chunk", acl=["g"]), _n("c", "DomainConcept", acl=["g"])]
    rels = [_r("a0", "CALLS", "a1"), _r("x1", "CALLS", "x2"), _r("k", "PART_OF", "d"), _r("k", "MENTIONS", "c"),
            _r("x1", "OWNS", "x2")]
    return nodes, rels


def test_load_partitions_skips_oversize_before_edges(monkeypatch, caplog):
    monkeypatch.setenv("KG_COMMUNITY_MAX_NODES", "5")
    nodes, rels = _loader_graph()
    w = LoaderWriter(nodes, rels)
    with caplog.at_level(logging.WARNING):
        parts, stats = communities.load_partitions(w)
    assert stats["skipped_too_large"] == 1 and stats["partitions"] == 2
    assert ("repo:big",) not in parts
    assert parts[("repo:small",)] == (["x1", "x2"], [("x1", "x2", 1.0)])
    assert parts[("g",)] == (["c", "d"], [("c", "d", EDGE_WEIGHTS["MENTIONS"])])  # chunk folded into d
    assert any("repo:big" in m for m in caplog.messages)
    for _, p in w.edge_queries():  # no edge query ever names a node of the skipped partition
        assert not any(i.startswith("a") for i in p["ids"])


def test_load_partitions_cypher_uses_only_edge_weight_types():
    nodes, rels = _loader_graph()
    w = LoaderWriter(nodes, rels)
    communities.load_partitions(w)
    types = {re.search(r"\[r:(\w+)\]", q).group(1) for q, _ in w.edge_queries()}
    assert types == set(EDGE_WEIGHTS)  # one query shape per relationship type, every weighted type
    assert "OWNS" not in types and "MODIFIES" not in types
    assert all("MATCH (a)-[r:" in q for q, _ in w.edge_queries())


def test_load_partitions_keyset_paging(monkeypatch):
    monkeypatch.setattr(communities, "PAGE", 4)
    nodes = [_n(f"n{i:02d}", "Symbol", repo="r") for i in range(10)]
    w = LoaderWriter(nodes, [])
    parts, _ = communities.load_partitions(w)
    assert parts[("repo:r",)][0] == [f"n{i:02d}" for i in range(10)]
    pages = [p for q, p in w.queries if "RETURN n.id AS id" in q]
    assert [p["after"] for p in pages] == ["", "n03", "n07"]
    assert all("SKIP" not in q.upper().split() for q, _ in w.queries)


def test_load_partitions_cochange_capped_at_50_files():
    files = [f"f{i:03d}" for i in range(200)]
    nodes = [_n(f, "File", repo="r") for f in files]
    w = LoaderWriter(nodes, [], [("c1", files), ("c2", files[:2])])
    parts, _ = communities.load_partitions(w)
    edges = dict(((u, v), x) for u, v, x in parts[("repo:r",)][1])
    assert ("f000", "f001") in edges and edges[("f000", "f001")] == pytest.approx(0.4)  # two commits
    assert ("f000", "f049") in edges and ("f000", "f050") not in edges  # the 51st file is cut
    q = next(q for q, _ in w.queries if "MODIFIES" in q)
    assert "[..50]" in q and "Commit|PullRequest" in q


def test_load_partitions_edge_cap_skips_partition(monkeypatch):
    monkeypatch.setenv("KG_COMMUNITY_MAX_EDGES", "1")
    nodes = [_n(i, "Symbol", repo="r") for i in ("a", "b", "c")]
    rels = [_r("a", "CALLS", "b"), _r("b", "CALLS", "c")]
    parts, stats = communities.load_partitions(LoaderWriter(nodes, rels))
    assert parts == {} and stats["skipped_too_large"] == 1


def test_extra_semantic_edge_weights():
    for t in ("REFERENCES", "LINKS", "IMPLEMENTED_IN", "DEPENDS_ON"):
        assert EDGE_WEIGHTS[t] == 0.5
    for t in ("OWNS", "IN_RELEASE", "PROVIDES_API", "MODIFIES"):
        assert t not in EDGE_WEIGHTS
    nodes = [_n("t1", "Ticket", acl=["j"]), _n("t2", "Ticket", acl=["j"])]
    assert project(nodes, [_r("t1", "LINKS", "t2")], [])[("j",)][1] == [("t1", "t2", 0.5)]


from kg_pipeline.communities import community_id, content_hash, reuse_ids  # noqa: E402


def test_community_id_uses_smallest_member_in_any_order():
    a = community_id("abc", 1, ["m3", "m1", "m2"])
    assert a == "community:abc:1:m1"
    assert a == community_id("abc", 1, ["m2", "m3", "m1"])
    assert community_id("abc", 0, ["m1"]) != a


def test_content_hash_order_free_and_sensitive():
    base = content_hash(["a", "b"], {"a": "h1", "b": "h2"}, "community-v1")
    assert base == content_hash(["b", "a"], {"b": "h2", "a": "h1"}, "community-v1")
    assert base != content_hash(["a", "b", "c"], {"a": "h1", "b": "h2"}, "community-v1")  # member added
    assert base != content_hash(["a", "b"], {"a": "h1", "b": "CHANGED"}, "community-v1")  # summary changed
    assert base != content_hash(["a", "b"], {"a": "h1", "b": "h2"}, "community-v2")  # prompt changed
    assert base != content_hash(["a", "b"], {"a": "h1"}, "community-v1")  # hash absent vs present


def test_content_hash_ignores_edge_weights_by_signature():
    import inspect
    assert list(inspect.signature(content_hash).parameters) == ["members", "member_hashes", "prompt_version"]


def test_member_added_keeps_id_when_anchor_unchanged_hash_changes():
    old = ["m1", "m2", "m3"]
    new = old + ["m9"]
    assert community_id("p", 0, old) == community_id("p", 0, new)
    assert content_hash(old, {}, "v") != content_hash(new, {}, "v")


def test_smaller_member_changes_id_unless_reused():
    old = {"community:p:0:m2": ["m2", "m3", "m4", "m5"]}
    new = {"community:p:0:m1": ["m1", "m2", "m3", "m4", "m5"]}
    assert community_id("p", 0, new["community:p:0:m1"]) != "community:p:0:m2"
    assert reuse_ids(new, old) == {"community:p:0:m1": "community:p:0:m2"}


def test_reuse_needs_overlap_above_half():
    old = {"community:p:0:m1": ["m1", "m2", "m3", "m4"]}
    exactly_half = {"community:p:0:a": ["m1", "m2", "x1", "x2"]}  # 2/4 = 0.5, not above
    assert reuse_ids(exactly_half, old) == {"community:p:0:a": "community:p:0:a"}
    above = {"community:p:0:a": ["m1", "m2", "m3", "x1"]}  # 3/4
    assert reuse_ids(above, old) == {"community:p:0:a": "community:p:0:m1"}


def test_reuse_is_one_to_one_same_level_and_partition():
    old = {"community:p:0:m1": ["m1", "m2", "m3", "m4"],
           "community:p:1:m1": ["m1", "m2", "m3", "m4"],
           "community:q:0:m1": ["m1", "m2", "m3", "m4"]}
    new = {"community:p:0:b1": ["m1", "m2", "m3", "m4", "n"],
           "community:p:0:b2": ["m1", "m2", "m3"]}
    out = reuse_ids(new, old)
    assert out["community:p:0:b1"] == "community:p:0:m1"  # best overlap wins the old id
    assert out["community:p:0:b2"] == "community:p:0:b2"  # old id already taken
    assert len(set(out.values())) == 2


def test_reuse_never_takes_the_own_id_of_another_new_community():
    old = {"community:p:0:m2": ["m2", "m3", "m4"]}
    new = {"community:p:0:m1": ["m1", "m2", "m3", "m4"], "community:p:0:m2": ["m2", "z"]}
    out = reuse_ids(new, old)
    assert out == {"community:p:0:m1": "community:p:0:m1", "community:p:0:m2": "community:p:0:m2"}


# ---- T8: schema ----
import os  # noqa: E402

from kg_pipeline.communities import SCHEMA_STATEMENTS, ensure_schema  # noqa: E402


class _SchemaWriter:
    def __init__(self):
        self.sent = []

    def _run(self, query, **params):
        self.sent.append(query)


def test_ensure_schema_statements():
    w = _SchemaWriter()
    ensure_schema(w, 256)
    assert w.sent and all("IF NOT EXISTS" in q for q in w.sent)
    text = "\n".join(w.sent)
    for label, name in (("Community", "community_id"), ("CommunityReport", "community_report_id")):
        assert f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE" in text
    vector = next(q for q in w.sent if q.startswith("CREATE VECTOR INDEX community_report_embedding"))
    assert "FOR (n:CommunityReport) ON n.embedding" in vector
    assert "`vector.dimensions`: 256" in vector and "'cosine'" in vector
    assert ("CREATE FULLTEXT INDEX community_report_text IF NOT EXISTS FOR (n:CommunityReport) "
            "ON EACH [n.title, n.summary]") in text


def test_ensure_schema_is_idempotent_and_skips_vector_without_dimensions():
    a, b, c = _SchemaWriter(), _SchemaWriter(), _SchemaWriter()
    ensure_schema(a, 256)
    ensure_schema(b, 256)
    assert a.sent == b.sent
    ensure_schema(c, None)
    assert len(c.sent) == len(a.sent) - 1
    assert not any("VECTOR" in q for q in c.sent)
    assert len(SCHEMA_STATEMENTS) == len(c.sent)


@pytest.mark.skipif(not os.environ.get("NEO4J_PASSWORD"), reason="NEO4J_PASSWORD not set")
def test_ensure_schema_live():
    from kg_pipeline.graph.neo4j_writer import Neo4jWriter
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        ensure_schema(w, 8)
        ensure_schema(w, 8)
        w._run("CALL db.awaitIndexes(60)")
        idx = {r["name"]: r["state"] for r in w._run("SHOW INDEXES YIELD name, state RETURN name, state").records}
        assert idx["community_report_embedding"] == "ONLINE" and idx["community_report_text"] == "ONLINE"
        cons = {r["name"] for r in w._run("SHOW CONSTRAINTS YIELD name RETURN name").records}
        assert {"community_id", "community_report_id"} <= cons
        w._run("CALL db.index.vector.queryNodes('community_report_embedding', 1, $v) YIELD node RETURN node",
               v=[0.0] * 8)
        w._run("CALL db.index.fulltext.queryNodes('community_report_text', 'x') YIELD node RETURN node")


# ---- T9: writer, build, build_communities ----
class StoreWriter:
    """In-memory Neo4j stand-in: interprets the Cypher of load_existing, write and the schema."""

    def __init__(self):
        self.comm = {}  # id -> props
        self.member = set()  # (member id, community id)
        self.child = {}  # community id -> parent id
        self.reports = {}  # report id -> community id
        self.queries = []

    def _run(self, query, **p):
        self.queries.append(query)
        if "MATCH (c:Community) WHERE c.id > $after" in query:
            ids = sorted(i for i in self.comm if i > p["after"])[:p["limit"]]
            return _Result([{"id": i, "level": self.comm[i]["level"], "content_hash": self.comm[i]["content_hash"],
                             "updated_at": self.comm[i]["updated_at"], "parent": self.child.get(i),
                             "members": sorted(m for m, c in self.member if c == i)} for i in ids])
        if "DETACH DELETE" in query:
            for i in p["ids"]:
                self.comm.pop(i, None)
                self.child.pop(i, None)
                self.member = {x for x in self.member if x[1] != i}
                self.reports = {r: c for r, c in self.reports.items() if c != i}
            return _Result([])
        if "MERGE (c:Community {id: row.id})" in query:
            for r in p["rows"]:
                self.comm[r["id"]] = {k: r[k] for k in ("level", "size", "acl", "repo", "source", "content_hash")}
                self.comm[r["id"]]["updated_at"] = p["stamp"]
            return _Result([])
        if "DELETE r" in query and "IN_COMMUNITY" in query:
            self.member = {x for x in self.member if x[1] not in set(p["ids"])}
            return _Result([])
        if "DELETE r" in query and "CHILD_OF" in query:
            for i in p["ids"]:
                self.child.pop(i, None)
            return _Result([])
        if "MERGE (m)-[:IN_COMMUNITY]->(c)" in query:
            self.member |= {(x["m"], x["c"]) for x in p["pairs"]}
            return _Result([])
        if "MERGE (c)-[:CHILD_OF]->(q)" in query:
            self.child.update({x["c"]: x["p"] for x in p["pairs"]})
            return _Result([])
        return _Result([])  # schema statements

    def writes(self):
        return [q for q in self.queries if any(k in q for k in ("MERGE", "SET ", "DELETE", "CREATE"))]


def _cliq(names, w=1.0):
    return [(a, b, w) for i, a in enumerate(names) for b in names[i + 1:]]


def _t9_parts():
    a, b = [f"a{i}" for i in range(4)], [f"b{i}" for i in range(4)]
    j, k = [f"j{i}" for i in range(3)], [f"k{i}" for i in range(3)]
    return {
        ("repo:r",): (sorted(a + b), _cliq(a) + _cliq(b) + [("a0", "b0", 0.1)]),
        ("confluence:ENG", "jira:PREC"): (sorted(j + k), _cliq(j) + _cliq(k) + [("j0", "k0", 0.1)]),
    }


def _run_build(w, parts, **kw):
    existing = communities.load_existing(w)
    rows, stats = communities.build(parts, existing, levels=2, backend="fallback", **kw)
    communities.write(w, rows)
    return rows, stats


def test_build_first_run_writes_all():
    w = StoreWriter()
    rows, stats = _run_build(w, _t9_parts())
    assert stats["written"] == len(w.comm) > 0 and stats["unchanged"] == 0 and stats["stale_deleted"] == 0
    assert stats["partitions"] == 2 and sum(stats["communities_by_level"].values()) == len(w.comm)
    assert {c["level"] for c in w.comm.values()} == {0, 1} or {c["level"] for c in w.comm.values()} == {0}
    for cid, par in w.child.items():
        assert w.comm[par]["level"] == w.comm[cid]["level"] + 1


def test_zero_change_run_sends_no_write_query():
    w = StoreWriter()
    _run_build(w, _t9_parts())
    before = {i: dict(c) for i, c in w.comm.items()}
    n = len(w.queries)
    w.queries.clear()
    _, stats = _run_build(w, _t9_parts())
    assert stats["written"] == 0 and stats["unchanged"] == len(before) and stats["stale_deleted"] == 0
    assert w.writes() == [] and n > 0
    assert w.comm == before  # updated_at unchanged


def test_added_member_rewrites_only_its_communities():
    w = StoreWriter()
    _run_build(w, _t9_parts())
    before = {i: dict(c) for i, c in w.comm.items()}
    parts = _t9_parts()
    ids, edges = parts[("repo:r",)]
    parts[("repo:r",)] = (sorted(ids + ["a9"]), edges + [("a0", "a9", 1.0)])
    rows, stats = _run_build(w, parts, )
    changed = [r for r in rows if r["status"] != "unchanged"]
    assert changed and all("a9" in r["members"] for r in changed)
    assert stats["written"] == len(changed) and stats["unchanged"] == len(before) - len(changed)
    assert set(w.comm) == set(before)  # anchors did not move: same ids
    for i, c in w.comm.items():
        if i not in {r["id"] for r in changed}:
            assert c == before[i]
    assert ("a9", next(r["id"] for r in changed if r["level"] == 0)) in w.member


def test_vanished_community_deleted_with_report_and_edges():
    w = StoreWriter()
    _run_build(w, _t9_parts())
    gone = [i for i in w.comm if i.split(":")[3] == "b0" and w.comm[i]["level"] == 0]
    assert gone
    w.reports["rep1"] = gone[0]
    w.reports["rep2"] = next(i for i in w.comm if i not in gone and w.comm[i]["level"] == 0)
    parts = _t9_parts()
    ids, edges = parts[("repo:r",)]
    parts[("repo:r",)] = ([i for i in ids if not i.startswith("b")], [e for e in edges if "b" not in e[0] + e[1]])
    _, stats = _run_build(w, parts)
    assert stats["stale_deleted"] >= 1
    assert gone[0] not in w.comm and "rep1" not in w.reports and "rep2" in w.reports
    assert not any(c == gone[0] for _, c in w.member) and gone[0] not in w.child
    assert not any(m.startswith("b") for m, _ in w.member)


def test_community_acl_equals_partition_key_and_never_mixes():
    w = StoreWriter()
    parts = _t9_parts()
    _run_build(w, parts)
    node_key = {n: key for key, (ids, _) in parts.items() for n in ids}
    assert w.member
    for m, c in w.member:
        assert w.comm[c]["acl"] == list(node_key[m])
    for c in w.comm.values():
        assert c["acl"] in ([list(k) for k in parts])
    assert ["confluence:ENG", "jira:PREC"] in [c["acl"] for c in w.comm.values()]


def test_repo_and_source_fields():
    w = StoreWriter()
    _run_build(w, _t9_parts())
    for c in w.comm.values():
        if c["acl"] == ["repo:r"]:
            assert c["repo"] == "r" and c["source"] is None
        else:
            assert c["repo"] is None and c["source"] == "confluence,jira"


def test_build_communities_runs_schema_and_returns_stats(monkeypatch):
    w = StoreWriter()
    monkeypatch.setattr(communities, "load_partitions", lambda _w: (_t9_parts(), {"partitions": 2, "skipped_too_large": 1}))
    stats = communities.build_communities(w, levels=2, backend="fallback")
    assert stats["written"] == len(w.comm) and stats["skipped_too_large"] == 1
    assert any("CREATE CONSTRAINT community_id" in q for q in w.queries)
    w.queries.clear()
    assert communities.build_communities(w, levels=2, backend="fallback")["written"] == 0
    assert all("MERGE" not in q for q in w.queries)
