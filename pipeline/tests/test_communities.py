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
