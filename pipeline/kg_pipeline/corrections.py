"""Corrections overlay: human fixes that survive re-ingestion.

    kg-corrections list                       # all corrections
    kg-corrections list --status approved
    kg-corrections apply                      # apply approved corrections, undo withdrawn ones

A review flow (MCP or web) creates :Correction nodes with status 'proposed' and a reviewer sets
'approved' or 'rejected'. Ingestion rewrites nodes and edges, so the approved corrections are applied
again after every write of the pipeline (the Dagster writer does it on close; after a CLI run, run
kg-corrections apply).

    (:Correction {id, kind, target_id, edge_type, other_id, text, author, created_at, status,
                  applied_at, applied_count, error})

kind            effect of an approved correction
suppress_edge   delete (target_id)-[edge_type]->(other_id) every time it comes back
add_fact        MERGE (target_id)-[edge_type {method: 'human', confidence: 1.0, correction_id, text}]->(other_id)
mark_outdated   SET stale = true, stale_reason = text, stale_by = 'correction' on target_id

When a correction is no longer approved, its add_fact edge is deleted and its stale flag is removed.
A suppressed edge comes back with the next ingestion of its source.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

from .graph.neo4j_writer import Neo4jWriter

KINDS = ("suppress_edge", "add_fact", "mark_outdated")
STATUSES = ("proposed", "approved", "rejected")
LABELS = ("Code", "Source", "Domain", "Requirement", "Summary", "Folder")   # node labels with a unique id
_EDGE = re.compile(r"^[A-Z][A-Z0-9_]*$")

SCHEMA = [
    "CREATE CONSTRAINT correction_id IF NOT EXISTS FOR (n:Correction) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX correction_status IF NOT EXISTS FOR (n:Correction) ON (n.status)",
]


def _node(var: str, key: str) -> str:
    """Subquery that finds one node by id under any anchor label."""
    union = " UNION ".join(f"MATCH ({var}:{label} {{id: row.{key}}}) RETURN {var}" for label in LABELS)
    return f"CALL (row) {{ {union} }}"


def problem(c: dict) -> str | None:
    """Why a correction cannot be applied, or None."""
    if c.get("kind") not in KINDS:
        return f"kind must be one of {', '.join(KINDS)}"
    if not c.get("target_id"):
        return "target_id is required"
    if c["kind"] in ("suppress_edge", "add_fact"):
        if not c.get("other_id"):
            return f"{c['kind']} needs other_id"
        if not _EDGE.match(c.get("edge_type") or ""):
            return f"{c['kind']} needs edge_type in UPPER_SNAKE_CASE"
    return None


def plan(corrections: list[dict]) -> dict:
    """Group corrections into the operations of one apply run."""
    out = {"suppress": defaultdict(list), "add": defaultdict(list), "outdated": [], "undo": [], "invalid": []}
    for c in sorted(corrections, key=lambda c: (str(c.get("created_at") or ""), c["id"])):
        if c.get("status") != "approved":
            if c.get("applied_at"):
                out["undo"].append(c)
            continue
        why = problem(c)
        if why:
            out["invalid"].append({"id": c["id"], "error": why})
        elif c["kind"] == "suppress_edge":
            out["suppress"][c["edge_type"]].append(c)
        elif c["kind"] == "add_fact":
            out["add"][c["edge_type"]].append(c)
        else:
            out["outdated"].append(c)
    return out


def _rows(cs: list[dict]) -> list[dict]:
    return [{"id": c["id"], "target_id": c["target_id"], "other_id": c.get("other_id"), "text": c.get("text"),
             "author": c.get("author"), "edge_type": c.get("edge_type")} for c in cs]


def load(w: Neo4jWriter, status: str | None = None) -> list[dict]:
    return [r.data() for r in w._run(
        "MATCH (c:Correction) WHERE $status IS NULL OR c.status = $status "
        "RETURN c.id AS id, c.kind AS kind, c.target_id AS target_id, c.edge_type AS edge_type, c.other_id AS other_id, "
        "c.text AS text, c.author AS author, toString(c.created_at) AS created_at, c.status AS status, "
        "toString(c.applied_at) AS applied_at, c.applied_count AS applied_count, c.error AS error "
        "ORDER BY created_at, id", status=status).records]


def apply(w: Neo4jWriter) -> dict:
    """Apply every approved correction and undo the withdrawn ones. Idempotent; a few queries per run."""
    for s in SCHEMA:
        w._run(s)
    p = plan(load(w))
    stamp = datetime.now(timezone.utc).isoformat()
    counts: dict[str, int] = {}

    def run(query: str, rows: list[dict]) -> None:
        for r in w._run(query, rows=rows).records:
            counts[r["id"]] = counts.get(r["id"], 0) + r["n"]

    # 1. Undo corrections that were approved before and are not any more.
    if p["undo"]:
        rows = _rows(p["undo"])
        for etype in sorted({c["edge_type"] for c in p["undo"] if c.get("kind") == "add_fact" and _EDGE.match(c.get("edge_type") or "")}):
            w._run(f"UNWIND $rows AS row {_node('a', 'target_id')} {_node('b', 'other_id')} "
                   f"MATCH (a)-[r:{etype} {{correction_id: row.id}}]->(b) DELETE r",
                   rows=[r for r in rows if r["edge_type"] == etype])
        w._run(f"UNWIND $rows AS row {_node('n', 'target_id')} WITH row, n WHERE n.correction_id = row.id "
               "AND n.stale_by = 'correction' REMOVE n.stale, n.stale_reason, n.stale_by, n.correction_id", rows=rows)
        w._run("UNWIND $rows AS row MATCH (c:Correction {id: row.id}) REMOVE c.applied_at, c.applied_count", rows=rows)
    # 2. Suppress edges, then add facts (an added fact wins over a suppression of the same edge).
    for etype, cs in sorted(p["suppress"].items()):
        rows = _rows(cs)
        for r in rows:
            counts.setdefault(r["id"], 0)
        run(f"UNWIND $rows AS row {_node('a', 'target_id')} {_node('b', 'other_id')} "
            f"MATCH (a)-[r:{etype}]->(b) WHERE coalesce(r.method, '') <> 'human' "
            f"WITH row, r DELETE r RETURN row.id AS id, count(*) AS n", rows)
    for etype, cs in sorted(p["add"].items()):
        rows = _rows(cs)
        run(f"UNWIND $rows AS row {_node('a', 'target_id')} {_node('b', 'other_id')} "
            f"MERGE (a)-[r:{etype}]->(b) SET r.method = 'human', r.confidence = 1.0, r.correction_id = row.id, "
            f"r.text = row.text, r.author = row.author RETURN row.id AS id, count(*) AS n", rows)
    # 3. Outdated marks.
    if p["outdated"]:
        run(f"UNWIND $rows AS row {_node('n', 'target_id')} "
            "SET n.stale = true, n.stale_reason = coalesce(row.text, 'marked outdated by ' + coalesce(row.author, 'a reviewer')), "
            "n.stale_by = 'correction', n.correction_id = row.id RETURN row.id AS id, count(*) AS n", _rows(p["outdated"]))
    applied = [{"id": c["id"], "n": counts.get(c["id"], 0)}
               for c in [*sum(p["suppress"].values(), []), *sum(p["add"].values(), []), *p["outdated"]]]
    w._run("UNWIND $rows AS row MATCH (c:Correction {id: row.id}) SET c.applied_at = $stamp, c.applied_count = row.n "
           "REMOVE c.error", rows=applied, stamp=stamp)
    w._run("UNWIND $rows AS row MATCH (c:Correction {id: row.id}) SET c.error = row.error", rows=p["invalid"])
    return {"approved": len(applied) + len(p["invalid"]), "applied": len(applied),
            "suppressed_edges": sum(counts.get(c["id"], 0) for cs in p["suppress"].values() for c in cs),
            "added_facts": sum(counts.get(c["id"], 0) for cs in p["add"].values() for c in cs),
            "outdated_marks": sum(counts.get(c["id"], 0) for c in p["outdated"]),
            "not_found": sum(1 for a in applied if a["n"] == 0 and a["id"] not in
                             {c["id"] for cs in p["suppress"].values() for c in cs}),
            "undone": len(p["undo"]), "invalid": len(p["invalid"])}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("apply", help="apply approved corrections")
    ls = sub.add_parser("list", help="list corrections")
    ls.add_argument("--status", choices=STATUSES)
    args = ap.parse_args(argv)
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        out = apply(w) if args.command == "apply" else load(w, args.status)
        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
