"""Embeddings in Neo4j vector indexes: code chunks, domain concepts, tickets and document sections.

Each chunk is embedded with a short structural prefix (file, symbol, role, concepts) so that a
question in business words can find the code. Only chunks whose prefixed text changed are embedded.
"""
from __future__ import annotations

import hashlib

from .graph.neo4j_writer import Neo4jWriter
from .models import Embedder
from .privacy import mask

MAX_CHARS = 6_000
WRITE_BATCH = 200


def ensure_indexes(w: Neo4jWriter, dimensions: int) -> None:
    for name, label in (("chunk_embedding", "Chunk"), ("concept_embedding", "DomainConcept"),
                        ("requirement_embedding", "Requirement"), ("source_embedding", "Source")):
        w._run(f"CREATE VECTOR INDEX {name} IF NOT EXISTS FOR (n:{label}) ON n.embedding "
               f"OPTIONS {{indexConfig: {{`vector.dimensions`: {int(dimensions)}, "
               f"`vector.similarity_function`: 'cosine'}}}}")
    w._run("CREATE FULLTEXT INDEX domain_text IF NOT EXISTS FOR (n:DomainConcept|BusinessRule|Capability) "
           "ON EACH [n.name, n.definition, n.statement, n.summary]")


def _chunk_texts(w: Neo4jWriter, repo: str) -> list[dict]:
    rows = w._run("""
        MATCH (c:Chunk {repo: $repo})-[:PART_OF]->(o)
        OPTIONAL MATCH (o)-[:REALISES]->(dc:DomainConcept)
        OPTIONAL MATCH (cap:Capability)-[:COVERS]->(:File {repo: $repo, path: c.path})
        RETURN c.id AS id, c.path AS path, c.text AS text, c.embedding_hash AS hash,
               o.qualname AS owner, o.kind AS kind, o.role AS role, o.signature AS signature,
               collect(DISTINCT dc.name) AS concepts, head(collect(DISTINCT cap.name)) AS capability""",
                  repo=repo).records
    out = []
    for r in rows:
        header = [f"File: {r['path']}"]
        if r["capability"]:
            header.append(f"Capability: {r['capability']}")
        if r["owner"]:
            header.append(f"Symbol: {r['owner']} ({r['kind']}{', ' + r['role'] if r['role'] else ''})")
        if r["concepts"]:
            header.append(f"Concepts: {', '.join(r['concepts'])}")
        out.append({"id": r["id"], "hash": r["hash"], "text": "\n".join(header) + "\n\n" + (r["text"] or "")[:MAX_CHARS]})
    return out


def _concept_texts(w: Neo4jWriter, repo: str) -> list[dict]:
    rows = w._run("MATCH (c:DomainConcept {repo: $repo}) RETURN c.id AS id, c.name AS name, c.kind AS kind, "
                  "c.definition AS definition, c.embedding_hash AS hash", repo=repo).records
    return [{"id": r["id"], "hash": r["hash"],
             "text": f"{r['name']} ({r['kind']}): {r['definition'] or ''}"} for r in rows]


def _source_texts(w: Neo4jWriter) -> list[dict]:
    """Tickets and document sections (commits are short and found through tickets and code)."""
    rows = w._run("""
        MATCH (n:Source) WHERE n:Ticket OR n:Section
        OPTIONAL MATCH (n)-[:PART_OF]->(d:Document)
        RETURN n.id AS id, n.embedding_hash AS hash, n.key AS key, n.type AS type, n.title AS title,
               n.text AS text, d.title AS doc""").records
    out = []
    for r in rows:
        head = f"{r['type']} {r['key']}: {r['title']}" if r["key"] else f"{r['doc'] or ''} > {r['title'] or ''}"
        out.append({"id": r["id"], "hash": r["hash"], "text": head + "\n\n" + (r["text"] or "")[:MAX_CHARS]})
    return out


def embed_sources(w: Neo4jWriter, emb: Embedder) -> dict:
    ensure_indexes(w, emb.dimensions)
    return {"sources": _embed_label(w, emb, _source_texts(w), "Source")}


def _embed_label(w: Neo4jWriter, emb: Embedder, items: list[dict], label: str) -> dict:
    todo = []
    for it in items:
        it = {**it, "text": mask(it["text"])}  # secrets and PII never reach the embedding server (KG_PII_MODE)
        h = hashlib.sha256(f"{emb.model}|{it['text']}".encode()).hexdigest()[:32]
        if h != it["hash"]:
            todo.append({**it, "new_hash": h})
    for i in range(0, len(todo), WRITE_BATCH):
        batch = todo[i:i + WRITE_BATCH]
        vectors = emb.documents([t["text"] for t in batch])
        w._run(f"UNWIND $rows AS row MATCH (n:{label} {{id: row.id}}) "
               "SET n.embedding = row.vec, n.embedding_hash = row.hash",
               rows=[{"id": t["id"], "vec": v, "hash": t["new_hash"]} for t, v in zip(batch, vectors)])
    return {"total": len(items), "embedded": len(todo), "unchanged": len(items) - len(todo)}


def embed_repo(w: Neo4jWriter, repo: str, emb: Embedder) -> dict:
    ensure_indexes(w, emb.dimensions)
    return {"chunks": _embed_label(w, emb, _chunk_texts(w, repo), "Chunk"),
            "concepts": _embed_label(w, emb, _concept_texts(w, repo), "DomainConcept")}


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json
    import os

    ap = argparse.ArgumentParser(description="Embed changed chunks and concepts of a repository.")
    ap.add_argument("--repo", help="embed chunks and concepts of this repository")
    ap.add_argument("--sources", action="store_true", help="embed tickets and document sections")
    args = ap.parse_args(argv)
    if not args.repo and not args.sources:
        ap.error("give --repo, --sources or both")
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        emb = Embedder()
        out = embed_repo(w, args.repo, emb) if args.repo else {}
        if args.sources:
            out |= embed_sources(w, emb)
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
