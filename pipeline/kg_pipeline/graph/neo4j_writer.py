"""Write a CodeGraph snapshot to Neo4j.

Every node carries the :Code label and a unique id. A snapshot is idempotent: nodes and edges are
MERGEd and stamped with run_id, then anything of this repo from an older run is deleted.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from neo4j import GraphDatabase

from ..code.model import CodeGraph
from ..privacy import protect_row

BATCH = 2000
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

SCHEMA = [
    "CREATE CONSTRAINT code_id IF NOT EXISTS FOR (n:Code) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX code_repo IF NOT EXISTS FOR (n:Code) ON (n.repo)",
    "CREATE INDEX symbol_name IF NOT EXISTS FOR (n:Symbol) ON (n.name)",
    "CREATE INDEX file_path IF NOT EXISTS FOR (n:File) ON (n.path)",
    "CREATE FULLTEXT INDEX symbol_text IF NOT EXISTS FOR (n:Symbol) ON EACH [n.name, n.qualname, n.signature]",
    "CREATE FULLTEXT INDEX chunk_text IF NOT EXISTS FOR (n:Chunk) ON EACH [n.text]",
    # Non-code sources (Jira, git history, documents) share the :Source label.
    "CREATE CONSTRAINT source_id IF NOT EXISTS FOR (n:Source) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX source_key IF NOT EXISTS FOR (n:Ticket) ON (n.key)",
    "CREATE INDEX commit_sha IF NOT EXISTS FOR (n:Commit) ON (n.sha)",
    "CREATE FULLTEXT INDEX source_text IF NOT EXISTS FOR (n:Ticket|Commit|Section) ON EACH [n.key, n.title, n.text]",
    "CREATE FULLTEXT INDEX review_text IF NOT EXISTS FOR (n:PullRequest|Review|ReviewComment) ON EACH [n.key, n.title, n.text]",
    "CREATE INDEX person_github IF NOT EXISTS FOR (n:Person) ON (n.github)",
]
ANCHORS = {"Code", "Source", "Domain", "Requirement"}  # labels with a unique id that edges can join on


def _safe(name: str) -> str:
    if not _NAME.match(name):
        raise ValueError(f"unsafe label or relationship type: {name!r}")
    return name


def _batches(rows: list, size: int = BATCH):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


class Neo4jWriter:
    def __init__(self, uri: str, user: str, password: str, database: str | None = None):
        # Schema warnings (unknown property on first run) are expected; keep logs clean.
        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.database = database

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.driver.close()

    def _run(self, query: str, **params):
        return self.driver.execute_query(query, params, database_=self.database)

    def ensure_schema(self) -> None:
        for statement in SCHEMA:
            self._run(statement)

    def upsert(self, labels: tuple[str, ...], rows: list[dict], keep: tuple[str, ...] = ("embedding", "embedding_hash")) -> int:
        """MERGE :Source nodes by id and replace their properties, keeping computed ones (embeddings).

        Secrets are removed from title and text before the write; text_masked holds the text without PII.
        """
        self.ensure_schema()
        rows = [protect_row(r) for r in rows]
        extra = "".join(f":{_safe(l)}" for l in labels)
        kept = ", ".join(f"n.{_safe(k)} AS {k}" for k in keep)
        restore = ", ".join(f"n.{k} = {k}" for k in keep)
        stamp = datetime.now(timezone.utc).isoformat()
        for batch in _batches([{"id": r["id"], "props": {k: v for k, v in r.items() if v is not None}} for r in rows]):
            self._run(f"UNWIND $rows AS row MERGE (n:Source {{id: row.id}}) WITH n, row, {kept} "
                      f"SET n = row.props, n.ingested_at = $stamp, n{extra}, {restore}", rows=batch, stamp=stamp)
        return len(rows)

    def link(self, etype: str, rows: list[dict], src: str = "Source", dst: str = "Source", replace_from: list[str] | None = None) -> int:
        """MERGE edges {src, dst, **props}. replace_from: delete this type's old edges of these source ids first."""
        if src not in ANCHORS or dst not in ANCHORS:
            raise ValueError(f"edges join on {sorted(ANCHORS)}")
        t = _safe(etype)
        if replace_from:
            for batch in _batches(replace_from):
                self._run(f"UNWIND $ids AS id MATCH (:{src} {{id: id}})-[r:{t}]->(:{dst}) DELETE r", ids=batch)
        for batch in _batches([{"src": r["src"], "dst": r["dst"], "props": {k: v for k, v in r.items() if k not in ("src", "dst")}}
                               for r in rows]):
            self._run(f"UNWIND $rows AS row MATCH (a:{src} {{id: row.src}}) MATCH (b:{dst} {{id: row.dst}}) "
                      f"MERGE (a)-[r:{t}]->(b) SET r = row.props", rows=batch)
        return len(rows)

    def state(self, key: str, value: str | None = None) -> str | None:
        """Read or write a sync cursor (last Jira update time, last ingested commit, ...)."""
        if value is not None:
            self._run("MERGE (s:SyncState {key: $key}) SET s.value = $value, s.updated_at = datetime()", key=key, value=value)
            return value
        rows = self._run("MATCH (s:SyncState {key: $key}) RETURN s.value AS v", key=key).records
        return rows[0]["v"] if rows else None

    def write_delta(self, g: CodeGraph) -> dict:
        """Write only what changed since the last snapshot of this repo.

        Each node and edge stores content_hash, a hash of its labels and parsed properties. New and
        changed items are written; items that are no longer in the graph are deleted; the rest is not
        touched (so their embeddings, roles and links from other layers stay as they are).
        """
        self.ensure_schema()
        stamp = datetime.now(timezone.utc).isoformat()
        h = lambda *parts: hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]
        nodes = {n.id: (n, h(sorted(n.labels), n.props)) for n in g.nodes.values()}
        edges = {(e.src, e.type, e.dst): (e, h(e.props)) for e in g.edges.values()}
        old_nodes = {r["id"]: r["h"] for r in self._run(
            "MATCH (n:Code {repo: $repo}) RETURN n.id AS id, n.content_hash AS h", repo=g.repo).records}
        old_edges = {(r["a"], r["t"], r["b"]): r["h"] for r in self._run(
            "MATCH (a:Code {repo: $repo})-[r]->(b:Code) RETURN a.id AS a, type(r) AS t, b.id AS b, r.content_hash AS h",
            repo=g.repo).records}

        changed = defaultdict(list)
        for nid, (n, nh) in nodes.items():
            if old_nodes.get(nid) != nh:
                changed[n.labels].append({"id": nid, "props": {**n.props, "content_hash": nh}})
        for labels, rows in changed.items():
            extra = "".join(f":{_safe(l)}" for l in labels if l != "Code")
            query = (f"UNWIND $rows AS row MERGE (n:Code {{id: row.id}}) "
                     f"WITH n, row, n.embedding AS emb, n.embedding_hash AS emb_hash, n.role AS role, "
                     f"n.http_method AS http_method, n.route AS route "
                     f"SET n = row.props, n.id = row.id, n.ingested_at = $stamp, n{extra}, "
                     f"n.embedding = emb, n.embedding_hash = emb_hash, n.role = role, "
                     f"n.http_method = http_method, n.route = route")
            for batch in _batches(rows):
                self._run(query, rows=batch, stamp=stamp)
        removed_nodes = [i for i in old_nodes if i not in nodes]
        for batch in _batches(removed_nodes):
            self._run("UNWIND $ids AS id MATCH (n:Code {id: id}) DETACH DELETE n", ids=batch)

        changed_edges = defaultdict(list)
        for key, (e, eh) in edges.items():
            if old_edges.get(key) != eh:
                changed_edges[e.type].append({"src": e.src, "dst": e.dst, "props": {**e.props, "content_hash": eh}})
        for etype, rows in changed_edges.items():
            query = (f"UNWIND $rows AS row MATCH (a:Code {{id: row.src}}) MATCH (b:Code {{id: row.dst}}) "
                     f"MERGE (a)-[r:{_safe(etype)}]->(b) SET r = row.props, r.ingested_at = $stamp")
            for batch in _batches(rows):
                self._run(query, rows=batch, stamp=stamp)
        gone = set(removed_nodes)
        removed_edges = defaultdict(list)
        for (a, t, b) in old_edges:
            if (a, t, b) not in edges and a not in gone and b not in gone:
                removed_edges[t].append({"a": a, "b": b})
        for etype, rows in removed_edges.items():
            for batch in _batches(rows):
                self._run(f"UNWIND $rows AS row MATCH (:Code {{id: row.a}})-[r:{_safe(etype)}]->(:Code {{id: row.b}}) "
                          f"DELETE r", rows=batch)
        return {"mode": "delta", "nodes": len(nodes), "edges": len(edges),
                "nodes_written": sum(map(len, changed.values())), "edges_written": sum(map(len, changed_edges.values())),
                "stale_nodes_deleted": len(removed_nodes), "stale_edges_deleted": sum(map(len, removed_edges.values()))}

    def write_snapshot(self, g: CodeGraph) -> dict:
        self.ensure_schema()
        run_id = uuid.uuid4().hex
        stamp = datetime.now(timezone.utc).isoformat()

        by_labels = defaultdict(list)
        for n in g.nodes.values():
            by_labels[n.labels].append({"id": n.id, "props": n.props})
        for labels, rows in by_labels.items():
            extra = "".join(f":{_safe(l)}" for l in labels if l != "Code")
            # Replace parsed properties but keep what later stages computed (embeddings, roles).
            query = (f"UNWIND $rows AS row MERGE (n:Code {{id: row.id}}) "
                     f"WITH n, row, n.embedding AS emb, n.embedding_hash AS emb_hash, n.role AS role, "
                     f"n.http_method AS http_method, n.route AS route "
                     f"SET n = row.props, n.id = row.id, n.run_id = $run_id, n.ingested_at = $stamp, n{extra}, "
                     f"n.embedding = emb, n.embedding_hash = emb_hash, n.role = role, "
                     f"n.http_method = http_method, n.route = route")
            for batch in _batches(rows):
                self._run(query, rows=batch, run_id=run_id, stamp=stamp)

        by_type = defaultdict(list)
        for e in g.edges.values():
            by_type[e.type].append({"src": e.src, "dst": e.dst, "props": e.props})
        for etype, rows in by_type.items():
            query = (f"UNWIND $rows AS row MATCH (a:Code {{id: row.src}}) MATCH (b:Code {{id: row.dst}}) "
                     f"MERGE (a)-[r:{_safe(etype)}]->(b) "
                     f"SET r = row.props, r.run_id = $run_id, r.ingested_at = $stamp")
            for batch in _batches(rows):
                self._run(query, rows=batch, run_id=run_id, stamp=stamp)

        # Remove what this snapshot no longer contains (deleted files, renamed symbols, stale edges).
        stale_edges = self._run(
            "MATCH (n:Code {repo: $repo})-[r]->(:Code) WHERE r.run_id <> $run_id "
            "WITH r LIMIT 1000000 DELETE r RETURN count(*) AS c", repo=g.repo, run_id=run_id).records[0]["c"]
        stale_nodes = self._run(
            "MATCH (n:Code {repo: $repo}) WHERE n.run_id <> $run_id "
            "WITH n LIMIT 1000000 DETACH DELETE n RETURN count(*) AS c", repo=g.repo, run_id=run_id).records[0]["c"]
        return {"run_id": run_id, "nodes": len(g.nodes), "edges": len(g.edges),
                "stale_nodes_deleted": stale_nodes, "stale_edges_deleted": stale_edges}
