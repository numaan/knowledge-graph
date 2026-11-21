"""Pure-Python seeded Louvain with a refinement pass (fallback for the Leiden library).

No third-party import. Steps: local moving of nodes, aggregation of communities into
super nodes (repeated until no node moves), then one refinement pass that splits every
community into its connected components. The result is deterministic: nodes are sorted
before use, ties go to the lowest community id, and the visit order comes from
`random.Random(seed)`. Community ids are numbered by the first member in sorted order.

Complexity: one local-moving sweep is O(m). A level needs a few sweeps (capped at
MAX_SWEEPS), and each aggregation at least halves the node count in practice, so the
total is O(m) per level with few levels. The refinement pass is O(n + m).
"""

import random

MAX_SWEEPS = 50
EPS = 1e-12


def _clean(adj: dict) -> tuple[list, list[dict[int, float]]]:
    """Sorted node list and an int-indexed symmetric graph without self-loops."""
    names = set(adj)
    for nbrs in adj.values():
        names.update(nbrs)
    nodes = sorted(names, key=lambda n: (type(n).__name__, str(n)))
    idx = {n: i for i, n in enumerate(nodes)}
    g: list[dict[int, float]] = [{} for _ in nodes]
    for u, nbrs in adj.items():
        for v, w in nbrs.items():
            if u == v or w <= 0:
                continue
            i, j = idx[u], idx[v]
            g[i][j] = max(g[i].get(j, 0.0), float(w))
            g[j][i] = max(g[j].get(i, 0.0), float(w))
    return nodes, g


def _move(g: list[dict[int, float]], loops: list[float], resolution: float, rng: random.Random) -> list[int]:
    """Local moving: return a community id per node (ids are node indexes)."""
    n = len(g)
    deg = [sum(g[i].values()) + 2 * loops[i] for i in range(n)]
    m2 = sum(deg)
    comm = list(range(n))
    if m2 <= 0:
        return comm
    tot = deg[:]
    order = list(range(n))
    for _ in range(MAX_SWEEPS):
        rng.shuffle(order)
        moved = False
        for i in order:
            links: dict[int, float] = {}
            for j, w in g[i].items():
                links[comm[j]] = links.get(comm[j], 0.0) + w
            cur = comm[i]
            tot[cur] -= deg[i]
            best, best_gain = cur, links.get(cur, 0.0) - resolution * deg[i] * tot[cur] / m2
            for c in sorted(links):
                gain = links[c] - resolution * deg[i] * tot[c] / m2
                if gain > best_gain + EPS:
                    best, best_gain = c, gain
            tot[best] += deg[i]
            if best != cur:
                comm[i] = best
                moved = True
        if not moved:
            break
    return comm


def _aggregate(g, loops, comm):
    """Merge each community into one super node. Return (graph, loops, node -> super node)."""
    ids = {c: k for k, c in enumerate(sorted(set(comm)))}
    mapping = [ids[c] for c in comm]
    size = len(ids)
    ng: list[dict[int, float]] = [{} for _ in range(size)]
    nl = [0.0] * size
    for i, nbrs in enumerate(g):
        a = mapping[i]
        nl[a] += loops[i]
        for j, w in nbrs.items():
            b = mapping[j]
            if a == b:
                nl[a] += w / 2  # each internal edge is seen from both ends
            else:
                ng[a][b] = ng[a].get(b, 0.0) + w
    return ng, nl, mapping


def _split_disconnected(g: list[dict[int, float]], comm: list[int]) -> list[int]:
    """Refinement: give each connected component of each community its own id."""
    out = [-1] * len(g)
    nxt = 0
    for start in range(len(g)):
        if out[start] != -1:
            continue
        out[start] = nxt
        stack = [start]
        while stack:
            u = stack.pop()
            for v in g[u]:
                if out[v] == -1 and comm[v] == comm[start]:
                    out[v] = nxt
                    stack.append(v)
        nxt += 1
    return out


def louvain(adj: dict, resolution: float = 1.0, seed: int = 42) -> dict:
    """Partition an undirected weighted graph. `adj[u][v]` is the weight of edge u-v.

    Returns {node: community id}. Self-loops are ignored. Isolated nodes (also nodes that
    only appear as a neighbour) get their own community.
    """
    nodes, g0 = _clean(adj)
    if not nodes:
        return {}
    rng = random.Random(seed)
    g, loops = g0, [0.0] * len(g0)
    owner = list(range(len(g0)))  # original node -> node of the current level
    while True:
        comm = _move(g, loops, resolution, rng)
        if len(set(comm)) == len(g):
            break
        g, loops, mapping = _aggregate(g, loops, comm)
        owner = [mapping[o] for o in owner]
    final = _split_disconnected(g0, owner)
    return {nodes[i]: final[i] for i in range(len(nodes))}
