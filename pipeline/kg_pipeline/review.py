"""Review write-back proposals and turn approved ones into corrections.

    kg-review list                                  # open proposals, newest first
    kg-review list --status all --json
    kg-review approve proposal:3f2a... --reviewer sam
    kg-review approve proposal:3f2a... --kind mark_outdated --text "Replaced by the v2 API"
    kg-review reject proposal:3f2a... --reviewer sam --reason "The link is correct"

The MCP tools report_wrong_fact, record_decision and suggest_link and the REST feedback endpoint
create (:Proposal {status: 'open'}). Approve creates one (:Correction) per proposal, which the
pipeline applies on its next run:

    (:Correction {id, kind, target_id, edge_type, other_id, text, author, created_at, status,
                  proposal_id, approved_by, approved_at, links})
    kind: suppress_edge  hide the edge (target_id)-[edge_type]->(other_id)
          add_fact       add the edge, or attach text (a decision) to target_id
          mark_outdated  mark target_id as outdated, with text as the reason
    status: active (set here); the pipeline may later set applied or obsolete

Default kinds: a wrong edge -> suppress_edge, a wrong node -> mark_outdated, a decision or a link
-> add_fact, feedback "wrong"/"outdated" -> mark_outdated, other feedback -> add_fact.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from datetime import datetime, timezone

from .graph.neo4j_writer import Neo4jWriter

KINDS = ("suppress_edge", "add_fact", "mark_outdated")
SCHEMA = [
    "CREATE CONSTRAINT proposal_id IF NOT EXISTS FOR (n:Proposal) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX proposal_status IF NOT EXISTS FOR (n:Proposal) ON (n.status)",
    "CREATE CONSTRAINT correction_id IF NOT EXISTS FOR (n:Correction) REQUIRE n.id IS UNIQUE",
]


def default_kind(p: dict) -> str:
    kind = p.get("kind")
    if kind == "wrong_fact":
        return "suppress_edge" if p.get("edge_type") else "mark_outdated"
    if kind == "feedback":
        return "mark_outdated" if p.get("feedback_kind") in ("wrong", "outdated", "incorrect") else "add_fact"
    return "add_fact"   # decision, link


def correction_for(p: dict, reviewer: str, kind: str | None = None, text: str | None = None,
                   target: str | None = None, now: str | None = None) -> dict:
    """The :Correction row for an approved proposal. The id derives from the proposal id, so a
    second approve of the same proposal writes the same node."""
    p = {**p, "target_id": target or p.get("target_id")}
    kind = kind or default_kind(p)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if kind == "suppress_edge" and not (p.get("target_id") and p.get("edge_type") and p.get("other_id")):
        raise ValueError("suppress_edge needs a proposal about an edge (target_id, edge_type, other_id)")
    if not p.get("target_id"):
        raise ValueError("the proposal names no node; give --target")
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = {"id": "correction:" + p["id"].removeprefix("proposal:"), "kind": kind, "target_id": p["target_id"],
           "edge_type": p.get("edge_type") if kind != "mark_outdated" else None,
           "other_id": p.get("other_id") if kind != "mark_outdated" else None,
           "text": text if text is not None else p.get("text") or p.get("title") or "",
           "author": p.get("author"), "created_at": now, "status": "active", "proposal_id": p["id"],
           "approved_by": reviewer, "approved_at": now, "links": p.get("links") or None}
    return {k: v for k, v in row.items() if v is not None}


def list_proposals(w: Neo4jWriter, status: str = "open", limit: int = 50) -> list[dict]:
    rows = w._run("MATCH (p:Proposal) WHERE $status = 'all' OR p.status = $status "
                  "RETURN p {.*} AS p ORDER BY p.created_at DESC LIMIT $limit", status=status, limit=limit).records
    return [dict(r["p"]) for r in rows]


def _open(w: Neo4jWriter, proposal_id: str) -> dict:
    rows = w._run("MATCH (p:Proposal {id: $id}) RETURN p {.*} AS p", id=proposal_id).records
    if not rows:
        raise SystemExit(f"no proposal {proposal_id}")
    p = dict(rows[0]["p"])
    if p.get("status") != "open":
        raise SystemExit(f"{proposal_id} is {p.get('status')}, not open")
    return p


def approve(w: Neo4jWriter, proposal_id: str, reviewer: str, kind: str | None = None, text: str | None = None,
            target: str | None = None) -> dict:
    for statement in SCHEMA:
        w._run(statement)
    c = correction_for(_open(w, proposal_id), reviewer, kind, text, target)
    w._run("MATCH (p:Proposal {id: $pid, status: 'open'}) MERGE (c:Correction {id: $c.id}) SET c = $c "
           "SET p.status = 'approved', p.reviewed_by = $reviewer, p.reviewed_at = $c.approved_at, p.correction_id = $c.id",
           pid=proposal_id, c=c, reviewer=reviewer)
    return c


def reject(w: Neo4jWriter, proposal_id: str, reviewer: str, reason: str = "") -> dict:
    _open(w, proposal_id)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    w._run("MATCH (p:Proposal {id: $id, status: 'open'}) SET p.status = 'rejected', p.reviewed_by = $reviewer, "
           "p.reviewed_at = $now, p.review_reason = $reason", id=proposal_id, reviewer=reviewer, now=now, reason=reason)
    return {"id": proposal_id, "status": "rejected", "reviewed_by": reviewer}


def _table(rows: list[dict]) -> str:
    out = []
    for p in rows:
        target = p.get("target_id", "")
        if p.get("edge_type"):
            target = f"{target} -[{p['edge_type']}]-> {p.get('other_id', '')}"
        text = (p.get("text") or p.get("title") or "").replace("\n", " ")
        out.append(f"{p['id']}  {p.get('status', ''):8}  {p.get('kind', ''):10}  {p.get('author', ''):12}  "
                   f"{p.get('created_at', '')}\n    {target}\n    {text[:160]}")
    return "\n".join(out) or "no proposals"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--status", default="open", choices=["open", "approved", "rejected", "all"])
    ls.add_argument("--limit", type=int, default=50)
    ls.add_argument("--json", action="store_true")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("--reviewer", default=os.environ.get("KG_REVIEWER") or getpass.getuser())
        if name == "approve":
            p.add_argument("--kind", choices=KINDS, help="override the default correction kind")
            p.add_argument("--text", help="replace the proposal text in the correction")
            p.add_argument("--target", help="node id for a decision without links")
        else:
            p.add_argument("--reason", default="")
    args = ap.parse_args(argv)
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        if args.cmd == "list":
            rows = list_proposals(w, args.status, args.limit)
            print(json.dumps(rows, indent=2, default=str) if args.json else _table(rows))
        elif args.cmd == "approve":
            print(json.dumps(approve(w, args.id, args.reviewer, args.kind, args.text, args.target), indent=2))
        else:
            print(json.dumps(reject(w, args.id, args.reviewer, args.reason), indent=2))
