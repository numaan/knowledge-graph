"""Community detection for GraphRAG: levels, resolution, seed, size caps and a lazy library backend.

`detect(nodes, edges)` partitions an undirected weighted graph into communities on up to
`levels` levels. Level 0 is the finest partition. Each next level partitions the quotient
graph (communities as nodes, weights summed between communities), so a finer community
always nests in exactly one parent. `Detection.child_of[k]` maps a level-k community id to its
level-(k+1) parent id. A level with the same number of communities as the level below is not kept.

Backends (`KG_COMMUNITY_BACKEND`): `library` uses graspologic-native (extra `graphrag`, imported
lazily), `fallback` uses the pure-Python Louvain of `leiden_fallback`, `auto` uses the library when
it is installed. Both are deterministic for a fixed seed. Nodes and edges are sorted before use.
Community ids are numbered by the first member in sorted order.

Env: KG_COMMUNITY_RESOLUTION (1.0), KG_COMMUNITY_LEVELS (2, clamp 1 to 4), KG_COMMUNITY_SEED (42),
KG_COMMUNITY_MAX_NODES (200000), KG_COMMUNITY_MAX_EDGES (2000000), KG_COMMUNITY_BACKEND (auto).
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field

from kg_pipeline.leiden_fallback import louvain

log = logging.getLogger("kg_pipeline.communities")

MISSING_LIBRARY = "install the graphrag extra: pip install 'kg-pipeline[graphrag]'"
BACKENDS = ("auto", "fallback", "library")


@dataclass
class Detection:
    levels: list[dict] = field(default_factory=list)  # per level: node -> community id
    child_of: list[dict] = field(default_factory=list)  # child_of[k]: level-k id -> level-(k+1) id
    skipped_too_large: bool = False


def _env_num(name: str, default, cast):
    try:
        return cast(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def resolution_default() -> float:
    return _env_num("KG_COMMUNITY_RESOLUTION", 1.0, float)


def levels_default() -> int:
    return max(1, min(4, _env_num("KG_COMMUNITY_LEVELS", 2, int)))


def seed_default() -> int:
    return _env_num("KG_COMMUNITY_SEED", 42, int)


def max_nodes() -> int:
    return _env_num("KG_COMMUNITY_MAX_NODES", 200000, int)


def max_edges() -> int:
    return _env_num("KG_COMMUNITY_MAX_EDGES", 2000000, int)


def backend_default() -> str:
    value = os.environ.get("KG_COMMUNITY_BACKEND", "auto").strip().lower()
    return value if value in BACKENDS else "auto"


def _sort_key(n):
    return (type(n).__name__, str(n))


def _library_leiden(nodes: list, edges: list, resolution: float, seed: int) -> dict:
    """graspologic-native Leiden. `edges` is sorted (u, v, w) with u < v. Returns node -> int."""
    try:
        import graspologic_native as gn
    except ImportError as exc:
        raise ImportError(MISSING_LIBRARY) from exc
    part: dict = {}
    if edges:
        key = {str(n): n for n in nodes}
        text = [(str(u), str(v), float(w)) for u, v, w in edges]
        _, found = gn.leiden(text, None, resolution, 0.001, 1, True, seed, 1)
        part = {key[s]: c for s, c in found.items()}
    nxt = max(part.values(), default=-1) + 1
    for n in nodes:  # the library omits isolated nodes: each gets its own community
        if n not in part:
            part[n] = nxt
            nxt += 1
    return part


def _fallback(nodes: list, edges: list, resolution: float, seed: int) -> dict:
    adj: dict = {n: {} for n in nodes}
    for u, v, w in edges:
        adj[u][v] = w
        adj[v][u] = w
    return louvain(adj, resolution, seed)


def _canonical(part: dict, nodes: list) -> dict:
    """Renumber community ids by the first member in sorted order."""
    ids: dict = {}
    out = {}
    for n in nodes:
        out[n] = ids.setdefault(part[n], len(ids))
    return out


def _partition(backend: str, nodes: list, edges: list, resolution: float, seed: int) -> dict:
    nodes = sorted(nodes, key=_sort_key)
    edges = sorted(edges, key=lambda e: (_sort_key(e[0]), _sort_key(e[1])))
    if backend == "fallback":
        part = _fallback(nodes, edges, resolution, seed)
    else:
        part = _library_leiden(nodes, edges, resolution, seed)
    return _canonical(part, nodes)


def _clean_edges(nodes: set, edges) -> dict:
    """Undirected edges (u < v by sort key), parallel and opposite edges summed, loops dropped."""
    out: dict = {}
    for u, v, w in edges:
        if u == v or w <= 0:
            continue
        nodes.update((u, v))
        pair = (u, v) if _sort_key(u) <= _sort_key(v) else (v, u)
        out[pair] = out.get(pair, 0.0) + float(w)
    return out


def detect(nodes, edges, resolution: float | None = None, levels: int | None = None, seed: int | None = None,
           backend: str | None = None) -> Detection:
    """Detect communities. `edges` are (u, v, weight); both directions of a pair are summed."""
    resolution = resolution_default() if resolution is None else resolution
    levels = levels_default() if levels is None else max(1, min(4, levels))
    seed = seed_default() if seed is None else seed
    backend = backend_default() if backend is None else backend
    if backend not in BACKENDS:
        raise ValueError(f"unknown community backend {backend!r}, expected one of {BACKENDS}")
    if backend == "auto":
        try:
            import graspologic_native  # noqa: F401
            backend = "library"
        except ImportError:
            backend = "fallback"
    if backend == "library":
        try:
            import graspologic_native  # noqa: F401
        except ImportError as exc:
            raise ImportError(MISSING_LIBRARY) from exc

    node_set = set(nodes)
    weights = _clean_edges(node_set, edges)
    if len(node_set) > max_nodes() or len(weights) > max_edges():
        log.warning("community detection skipped: %d nodes, %d edges exceed the caps (%d nodes, %d edges)",
                    len(node_set), len(weights), max_nodes(), max_edges())
        return Detection(skipped_too_large=True)
    if not node_set:
        return Detection()

    current = _partition(backend, list(node_set), [(u, v, w) for (u, v), w in weights.items()], resolution, seed)
    result = Detection(levels=[current])
    while len(result.levels) < levels:
        count = len(set(current.values()))
        if count <= 1:
            break
        quotient: dict = {}
        for (u, v), w in weights.items():
            a, b = current[u], current[v]
            if a != b:
                pair = (a, b) if a < b else (b, a)
                quotient[pair] = quotient.get(pair, 0.0) + w
        coarse = _partition(backend, list(range(count)), [(a, b, w) for (a, b), w in quotient.items()], resolution, seed)
        if len(set(coarse.values())) == count:
            break  # same partition as the level below: not repeated
        result.child_of.append(dict(coarse))
        current = {n: coarse[c] for n, c in current.items()}
        result.levels.append(current)
    return result


def partition_key(node) -> tuple[str, ...]:
    """ACL partition of a node: the sorted, deduplicated `acl` list; `("repo:<repo>",)` when there
    is no acl but a `repo` (code nodes, same group as the MCP `$repos` scope); `()` (public) when
    neither exists or when acl is empty. Public and repo: partitions never merge. "*" is an ordinary
    group name (fail closed): the Go server (mcp/acl.go allowed()) treats "*" only as a caller group,
    so a node with "*" in its acl is not public.
    """
    acl = node.get("acl")
    groups = sorted({str(g) for g in acl if g}) if isinstance(acl, (list, tuple, set)) else []
    if groups:
        return tuple(groups)
    repo = node.get("repo")
    return (f"repo:{repo}",) if repo else ()


def partition_hash(key: tuple[str, ...]) -> str:
    """Short stable id of a partition key, used in community ids."""
    return hashlib.sha256(json.dumps(list(key)).encode()).hexdigest()[:12]


# Edge type to weight, the single table of the projection. Types not listed are ignored.
# Code layer: CALLS 1.0, IMPORTS 0.5, EXTENDS / IMPLEMENTS / OVERRIDES 1.0, USES_TYPE 0.5,
#   DEFINES / CONTAINS 0.3.
# Domain layer: REALISES / IMPLEMENTED_BY / COVERS / USES_CONCEPT 1.0.
# Sources: MENTIONS 0.7, PART_OF / CHILD_OF 0.5 (documents, sections, tickets).
# MODIFIES is not listed: co-change weight is min(1, n/5) for n commits that touched both files.
# Edge types such as REFERENCES, LINKS, IN_RELEASE, OWNS or DEPENDS_ON carry no weight in the spec.
EDGE_WEIGHTS = {
    "CALLS": 1.0, "IMPORTS": 0.5, "EXTENDS": 1.0, "IMPLEMENTS": 1.0, "OVERRIDES": 1.0, "USES_TYPE": 0.5,
    "DEFINES": 0.3, "CONTAINS": 0.3,
    "REALISES": 1.0, "IMPLEMENTED_BY": 1.0, "COVERS": 1.0, "USES_CONCEPT": 1.0,
    "MENTIONS": 0.7, "PART_OF": 0.5, "CHILD_OF": 0.5,
}
COCHANGE_FULL = 5  # shared commits that give the full weight 1.0
NOT_MEMBERS = frozenset({"Chunk", "Summary", "Folder", "Person", "Community", "CommunityReport", "Commit"})


def project(nodes, rel_rows, cochange_rows=None) -> dict:
    """Project the graph to one undirected weighted graph per ACL partition.

    `nodes`: dicts with `id`, `labels` and optional `acl` / `repo`. `rel_rows`: dicts with `src`,
    `type`, `dst`. `cochange_rows` (optional): one list of Code ids per commit or pull request.
    Returns `{partition_key: (sorted member ids, [(u, v, weight)])}`. Each pair appears once with
    u < v and the summed weight of both directions and of co-change, so `detect()` (which sums
    repeated pairs) does not count it twice. Self-loops, unknown endpoints and edges between
    partitions are dropped. Chunk edges fold into the owner of the chunk (its PART_OF target).
    """
    labels = {n["id"]: set(n.get("labels") or ()) for n in nodes}
    members = {n["id"]: partition_key(n) for n in nodes if not labels[n["id"]] & NOT_MEMBERS}
    owner = {r["src"]: r["dst"] for r in rel_rows
             if r["type"] == "PART_OF" and "Chunk" in labels.get(r["src"], ())}

    def fold(i):
        return owner.get(i, i)

    pairs: dict = {}

    def add(a, b, w):
        if a == b or a not in members or b not in members or members[a] != members[b]:
            return
        pair = (a, b) if str(a) <= str(b) else (b, a)
        pairs[pair] = pairs.get(pair, 0.0) + w

    for r in rel_rows:
        w = EDGE_WEIGHTS.get(r["type"])
        if w:
            add(fold(r["src"]), fold(r["dst"]), w)
    shared: dict = {}
    for files in cochange_rows or ():
        ids = sorted({f for f in files if f in members}, key=str)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                shared[(a, b)] = shared.get((a, b), 0) + 1
    for (a, b), n in shared.items():
        add(a, b, min(1.0, n / COCHANGE_FULL))

    out: dict = {}
    for i, key in members.items():
        out.setdefault(key, ([], []))[0].append(i)
    for key, (ids, _) in out.items():
        ids.sort(key=str)
    for (a, b), w in sorted(pairs.items(), key=lambda p: (str(p[0][0]), str(p[0][1]))):
        out[members[a]][1].append((a, b, w))
    return out
