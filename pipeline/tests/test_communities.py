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
