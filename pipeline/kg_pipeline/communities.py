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

Memory model of `load_partitions(w)`: nodes are read in keyset pages (`id > $after ORDER BY id LIMIT`,
no SKIP) and kept as small dicts. Members are counted per ACL partition BEFORE any edge is read, so a
partition above KG_COMMUNITY_MAX_NODES is skipped without an edge query. Edges are read per relationship
type (only the types of EDGE_WEIGHTS) and per batch of source ids of one partition, never as one big
`MATCH ()-[r]-()`; reading stops and the partition is skipped when its edges pass KG_COMMUNITY_MAX_EDGES.
Co-change comes from commits and pull requests in keyset pages, at most 50 files per commit. In Python,
the adjacency of one partition costs about 100 bytes per edge (dict of dicts), so the default cap of
2 000 000 edges needs a few hundred MB; nodes cost a few hundred bytes each. Partitions are skipped,
never truncated.

Community id rule: `community:<partition-hash>:<level>:<anchor>`, where the anchor is the smallest member
id (string order). The same members give the same id in any order, and adding a member keeps the id unless
the new member is smaller. Because Leiden can shift membership, `reuse_ids(new, old)` lets a new community
keep the old id of the same partition and level when the member overlap (shared / larger set) is above 0.5;
the best overlap wins, one old id goes to one new community, and an old id is never given to a new
community when another new community already has it as its own id. Otherwise the old community and its
report are removed as stale. `content_hash(members, member_hashes, prompt_version)` follows `summaries._hash`
(sha256 of JSON, first 20 hex chars): it covers sorted member ids, the member content hashes (summary
`content_hash`, `embedding_hash`) and the prompt version. Edge weights are not an input, so weights alone
never rewrite a community.

Env: KG_COMMUNITY_RESOLUTION (1.0), KG_COMMUNITY_LEVELS (2, clamp 1 to 4), KG_COMMUNITY_SEED (42),
KG_COMMUNITY_MAX_NODES (200000), KG_COMMUNITY_MAX_EDGES (2000000), KG_COMMUNITY_BACKEND (auto).
"""

import datetime as _dt
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field

from kg_pipeline.graph.neo4j_writer import Neo4jWriter
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


INVALID_GROUP = "__invalid__"  # sentinel group for a malformed acl: no caller holds it (fail closed)


def partition_key(node) -> tuple[str, ...]:
    """ACL partition of a node: the sorted, deduplicated `acl` list; `("repo:<repo>",)` when there
    is no acl but a `repo` (code nodes, same group as the MCP `$repos` scope); `()` (public) when
    neither exists or when acl is empty. Public and repo: partitions never merge. "*" is an ordinary
    group name (fail closed): the Go server (mcp/acl.go allowed()) treats "*" only as a caller group,
    so a node with "*" in its acl is not public.
    """
    acl = node.get("acl")
    listed = isinstance(acl, (list, tuple, set)) and len(acl) > 0
    groups = sorted({str(g) for g in acl if g}) if listed else []
    if groups:
        return tuple(groups)
    if listed:  # a non-empty acl without a valid group (`[""]`, `[None]`) is restricted, never public
        return (INVALID_GROUP,)
    repo = node.get("repo")
    return (f"repo:{repo}",) if repo else ()


def partition_hash(key: tuple[str, ...]) -> str:
    """Short stable id of a partition key, used in community ids."""
    return hashlib.sha256(json.dumps(list(key)).encode()).hexdigest()[:12]


def community_id(phash: str, level: int, members) -> str:
    """`community:<partition-hash>:<level>:<smallest member id>`."""
    return f"community:{phash}:{level}:{min(str(m) for m in members)}"


def content_hash(members, member_hashes: dict | None, prompt_version: str) -> str:
    """Hash of the sorted member ids, their content hashes (where known) and the prompt version.
    Same convention as `summaries._hash`. Edge weights are not an input."""
    ids = sorted(str(m) for m in members)
    hashes = member_hashes or {}
    parts = [prompt_version, ids, [hashes.get(m) or "" for m in ids]]
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _scope(cid: str) -> str:
    """`community:<partition-hash>:<level>` of an id (the anchor may contain colons)."""
    return ":".join(cid.split(":", 3)[:3])


def reuse_ids(new: dict, old: dict) -> dict:
    """Map each new community id to the id it should carry. `new` and `old` map id -> member ids.
    A new community takes an old id of the same partition and level when the overlap
    (shared members / size of the larger set) is above 0.5. Best overlap first, one to one."""
    out = {nid: nid for nid in new}
    cands = []
    for nid, nm in new.items():
        ns = set(nm)
        for oid, om in old.items():
            if oid == nid or _scope(oid) != _scope(nid) or oid in new:
                continue
            os_ = set(om)
            ov = len(ns & os_) / max(len(ns), len(os_), 1)
            if ov > 0.5:
                cands.append((-ov, nid, oid))
    taken_old, done_new = set(), set()
    for _, nid, oid in sorted(cands):
        if nid in done_new or oid in taken_old:
            continue
        out[nid] = oid
        done_new.add(nid)
        taken_old.add(oid)
    return out


# Edge type to weight, the single table of the projection. Types not listed are ignored.
# Code layer: CALLS 1.0, IMPORTS 0.5, EXTENDS / IMPLEMENTS / OVERRIDES 1.0, USES_TYPE 0.5,
#   DEFINES / CONTAINS 0.3.
# Domain layer: REALISES / IMPLEMENTED_BY / COVERS / USES_CONCEPT 1.0.
# Sources: MENTIONS 0.7, PART_OF / CHILD_OF 0.5 (documents, sections, tickets).
# MODIFIES is not listed: co-change weight is min(1, n/5) for n commits that touched both files.
# Added after T5 from the real graph: REFERENCES, LINKS, IMPLEMENTED_IN, DEPENDS_ON 0.5 (semantic links).
# Left out on purpose: OWNS, IN_RELEASE, PROVIDES_API (ownership and release bookkeeping, not topic).
EDGE_WEIGHTS = {
    "CALLS": 1.0, "IMPORTS": 0.5, "EXTENDS": 1.0, "IMPLEMENTS": 1.0, "OVERRIDES": 1.0, "USES_TYPE": 0.5,
    "DEFINES": 0.3, "CONTAINS": 0.3,
    "REALISES": 1.0, "IMPLEMENTED_BY": 1.0, "COVERS": 1.0, "USES_CONCEPT": 1.0,
    "MENTIONS": 0.7, "PART_OF": 0.5, "CHILD_OF": 0.5,
    "REFERENCES": 0.5, "LINKS": 0.5, "IMPLEMENTED_IN": 0.5, "DEPENDS_ON": 0.5,
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


PAGE = 5000  # nodes per keyset page
EDGE_BATCH = 5000  # source ids per edge query
COCHANGE_PAGE = 1000  # commits per keyset page
COCHANGE_FILES = 50  # files per commit that count for co-change

_NODES_Q = """
MATCH (n) WHERE n.id IS NOT NULL AND n.id > $after AND none(l IN labels(n) WHERE l IN $skip)
RETURN n.id AS id, labels(n) AS labels, n.acl AS acl, n.repo AS repo ORDER BY id LIMIT $limit"""
_COCHANGE_Q = f"""
MATCH (c:Commit|PullRequest) WHERE c.id > $after
WITH c ORDER BY c.id LIMIT $limit
OPTIONAL MATCH (c)-[:MODIFIES]->(f:Code)
WITH c, f ORDER BY f.id
WITH c, [x IN collect(f.id) WHERE x IS NOT NULL][..{COCHANGE_FILES}] AS files
RETURN c.id AS id, files ORDER BY id"""  # no filter after LIMIT: a page holds `limit` commits, the size filter is in Python


def _edge_query(rel_type: str) -> str:
    assert rel_type in EDGE_WEIGHTS  # only constants of the weight table reach the Cypher text
    return f"MATCH (a)-[r:{rel_type}]->(b) WHERE a.id IN $ids RETURN a.id AS src, type(r) AS type, b.id AS dst"


def _pages(w, query: str, limit: int, **params):
    """Keyset paging: yield pages of rows ordered by `id`, each query starts after the last id."""
    after = ""
    while True:
        rows = [r.data() for r in w._run(query, after=after, limit=limit, **params).records]
        if rows:
            yield rows
            after = rows[-1]["id"]
        if len(rows) < limit:
            return


ANCHOR_LABELS = ("Code", "Source", "Domain", "Requirement")  # labels with an index on `id`, in lookup order


def anchor_label(labels) -> str | None:
    """The label that member lookups by id use (a unique constraint or an index exists for it)."""
    return next((a for a in ANCHOR_LABELS if a in (labels or ())), None)


def _read_edges(w, ids: list, limit_edges: int) -> list | None:
    """Edges of all weighted types whose source is in `ids`; None when there are more than `limit_edges`."""
    rows: list = []
    for rel_type in EDGE_WEIGHTS:
        query = _edge_query(rel_type)
        for i in range(0, len(ids), EDGE_BATCH):
            rows += [r.data() for r in w._run(query, ids=ids[i:i + EDGE_BATCH]).records]
            if len(rows) > limit_edges:
                return None
    return rows


def load_partitions(w) -> tuple[dict, dict]:
    """Read the graph from Neo4j and project it per ACL partition (see the module docstring).

    Returns `(partitions, stats)`. `partitions` is the output of `project()`. `stats` has `partitions`
    (kept), `skipped_too_large`, `skipped_hashes` (partition hashes of the skipped partitions: their
    existing communities must be kept), `nodes` and `edges` read, and `labels` (member id to the label
    for id lookups, see `anchor_label`). Chunks are read only to fold their edges into their owner; they
    do not count toward KG_COMMUNITY_MAX_NODES. A group that holds only chunks (the chunk acl differs from
    the acl of its owner) is read for its edges too: they fold to the owner and count where the owner lives.
    """
    skip = sorted(NOT_MEMBERS - {"Chunk"})
    groups: dict = {}
    for page in _pages(w, _NODES_Q, PAGE, skip=skip):
        for n in page:
            groups.setdefault(partition_key(n), []).append(n)

    stats = {"partitions": 0, "skipped_too_large": 0, "skipped_hashes": [], "nodes": 0, "edges": 0, "labels": {}}
    limit_nodes, limit_edges = max_nodes(), max_edges()
    kept_nodes: list = []
    rows: list = []
    for key, nodes in sorted(groups.items()):
        count = sum(1 for n in nodes if "Chunk" not in (n.get("labels") or ()))
        if count > limit_nodes:  # counted before any edge is read
            log.warning("partition %s skipped: %d nodes exceed KG_COMMUNITY_MAX_NODES=%d", list(key), count, limit_nodes)
            stats["skipped_too_large"] += 1
            stats["skipped_hashes"].append(partition_hash(key))
            continue
        part_rows = _read_edges(w, sorted(n["id"] for n in nodes), limit_edges)
        if part_rows is None:
            log.warning("partition %s skipped: more than KG_COMMUNITY_MAX_EDGES=%d edges", list(key), limit_edges)
            stats["skipped_too_large"] += 1
            stats["skipped_hashes"].append(partition_hash(key))
            continue
        kept_nodes += nodes
        rows += part_rows
        if count == 0:  # chunks only: no partition of its own, the edges fold to the owners
            continue
        stats["nodes"] += count
        stats["edges"] += len(part_rows)
        for n in nodes:
            anchor = anchor_label(n.get("labels"))
            if anchor:
                stats["labels"][n["id"]] = anchor

    cochange: list = []
    if kept_nodes:
        kept = {n["id"] for n in kept_nodes}
        for page in _pages(w, _COCHANGE_Q, COCHANGE_PAGE):
            for r in page:
                files = [f for f in r["files"] if f in kept]
                if len(files) > 1:
                    cochange.append(files)
    partitions = project(kept_nodes, rows, cochange)
    stats["partitions"] = len(partitions)
    return partitions, stats


# --- schema -----------------------------------------------------------------------------------------

SCHEMA_STATEMENTS = (
    "CREATE CONSTRAINT community_id IF NOT EXISTS FOR (n:Community) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT community_report_id IF NOT EXISTS FOR (n:CommunityReport) REQUIRE n.id IS UNIQUE",
    "CREATE FULLTEXT INDEX community_report_text IF NOT EXISTS FOR (n:CommunityReport) "
    "ON EACH [n.title, n.summary]",
    # Member lookups by id are labelled. Code and Source have a unique constraint; Domain and Requirement
    # need an index, or every lookup scans the label. IF NOT EXISTS skips an existing constraint or index.
    "CREATE INDEX domain_id IF NOT EXISTS FOR (n:Domain) ON (n.id)",
    "CREATE INDEX requirement_id IF NOT EXISTS FOR (n:Requirement) ON (n.id)",
)


def ensure_schema(w, dimensions: int | None) -> None:
    """Create the constraints and indexes of communities. Idempotent. No vector index without dimensions."""
    for statement in SCHEMA_STATEMENTS:
        w._run(statement)
    if dimensions:
        w._run("CREATE VECTOR INDEX community_report_embedding IF NOT EXISTS FOR (n:CommunityReport) "
               f"ON n.embedding OPTIONS {{indexConfig: {{`vector.dimensions`: {int(dimensions)}, "
               "`vector.similarity_function`: 'cosine'}}}")


# --- build and write ----------------------------------------------------------------------------------

PROMPT_VERSION = "community-v1"  # part of the community content hash; T10 reports use the same value
EXISTING_PAGE = 1000  # communities per page of load_existing
WRITE_ROWS = 200  # community rows per write query
WRITE_PAIRS = 5000  # relationship pairs per write query

_EXISTING_Q = """
MATCH (c:Community) WHERE c.id > $after
WITH c ORDER BY c.id LIMIT $limit
OPTIONAL MATCH (m)-[:IN_COMMUNITY]->(c)
WITH c, collect(m.id) AS members
OPTIONAL MATCH (c)-[:CHILD_OF]->(p:Community)
RETURN c.id AS id, c.level AS level, c.content_hash AS content_hash, c.updated_at AS updated_at,
       p.id AS parent, members ORDER BY id"""


def load_existing(w) -> dict:
    """Communities in the graph: `{id: {level, content_hash, updated_at, parent, members}}` (keyset paged)."""
    out: dict = {}
    for page in _pages(w, _EXISTING_Q, EXISTING_PAGE):
        for r in page:
            out[r["id"]] = {"level": r["level"], "content_hash": r["content_hash"], "updated_at": r["updated_at"],
                            "parent": r["parent"], "members": sorted(r["members"] or [], key=str)}
    return out


_HASHES_Q = """
MATCH (s:Summary)-[:SUMMARIZES]->(m) WHERE m.id > $after AND s.content_hash IS NOT NULL
WITH m.id AS id, collect(s.content_hash) AS hashes
ORDER BY id LIMIT $limit
RETURN id, hashes"""


def load_member_hashes(w) -> dict:
    """`{member id: hash}` of the summaries of each node (Summary.content_hash via SUMMARIZES, keyset paged).
    A node with several summaries gets one hash over the sorted list, so any change shows in the community hash."""
    out: dict = {}
    for page in _pages(w, _HASHES_Q, PAGE):
        for r in page:
            hs = sorted(str(h) for h in r["hashes"] or [] if h)
            if hs:
                out[r["id"]] = hs[0] if len(hs) == 1 else hashlib.sha256(json.dumps(hs).encode()).hexdigest()[:20]
    return out


def _source_of(key: tuple[str, ...]) -> str | None:
    """Source systems of a document partition: the prefixes of its acl groups (`jira:PREC` -> `jira`)."""
    if not key or (len(key) == 1 and key[0].startswith("repo:")):
        return None
    return ",".join(sorted({g.split(":", 1)[0] for g in key})) or None


def build(partitions: dict, existing: dict, resolution: float | None = None, levels: int | None = None,
          seed: int | None = None, backend: str | None = None, member_hashes: dict | None = None,
          prompt_version: str = PROMPT_VERSION, labels: dict | None = None,
          skipped_hashes=None) -> tuple[list[dict], dict]:
    """Detect communities per partition and compare them with `existing` (see `load_existing`).

    Returns `(rows, stats)`. Each row has `id, level, size, members, parent, acl, repo, source, content_hash,
    status`. `status` is `new`, `changed` (hash, level or parent differs), `unchanged`, or `stale` (an
    existing id that no new community carries; the row only has `id`). Detection runs inside one ACL
    partition, so a community never mixes ACL sets, and `acl` is the partition key. Old ids are reused
    through `reuse_ids`.

    `labels` (member id to lookup label, from `load_partitions`) is carried in each row as `member_labels`;
    without it every member is looked up as `Code`. `skipped_hashes`: partition hashes that the loader
    skipped as too large. Partitions that `detect` skips are added. Existing communities of a skipped
    partition are neither rewritten nor stale: an oversize run must not delete reports that cost LLM calls.
    A community is `changed` when its stored members differ from the new ones (repairs a partial write).
    """
    skipped_set = set(skipped_hashes or ())
    rows: list[dict] = []
    skipped = 0
    by_level: dict = {}
    kept = 0
    for key, (ids, edges) in sorted(partitions.items()):
        det = detect(ids, edges, resolution=resolution, levels=levels, seed=seed, backend=backend)
        phash = partition_hash(key)
        if det.skipped_too_large:
            skipped += 1
            skipped_set.add(phash)
            continue
        kept += 1
        part_rows: list[dict] = []
        label_to_id: list[dict] = []
        for level, assign in enumerate(det.levels):
            groups: dict = {}
            for node, label in assign.items():
                groups.setdefault(label, []).append(node)
            label_to_id.append({label: community_id(phash, level, m) for label, m in groups.items()})
            for label, m in groups.items():
                m = sorted(m, key=str)
                part_rows.append({
                    "id": label_to_id[level][label], "level": level, "size": len(m), "members": m, "label": label,
                    "acl": list(key), "repo": key[0][5:] if len(key) == 1 and key[0].startswith("repo:") else None,
                    "source": _source_of(key), "partition": phash,
                    "member_labels": {x: (labels or {}).get(x, "Code" if labels is None else None) for x in m},
                    "content_hash": content_hash(m, member_hashes, prompt_version)})
        for r in part_rows:
            nxt = det.child_of[r["level"]].get(r["label"]) if r["level"] < len(det.child_of) else None
            r["parent"] = label_to_id[r["level"] + 1][nxt] if nxt is not None else None
        rows += part_rows

    old_members = {i: e["members"] for i, e in existing.items()}
    final = reuse_ids({r["id"]: r["members"] for r in rows}, old_members)
    for r in rows:
        r["id"] = final[r["id"]]
    for r in rows:
        if r["parent"] is not None:
            r["parent"] = final[r["parent"]]
        del r["label"]
        old = existing.get(r["id"])
        if old is None:
            r["status"] = "new"
        elif (old["content_hash"] != r["content_hash"] or old["parent"] != r["parent"] or old["level"] != r["level"]
              or old["members"] != r["members"]):
            r["status"] = "changed"
        else:
            r["status"] = "unchanged"
        by_level[r["level"]] = by_level.get(r["level"], 0) + 1
    carried = {r["id"] for r in rows}
    stale = sorted(i for i in existing if i not in carried and _scope(i).split(":")[1] not in skipped_set)
    rows += [{"id": i, "status": "stale"} for i in stale]
    stats = {
        "written": sum(1 for r in rows if r["status"] in ("new", "changed")),
        "unchanged": sum(1 for r in rows if r["status"] == "unchanged"),
        "stale_deleted": len(stale), "skipped_too_large": skipped, "partitions": kept,
        "communities_by_level": dict(sorted(by_level.items())),
    }
    return rows, stats


_UPSERT_Q = """
UNWIND $rows AS row
MERGE (c:Community {id: row.id})
SET c.level = row.level, c.size = row.size, c.acl = row.acl, c.repo = row.repo, c.source = row.source,
    c.content_hash = row.content_hash, c.updated_at = $stamp"""
_CLEAR_MEMBERS_Q = "MATCH (m)-[r:IN_COMMUNITY]->(c:Community) WHERE c.id IN $ids DELETE r"
_CLEAR_PARENT_Q = "MATCH (c:Community)-[r:CHILD_OF]->(:Community) WHERE c.id IN $ids DELETE r"
_MEMBERS_Q = """
UNWIND $pairs AS pair
MATCH (c:Community {id: pair.c})
MATCH (m:%s {id: pair.m})
MERGE (m)-[:IN_COMMUNITY]->(c)"""  # the label is one of ANCHOR_LABELS: a lookup by id never scans all nodes
_PARENT_Q = """
UNWIND $pairs AS pair
MATCH (c:Community {id: pair.c})
MATCH (q:Community {id: pair.p})
MERGE (c)-[:CHILD_OF]->(q)"""
_DELETE_Q = """
MATCH (c:Community) WHERE c.id IN $ids
OPTIONAL MATCH (r:CommunityReport)-[:REPORTS_ON]->(c)
DETACH DELETE r, c"""


def write(w, rows: list[dict], stamp: str | None = None) -> None:
    """Write `new` and `changed` rows and delete `stale` ones. Rows with another status send no query.
    IN_COMMUNITY (node to its community, one per level) and CHILD_OF of rewritten communities are replaced;
    `updated_at` is set only here, on a rewrite. Idempotent: MERGE on the community id."""
    todo = [r for r in rows if r["status"] in ("new", "changed")]
    stale = [r["id"] for r in rows if r["status"] == "stale"]
    stamp = stamp or _dt.datetime.now(_dt.timezone.utc).isoformat()
    for i in range(0, len(stale), WRITE_ROWS):
        w._run(_DELETE_Q, ids=stale[i:i + WRITE_ROWS])
    for i in range(0, len(todo), WRITE_ROWS):
        batch = todo[i:i + WRITE_ROWS]
        ids = [r["id"] for r in batch]
        w._run(_UPSERT_Q, rows=[{k: r[k] for k in ("id", "level", "size", "acl", "repo", "source", "content_hash")}
                                for r in batch], stamp=stamp)
        w._run(_CLEAR_MEMBERS_Q, ids=ids)
        w._run(_CLEAR_PARENT_Q, ids=ids)
    by_label: dict = {a: [] for a in ANCHOR_LABELS}
    for r in todo:
        for m in r["members"]:
            label = (r.get("member_labels") or {}).get(m)
            if label in by_label:
                by_label[label].append({"c": r["id"], "m": m})
            else:
                log.warning("member %s of %s has no label with an id index: not linked", m, r["id"])
    for label, pairs in by_label.items():
        for i in range(0, len(pairs), WRITE_PAIRS):
            w._run(_MEMBERS_Q % label, pairs=pairs[i:i + WRITE_PAIRS])
    links = [{"c": r["id"], "p": r["parent"]} for r in todo if r["parent"]]
    for i in range(0, len(links), WRITE_PAIRS):
        w._run(_PARENT_Q, pairs=links[i:i + WRITE_PAIRS])


def build_communities(w, resolution: float | None = None, levels: int | None = None, seed: int | None = None,
                      backend: str | None = None, dry_run: bool = False) -> dict:
    """Load, detect, compare and write communities. Returns the stats of `build` plus the loader's
    `nodes` and `edges`. With `dry_run` nothing is written (and no schema statement is sent)."""
    partitions, load_stats = load_partitions(w)
    if not dry_run:
        ensure_schema(w, None)
    rows, stats = build(partitions, load_existing(w), resolution, levels, seed, backend,
                        member_hashes=load_member_hashes(w), labels=load_stats.get("labels"),
                        skipped_hashes=load_stats.get("skipped_hashes"))
    stats["skipped_too_large"] += load_stats.get("skipped_too_large", 0)
    for k in ("nodes", "edges"):
        if k in load_stats:
            stats[k] = load_stats[k]
    if not dry_run:
        write(w, rows)
    return stats


def main(argv: list[str] | None = None) -> None:
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(prog="kg-communities", description="Build GraphRAG communities and, with --report, "
                                 "their reports. Prints the stats as JSON.")
    ap.add_argument("--report", action="store_true", help="also build the community reports")
    ap.add_argument("--dry-run", action="store_true", help="detect and compare only: no write, no schema, no LLM, no embedder")
    ap.add_argument("--level", type=int, default=None, help="limit reports and printed counts to this level")
    ap.add_argument("--resolution", type=float, default=None, help="override KG_COMMUNITY_RESOLUTION")
    ap.add_argument("--no-llm", action="store_true", help="extractive reports only")
    ap.add_argument("--budget", type=int, default=None, help="maximum LLM calls for reports (default COMMUNITY_BUDGET or 100)")
    ap.add_argument("--cache", type=Path, default=Path(os.environ.get("LLM_CACHE_DIR", ".kg-cache/llm")))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        stats = build_communities(w, resolution=args.resolution, dry_run=args.dry_run)
        if args.level is not None:
            stats["communities_by_level"] = {k: v for k, v in stats["communities_by_level"].items() if k == args.level}
        stats["dry_run"] = args.dry_run
        if args.report and not args.dry_run:
            from kg_pipeline import community_reports
            from kg_pipeline.models import LLM, Embedder

            llm = None if args.no_llm else LLM()
            stats["reports"] = community_reports.build_reports(w, llm=llm, emb=Embedder(), level=args.level,
                                                               budget=args.budget, cache=args.cache)
        elif args.report:
            stats["reports"] = "skipped (dry run)"
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
