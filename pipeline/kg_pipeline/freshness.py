"""Freshness of documents: mark Document and Section nodes as stale when the code they name changed later.

    kg-freshness                         # stale flags only (no LLM)
    kg-freshness --llm --budget 20       # also ask the LLM whether stale sections contradict the code

Run it after kg-git-history and kg-docs. A Section is stale when a file or symbol that it MENTIONS was
modified by a commit dated after the section's source_updated_at. A Document is stale when one of its
sections is stale or when a commit that REFERENCES the document (feature id such as P14-02) is newer.
The document time is source_updated_at, else updated (Confluence), else ingested_at.

Graph:
    (:Document|Section {stale: true, stale_reason, stale_by: 'freshness', stale_checked_at})
    (:Section) -[:CONFLICTS_WITH {reason, confidence, method: 'llm', content_hash}]-> (:Symbol|File)

Flags set by a human correction (stale_by = 'correction', see corrections.py) are never changed here.
The LLM check runs only on stale sections, at most --budget calls per run; answers are cached.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .domain.extract import _cache_get, _cache_put
from .graph.neo4j_writer import Neo4jWriter
from .models import LLM

log = logging.getLogger(__name__)

PROMPT_VERSION = "conflict-v1"
DEFAULT_BUDGET = int(os.environ.get("FRESHNESS_LLM_BUDGET", "20"))
MIN_CONFIDENCE = 0.5
SECTION_CHARS = 4_000
CODE_CHARS = 6_000
MAX_REASONS = 3

SCHEMA = {"type": "object", "properties": {
    "conflict": {"type": "boolean"},
    "reason": {"type": "string", "description": "one sentence: what the document says and what the code does"},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
    "required": ["conflict", "reason", "confidence"]}

SYSTEM = """You compare a documentation section with the current code that it names.
Set conflict=true only when the section states something that the code clearly does differently
(a different rule, value, field, endpoint, flow or name). Missing detail, a different level of
detail or a statement about code that is not shown is not a conflict.
reason: one sentence that names the statement and the code behaviour. confidence: 0 to 1.
Return only JSON that matches the schema."""


def parse_time(value) -> datetime | None:
    """ISO 8601 text (with Z, offset or no zone), Neo4j DateTime or datetime, as an aware UTC datetime."""
    if value is None or value == "":
        return None
    if hasattr(value, "to_native"):
        value = value.to_native()
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _latest(changes: list[dict], after: datetime) -> list[dict]:
    newer = [c for c in changes if (t := parse_time(c.get("date"))) and t > after]
    return sorted(newer, key=lambda c: parse_time(c["date"]), reverse=True)


def _reason(parts: list[str], when: datetime) -> str:
    head = "; ".join(parts[:MAX_REASONS]) + (f"; and {len(parts) - MAX_REASONS} more" if len(parts) > MAX_REASONS else "")
    return f"{head} -- after the document was updated on {when.date().isoformat()}"


def assess(sections: list[dict], docs: list[dict], code_changes: dict[str, list[dict]],
           doc_changes: dict[str, list[dict]]) -> dict[str, dict]:
    """{node id: {reason, changed: [code ids]}} for every stale Section and Document.

    sections: [{id, doc_id, updated, mentions: [{id, name}]}]; docs: [{id, updated}];
    code_changes / doc_changes: {node id: [{sha, title, date}]} (commits that MODIFIES / REFERENCES it).
    """
    out: dict[str, dict] = {}
    doc_time = {d["id"]: parse_time(d.get("updated")) for d in docs}
    stale_sections: dict[str, list[str]] = {}
    for s in sections:
        when = parse_time(s.get("updated")) or doc_time.get(s["doc_id"])
        if when is None:
            continue
        parts, changed = [], []
        for m in s["mentions"]:
            newer = _latest(code_changes.get(m["id"], []), when)
            if newer:
                c = newer[0]
                parts.append(f"{m['name']} changed in commit {str(c['sha'])[:10]} on "
                             f"{parse_time(c['date']).date().isoformat()} ({c.get('title') or ''})"
                             + (f" and {len(newer) - 1} later commit(s)" if len(newer) > 1 else ""))
                changed.append(m["id"])
        if parts:
            out[s["id"]] = {"reason": _reason(parts, when), "changed": changed}
            stale_sections.setdefault(s["doc_id"], []).append(s.get("title") or s["id"])
    for d in docs:
        when = doc_time.get(d["id"])
        if when is None:
            continue
        parts = [f"feature commit {str(c['sha'])[:10]} on {parse_time(c['date']).date().isoformat()} "
                 f"({c.get('title') or ''})" for c in _latest(doc_changes.get(d["id"], []), when)]
        if d["id"] in stale_sections:
            titles = stale_sections[d["id"]]
            parts.append(f"{len(titles)} stale section(s): {', '.join(titles[:MAX_REASONS])}")
        if parts:
            out[d["id"]] = {"reason": _reason(parts, when), "changed": []}
    return out


def check_conflicts(pairs: list[dict], llm: LLM, budget: int, cache: Path | None) -> tuple[list[dict], dict]:
    """LLM verdict per (section, code) pair, within the budget. pairs: [{src, dst, section, code, name}].
    Returns one result per pair that got an answer (LLM or cache): {src, dst, conflict, reason, confidence, content_hash}."""
    stats = {"pairs": len(pairs), "llm_calls": 0, "llm_cache_hits": 0, "over_budget": 0, "failed": 0, "conflicts": 0}
    results = []
    for p in pairs:
        user = (f"Documentation section:\n{p['section'][:SECTION_CHARS]}\n\n"
                f"Current code of {p['name']}:\n{p['code'][:CODE_CHARS]}")
        h = hashlib.sha256(f"{PROMPT_VERSION}|{user}".encode()).hexdigest()[:20]
        key = hashlib.sha256(f"{PROMPT_VERSION}|{llm.name}|{user}".encode()).hexdigest()[:32]
        answer = _cache_get(cache, key)
        if answer is not None:
            stats["llm_cache_hits"] += 1
        elif budget <= 0:
            stats["over_budget"] += 1
            continue
        else:
            budget -= 1
            try:
                answer = llm.json(SYSTEM, user, SCHEMA)
                answer = {"conflict": bool(answer["conflict"]), "reason": str(answer.get("reason", "")),
                          "confidence": max(0.0, min(1.0, float(answer.get("confidence", 0))))}
            except Exception:
                log.exception("conflict check failed for %s -> %s", p["src"], p["dst"])
                stats["failed"] += 1
                continue
            _cache_put(cache, key, answer)
            stats["llm_calls"] += 1
        conflict = bool(answer["conflict"]) and float(answer["confidence"]) >= MIN_CONFIDENCE
        stats["conflicts"] += conflict
        results.append({"src": p["src"], "dst": p["dst"], "conflict": conflict, "reason": answer["reason"],
                        "confidence": round(float(answer["confidence"]), 2), "content_hash": h})
    return results, stats


# ---- Neo4j ------------------------------------------------------------------------------------------------

def load(w: Neo4jWriter) -> tuple[list[dict], list[dict], dict, dict, dict]:
    sections = [r.data() for r in w._run("""
        MATCH (s:Section)-[:PART_OF]->(d:Document)
        OPTIONAL MATCH (s)-[:MENTIONS]->(x:Code)
        WITH s, d, collect(CASE WHEN x IS NULL THEN NULL ELSE {id: x.id, name: coalesce(x.qualname, x.path)} END) AS mentions
        RETURN s.id AS id, d.id AS doc_id, s.title AS title,
               coalesce(s.source_updated_at, d.source_updated_at, d.updated, d.ingested_at, s.ingested_at) AS updated,
               mentions""").records]
    docs = [r.data() for r in w._run(
        "MATCH (d:Document) RETURN d.id AS id, coalesce(d.source_updated_at, d.updated, d.ingested_at) AS updated").records]
    code_ids = sorted({m["id"] for s in sections for m in s["mentions"]})
    code_changes: dict[str, list[dict]] = {}
    for r in w._run("""
        UNWIND $ids AS id MATCH (c:Commit)-[:MODIFIES]->(x:Code {id: id})
        WITH id, c ORDER BY c.date DESC
        RETURN id, collect({sha: c.sha, title: c.title, date: c.date})[..20] AS commits""", ids=code_ids).records:
        code_changes[r["id"]] = r["commits"]
    doc_changes = {r["id"]: r["commits"] for r in w._run("""
        MATCH (c:Commit)-[:REFERENCES]->(d:Document)
        WITH d, c ORDER BY c.date DESC
        RETURN d.id AS id, collect({sha: c.sha, title: c.title, date: c.date})[..20] AS commits""").records}
    current = {r["id"]: r.data() for r in w._run(
        "MATCH (n:Source) WHERE (n:Document OR n:Section) AND n.stale_by IS NOT NULL "
        "RETURN n.id AS id, n.stale_by AS stale_by, n.stale_reason AS reason").records}
    return sections, docs, code_changes, doc_changes, current


def write_flags(w: Neo4jWriter, stale: dict[str, dict], current: dict[str, dict]) -> dict:
    """Set new or changed flags and clear the ones that are no longer stale. Corrections stay as they are."""
    stamp = datetime.now(timezone.utc).isoformat()
    human = {i for i, c in current.items() if c["stale_by"] != "freshness"}
    rows = [{"id": i, "reason": s["reason"]} for i, s in sorted(stale.items())
            if i not in human and (current.get(i) or {}).get("reason") != s["reason"]]
    w._run("UNWIND $rows AS row MATCH (n:Source {id: row.id}) SET n.stale = true, n.stale_reason = row.reason, "
           "n.stale_by = 'freshness', n.stale_checked_at = $stamp", rows=rows, stamp=stamp)
    cleared = [i for i, c in current.items() if c["stale_by"] == "freshness" and i not in stale]
    w._run("UNWIND $ids AS id MATCH (n:Source {id: id}) REMOVE n.stale, n.stale_reason, n.stale_by, n.stale_checked_at",
           ids=cleared)
    return {"stale": len(stale), "flags_written": len(rows), "flags_cleared": len(cleared),
            "kept_by_correction": len(human & set(stale))}


def _pairs(w: Neo4jWriter, stale: dict[str, dict]) -> list[dict]:
    wanted = [{"src": sid, "dst": cid} for sid, s in sorted(stale.items()) for cid in s["changed"]]
    rows = w._run("""
        UNWIND $rows AS row MATCH (s:Source {id: row.src}) MATCH (x:Code {id: row.dst})
        OPTIONAL MATCH (c:Chunk)-[:PART_OF]->(x)
        WITH row, s, x, c ORDER BY c.start_line
        RETURN row.src AS src, row.dst AS dst, s.title AS title, s.text AS text,
               coalesce(x.qualname, x.path) AS name, collect(c.text) AS code""", rows=wanted).records
    return [{"src": r["src"], "dst": r["dst"], "name": r["name"], "section": f"{r['title'] or ''}\n\n{r['text'] or ''}",
             "code": "\n".join(t for t in r["code"] if t)} for r in rows if r["code"]]


def write_conflicts(w: Neo4jWriter, results: list[dict], candidates: list[dict]) -> dict:
    stamp = datetime.now(timezone.utc).isoformat()
    hits = [r for r in results if r["conflict"]]
    w._run("UNWIND $rows AS row MATCH (s:Source {id: row.src}) MATCH (x:Code {id: row.dst}) "
           "MERGE (s)-[r:CONFLICTS_WITH]->(x) SET r.reason = row.reason, r.confidence = row.confidence, "
           "r.method = 'llm', r.content_hash = row.content_hash, r.checked_at = $stamp", rows=hits, stamp=stamp)
    cleared = [r for r in results if not r["conflict"]]
    w._run("UNWIND $rows AS row MATCH (:Source {id: row.src})-[r:CONFLICTS_WITH {method: 'llm'}]->(:Code {id: row.dst}) "
           "DELETE r", rows=cleared)
    keys = [f"{c['src']}|{c['dst']}" for c in candidates]
    gone = w._run("MATCH (s:Section)-[r:CONFLICTS_WITH {method: 'llm'}]->(x:Code) WHERE NOT s.id + '|' + x.id IN $keys "
                  "DELETE r RETURN count(*) AS n", keys=keys).records
    return {"conflicts_written": len(hits), "conflicts_removed": len(cleared) + (gone[0]["n"] if gone else 0)}


def run(w: Neo4jWriter, llm: LLM | None = None, budget: int = DEFAULT_BUDGET, cache: Path | None = None) -> dict:
    sections, docs, code_changes, doc_changes, current = load(w)
    stale = assess(sections, docs, code_changes, doc_changes)
    stats = {"sections": len(sections), "documents": len(docs), **write_flags(w, stale, current)}
    if llm is not None:
        pairs = _pairs(w, stale)
        results, cstats = check_conflicts(pairs, llm, budget, cache)
        stats.update({f"conflict_{k}": v for k, v in cstats.items()}, **write_conflicts(w, results, pairs))
    return stats


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", action="store_true", help="check stale sections against the code with the LLM")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="maximum LLM calls in this run")
    ap.add_argument("--cache", type=Path, default=Path(os.environ.get("LLM_CACHE_DIR", ".kg-cache/llm")))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        print(json.dumps(run(w, LLM() if args.llm else None, args.budget, args.cache), indent=2))


if __name__ == "__main__":
    main()
