"""Near-duplicate documents and sections across Confluence spaces and document folders.

    kg-dedupe                       # after kg-docs; incremental
    kg-dedupe --threshold 0.9

Each section gets a normalised-text hash (exact copies) and a bottom-k MinHash sketch of its word
5-shingles (near copies). The sketch of a document is the bottom-k of its sections' sketches, which
is the sketch of the union of their shingles, so documents need no text. Pairs whose estimated
Jaccard similarity is at least the threshold are clustered, and every member of a cluster points to
one canonical node: Confluence before files, then the longer text, then the smaller id.

Graph:
    (:Section)-[:DUPLICATE_OF {method: hash|minhash, confidence}]->(:Section)
    (:Document)-[:DUPLICATE_OF {method, confidence}]->(:Document)

Sketches are stored on the sections (dup_sketch, dup_hash, dup_words). A rewritten section loses
them (Neo4jWriter.upsert replaces properties), so only new and changed sections are read and hashed;
edges are written only when they change (content_hash), like the other assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from itertools import combinations

from .graph.neo4j_writer import Neo4jWriter

K = 64              # sketch size
SHINGLE = 5         # words per shingle
MIN_WORDS = 20      # shorter sections ("See also", "TODO") are not compared
MAX_POSTING = 200   # a shingle in more items than this is boilerplate and does not propose pairs
THRESHOLD = float(os.environ.get("KG_DUP_THRESHOLD", "0.8"))
SOURCE_RANK = {"confluence": 0, "files": 1}
_MARKUP = re.compile(r"[#*_`>|~\[\]()!:-]+|\s+")


def words(text: str) -> list[str]:
    return [w for w in _MARKUP.sub(" ", (text or "").lower()).split() if w]


def norm_hash(text: str) -> str:
    """Hash of the text without case, Markdown markup and spacing: the same text pasted in two tools."""
    return hashlib.sha256(" ".join(words(text)).encode()).hexdigest()[:20]


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big") >> 1   # fits a Neo4j integer


def sketch(text: str) -> list[int]:
    ws = words(text)
    n = min(SHINGLE, len(ws))
    shingles = {" ".join(ws[i:i + n]) for i in range(len(ws) - n + 1)} if ws else set()
    return sorted({_h(s) for s in shingles})[:K]


def union(sketches: list[list[int]]) -> list[int]:
    return sorted({v for s in sketches for v in s})[:K]


def jaccard(a: list[int], b: list[int]) -> float:
    """Bottom-k estimate: share of the k smallest values of the union that are in both sketches."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    u = sorted(sa | sb)[:K]
    return sum(1 for v in u if v in sa and v in sb) / len(u)


def pairs(items: dict[str, dict], threshold: float) -> dict[tuple[str, str], tuple[str, float]]:
    """{(a, b): (method, similarity)} for a < b; items: id -> {sketch, hash}."""
    out: dict[tuple[str, str], tuple[str, float]] = {}
    by_hash = defaultdict(list)
    for i, it in items.items():
        by_hash[it["hash"]].append(i)
    for ids in by_hash.values():
        for a, b in combinations(sorted(ids), 2):
            out[(a, b)] = ("hash", 1.0)
    index = defaultdict(list)
    for i, it in items.items():
        for v in it["sketch"]:
            index[v].append(i)
    shared = Counter()
    for ids in index.values():
        if len(ids) <= MAX_POSTING:
            shared.update(combinations(sorted(ids), 2))
    need = max(2, int(K * threshold / 2))
    for (a, b), c in shared.items():
        if (a, b) not in out and c >= min(need, len(items[a]["sketch"]), len(items[b]["sketch"])):
            j = jaccard(items[a]["sketch"], items[b]["sketch"])
            if j >= threshold:
                out[(a, b)] = ("minhash", round(j, 3))
    return out


def clusters(items: dict[str, dict], found: dict[tuple[str, str], tuple[str, float]]) -> dict[str, tuple[str, str, float]]:
    """member -> (canonical, method, similarity to the canonical) for every non-canonical member."""
    parent = {i: i for i in items}

    def root(i: str) -> str:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for a, b in found:
        parent[root(a)] = root(b)
    groups = defaultdict(list)
    for i in items:
        groups[root(i)].append(i)
    out = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        rank = lambda i: (SOURCE_RANK.get(items[i]["source"], 9), -items[i]["chars"], i)
        canon = min(members, key=rank)
        for m in members:
            if m != canon:
                same = items[m]["hash"] == items[canon]["hash"]
                out[m] = (canon, "hash" if same else "minhash",
                          1.0 if same else round(jaccard(items[m]["sketch"], items[canon]["sketch"]), 3))
    return out


def _update_sketches(w: Neo4jWriter) -> int:
    rows = w._run("MATCH (s:Section) WHERE s.dup_sketch IS NULL RETURN s.id AS id, s.text AS text").records
    todo = [{"id": r["id"], "sketch": sketch(r["text"]), "hash": norm_hash(r["text"]), "words": len(words(r["text"]))}
            for r in rows]
    for i in range(0, len(todo), 1000):
        w._run("UNWIND $rows AS row MATCH (s:Section {id: row.id}) "
               "SET s.dup_sketch = row.sketch, s.dup_hash = row.hash, s.dup_words = row.words", rows=todo[i:i + 1000])
    return len(todo)


def detect(w: Neo4jWriter, threshold: float = THRESHOLD) -> dict:
    hashed = _update_sketches(w)
    rows = [r.data() for r in w._run(
        "MATCH (s:Section)-[:PART_OF]->(d:Document) RETURN s.id AS id, d.id AS doc, d.source AS source, "
        "s.dup_sketch AS sketch, s.dup_hash AS hash, s.dup_words AS words, s.order AS order, size(s.text) AS chars").records]
    sections = {r["id"]: r for r in rows if (r["words"] or 0) >= MIN_WORDS}
    by_doc = defaultdict(list)
    for r in sorted(rows, key=lambda r: (r["doc"], r["order"] or 0)):
        by_doc[r["doc"]].append(r)
    docs = {d: {"sketch": union([s["sketch"] or [] for s in ss]),
                "hash": hashlib.sha256("|".join(s["hash"] or "" for s in ss).encode()).hexdigest()[:20],
                "source": ss[0]["source"], "chars": sum(s["chars"] or 0 for s in ss),
                "words": sum(s["words"] or 0 for s in ss)}
            for d, ss in by_doc.items()}
    docs = {d: it for d, it in docs.items() if it["words"] >= MIN_WORDS}
    wanted = {**clusters(sections, pairs(sections, threshold)), **clusters(docs, pairs(docs, threshold))}
    return {"sections_hashed": hashed, "sections": len(sections), "documents": len(docs), **write_edges(w, wanted)}


def write_edges(w: Neo4jWriter, wanted: dict[str, tuple[str, str, float]]) -> dict:
    """Write new and changed DUPLICATE_OF edges, delete the ones that no longer hold."""
    h = lambda *p: hashlib.sha256(json.dumps(p).encode()).hexdigest()[:20]
    old = {(r["a"], r["b"]): r["h"] for r in w._run(
        "MATCH (a:Source)-[r:DUPLICATE_OF]->(b:Source) RETURN a.id AS a, b.id AS b, r.content_hash AS h").records}
    rows = [{"src": m, "dst": c, "props": {"method": method, "confidence": conf, "content_hash": h(method, conf)}}
            for m, (c, method, conf) in sorted(wanted.items())]
    changed = [r for r in rows if old.get((r["src"], r["dst"])) != r["props"]["content_hash"]]
    gone = [{"a": a, "b": b} for (a, b) in old if wanted.get(a, (None,))[0] != b]
    for i in range(0, len(gone), 1000):
        w._run("UNWIND $rows AS row MATCH (:Source {id: row.a})-[r:DUPLICATE_OF]->(:Source {id: row.b}) DELETE r",
               rows=gone[i:i + 1000])
    for i in range(0, len(changed), 1000):
        w._run("UNWIND $rows AS row MATCH (a:Source {id: row.src}) MATCH (b:Source {id: row.dst}) "
               "MERGE (a)-[r:DUPLICATE_OF]->(b) SET r = row.props", rows=changed[i:i + 1000])
    return {"duplicates": len(rows), "edges_written": len(changed), "edges_deleted": len(gone)}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threshold", type=float, default=THRESHOLD, help="minimum estimated Jaccard similarity")
    args = ap.parse_args(argv)
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        print(json.dumps(detect(w, args.threshold), indent=2))


if __name__ == "__main__":
    main()
