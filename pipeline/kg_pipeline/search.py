"""Hybrid retrieval: vector + full-text over code chunks, plus domain concepts, fused with RRF,
then expanded through the graph (owner symbol, callers, callees, concepts, rules, tests).

    kg-search "how is a simulation run scored" --repo precept
"""
from __future__ import annotations

import argparse
import json
import os
import re

from .graph.neo4j_writer import Neo4jWriter
from .models import Embedder

RRF_K = 60
TEST_PATH = r"(?i).*(^|/)(tests?|__tests__|spec)/.*|.*(_test|_spec|\.test|\.spec|Test|Tests)\.[a-z]+$|.*(^|/)test_[^/]*$"
_LUCENE = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')


def lucene(text: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9_]{3,}", text)][:30]
    return " OR ".join(_LUCENE.sub(r"\\\1", w) for w in words) or "*"


def _rrf(*ranked: list[str]) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ids in ranked:
        for rank, i in enumerate(ids):
            scores[i] = scores.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


def candidates(w: Neo4jWriter, emb: Embedder, repo: str, query: str, k: int = 8,
               vector: list[float] | None = None) -> list[tuple[str, float]]:
    """Top chunk ids for a query, fused from three rankings."""
    vec = vector or emb.query(query)
    by_vector = [r["id"] for r in w._run(
        "CALL db.index.vector.queryNodes('chunk_embedding', 80, $v) YIELD node, score "
        "WHERE node.repo = $repo RETURN node.id AS id ORDER BY score DESC LIMIT 40", v=vec, repo=repo).records]
    by_text = [r["id"] for r in w._run(
        "CALL db.index.fulltext.queryNodes('chunk_text', $q, {limit: 80}) YIELD node, score "
        "WHERE node.repo = $repo RETURN node.id AS id ORDER BY score DESC LIMIT 40",
        q=lucene(query), repo=repo).records]
    # Concepts that match the query lead to the code that realises them.
    by_concept = [r["id"] for r in w._run(
        "CALL db.index.vector.queryNodes('concept_embedding', 10, $v) YIELD node, score WHERE node.repo = $repo "
        "MATCH (node)<-[:REALISES]-(s:Symbol)<-[:PART_OF]-(c:Chunk) "
        "RETURN c.id AS id, max(score) AS s ORDER BY s DESC LIMIT 40", v=vec, repo=repo).records]
    return _rrf(by_vector, by_text, by_concept)[:k]


def expand(w: Neo4jWriter, chunk_ids: list[str], tests_per_symbol: int = 3) -> list[dict]:
    """Evidence pack per chunk: code, owner symbol, neighbours, concepts, rules and related tests."""
    rows = w._run("""
        UNWIND range(0, size($ids) - 1) AS i
        MATCH (c:Chunk {id: $ids[i]})-[:PART_OF]->(o)
        OPTIONAL MATCH (o)-[:REALISES]->(dc:DomainConcept)
        OPTIONAL MATCH (br:BusinessRule)-[:IMPLEMENTED_BY]->(o)
        WITH i, c, o, collect(DISTINCT dc.name) AS concepts, collect(DISTINCT br.statement) AS rules
        OPTIONAL MATCH (o)<-[:CALLS]-(caller:Symbol)
        WITH i, c, o, concepts, rules, collect(DISTINCT caller.qualname)[..6] AS callers
        OPTIONAL MATCH (o)-[:CALLS]->(callee:Symbol)
        WITH i, c, o, concepts, rules, callers, collect(DISTINCT callee.qualname)[..6] AS callees
        OPTIONAL MATCH (t:Symbol)-[:CALLS*1..2]->(o) WHERE t.path =~ $test_path
        WITH i, c, o, concepts, rules, callers, callees, collect(DISTINCT t)[..$tests] AS tests
        RETURN i, c.id AS chunk, c.path AS path, c.start_line AS start, c.end_line AS end, c.text AS text,
               coalesce(o.qualname, o.path) AS symbol, o.id AS symbol_id, coalesce(o.kind, "File") AS kind,
               o.role AS role, o.route AS route,
               concepts, rules, callers, callees,
               [t IN tests | {symbol: t.qualname, id: t.id, path: t.path}] AS tests
        ORDER BY i""", ids=chunk_ids, test_path=TEST_PATH, tests=tests_per_symbol).records
    return [r.data() for r in rows]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--repo", required=True)
    ap.add_argument("-k", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        hits = candidates(w, Embedder(), args.repo, args.query, args.k)
        evidence = expand(w, [h[0] for h in hits])
    if args.json:
        print(json.dumps(evidence, indent=2))
        return
    for (cid, score), e in zip(hits, evidence):
        role = f" [{e['role']}{' ' + e['route'] if e['route'] else ''}]" if e["role"] else ""
        print(f"{score:.4f}  {e['path']}:{e['start']}-{e['end']}  {e['kind']} {e['symbol']}{role}")
        for label in ("concepts", "rules", "callers", "callees"):
            if e[label]:
                print(f"         {label}: {', '.join(e[label][:4])}")
        if e["tests"]:
            print(f"         tests: {', '.join(t['symbol'] for t in e['tests'])}")


if __name__ == "__main__":
    main()
