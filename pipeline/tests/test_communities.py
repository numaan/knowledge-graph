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
