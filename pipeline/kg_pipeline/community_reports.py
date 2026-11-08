"""Community reports: the ACL-safe input of one community and its extractive report.

A report describes one community, and a community never mixes ACL sets (see communities.py). The
input of a report is therefore built from the members of that one community only:

    load_report_inputs(w, community_ids)   reads members through (:Node)-[:IN_COMMUNITY]->(:Community),
                                           the edges whose two ends are in the same community and the
                                           summaries (:Summary)-[:SUMMARIZES]->(member)
    report_context(community, members, edges, summaries)
                                           text for the LLM, capped to CONTEXT_CHARS; uses `text_masked`
                                           and summaries only, never the raw `text` of a node
    extractive_report(...)                 title, summary, findings, rank (0..10) without an LLM

The prompt version is the one of communities.py, so a change of the prompt changes the community hash.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .communities import EDGE_WEIGHTS, PROMPT_VERSION, _env_num, ensure_schema  # noqa: F401  (PROMPT_VERSION is re-exported)
from .domain.extract import _cache_get, _cache_put
from .summaries import CONTEXT_CHARS, _first_sentence, _hash

log = logging.getLogger(__name__)

EXTRACTIVE = "extractive"
DEFAULT_BUDGET = 100      # LLM calls per run when COMMUNITY_BUDGET is not set or not a number
LOAD_BATCH = 200          # community ids per query
MAX_MEMBERS = 40          # members listed in the context (highest degree first)
MAX_EDGES = 30            # relationships listed in the context (highest weight first)
MAX_SUMMARIES = 20        # member summaries in the context
MEMBER_TEXT_CHARS = 300
SUMMARY_CHARS = 500
RANK_MAX = 10.0

def budget_from_env() -> int | None:
    """COMMUNITY_BUDGET as an int; None when it is not set or not a number (a bad value never raises)."""
    raw = os.environ.get("COMMUNITY_BUDGET", "").strip()
    if not raw:
        return None
    value = _env_num("COMMUNITY_BUDGET", -1, int)
    return None if value < 0 else value  # a bad or negative value falls back to the default


def default_budget() -> int:
    value = budget_from_env()
    return DEFAULT_BUDGET if value is None else value


# Every query is scoped by `$ids` (community ids). A member is reached only through IN_COMMUNITY of such a
# community, and the raw `text` property is not read (only `text_masked`).
_COMMUNITY_Q = """
MATCH (c:Community) WHERE c.id IN $ids
RETURN c.id AS cid, c.level AS level, c.size AS size, c.acl AS acl, c.repo AS repo, c.content_hash AS content_hash"""

_MEMBERS_Q = """
MATCH (m)-[:IN_COMMUNITY]->(c:Community) WHERE c.id IN $ids
RETURN c.id AS cid, m.id AS mid, labels(m) AS labels,
       coalesce(m.name, m.title, m.qualname, m.path, m.id) AS name, m.text_masked AS text_masked
ORDER BY cid, mid"""

_EDGES_Q = """
MATCH (a)-[:IN_COMMUNITY]->(c:Community) WHERE c.id IN $ids
MATCH (a)-[r]->(b)-[:IN_COMMUNITY]->(c)
WHERE type(r) IN $types AND a.id <> b.id
RETURN c.id AS cid, a.id AS src, b.id AS dst, type(r) AS type
ORDER BY cid, src, dst, type"""

_SUMMARIES_Q = """
MATCH (m)-[:IN_COMMUNITY]->(c:Community) WHERE c.id IN $ids
MATCH (s:Summary)-[:SUMMARIZES]->(m)
RETURN c.id AS cid, m.id AS mid, s.text AS text
ORDER BY cid, mid"""


def load_report_inputs(w, community_ids) -> dict:
    """`{community id: {community, members, edges, summaries}}` for the given communities.

    `community`: `{id, level, size, acl, repo, content_hash}`; `members`: dicts `{id, labels, name,
    text_masked}` sorted by id; `edges`: `(src, dst, type, weight)`; `summaries`: `{member id: text}`.
    """
    ids = sorted(set(community_ids), key=str)
    out: dict = {}
    for i in range(0, len(ids), LOAD_BATCH):
        batch = ids[i:i + LOAD_BATCH]
        for r in w._run(_COMMUNITY_Q, ids=batch).records:
            out[r["cid"]] = {
                "community": {"id": r["cid"], "level": r["level"], "size": r["size"], "acl": list(r["acl"] or []),
                              "repo": r["repo"], "content_hash": r["content_hash"]},
                "members": [], "edges": [], "summaries": {}}
        for r in w._run(_MEMBERS_Q, ids=batch).records:
            if r["cid"] in out:
                out[r["cid"]]["members"].append({"id": r["mid"], "labels": list(r["labels"] or []),
                                                 "name": str(r["name"]), "text_masked": r["text_masked"]})
        for r in w._run(_EDGES_Q, ids=batch, types=sorted(EDGE_WEIGHTS)).records:
            if r["cid"] in out and r["type"] in EDGE_WEIGHTS:
                out[r["cid"]]["edges"].append((r["src"], r["dst"], r["type"], EDGE_WEIGHTS[r["type"]]))
        for r in w._run(_SUMMARIES_Q, ids=batch).records:
            if r["cid"] in out and r["text"]:
                out[r["cid"]]["summaries"][r["mid"]] = r["text"]
    for d in out.values():
        d["community"]["size"] = d["community"]["size"] or len(d["members"])
    return out


# ---- shared view of the input -----------------------------------------------------------------------

def _kind(m: dict) -> str:
    labels = [x for x in m.get("labels") or [] if x not in ("Code", "Domain", "Source")]
    return labels[0] if labels else "Node"


def _degrees(members: list[dict], edges) -> dict:
    ids = {m["id"] for m in members}
    deg: dict = defaultdict(float)
    for u, v, _t, w in edges:
        if u in ids and v in ids:
            deg[u] += w
            deg[v] += w
    return deg


def _ranked(members: list[dict], edges) -> list[dict]:
    deg = _degrees(members, edges)
    return sorted(members, key=lambda m: (-deg.get(m["id"], 0.0), str(m["name"]), str(m["id"])))


def _top_edges(members: list[dict], edges) -> list[tuple]:
    ids = {m["id"] for m in members}
    seen = sorted({e for e in edges if e[0] in ids and e[1] in ids}, key=lambda e: (-e[3], str(e[0]), str(e[1]), e[2]))
    return seen[:MAX_EDGES]


def report_context(community: dict, members: list[dict], edges, summaries: dict) -> str:
    """The LLM input for one community, at most CONTEXT_CHARS characters.

    Lists the top members by degree with their `text_masked`, the top edges and the summaries of the top
    members. A `text` key on a member is ignored. Members outside `members` are never named.
    """
    ranked = _ranked(members, edges)
    names = {m["id"]: str(m["name"]) for m in members}
    lines = [f"Community: {community.get('id')}", f"Level: {community.get('level')}",
             f"Members: {len(members)}"]
    lines.append("Top members (most connected first):")
    for m in ranked[:MAX_MEMBERS]:
        text = " ".join(str(m.get("text_masked") or "").split())[:MEMBER_TEXT_CHARS]
        lines.append(f"- {m['name']} ({_kind(m)})" + (f": {text}" if text else ""))
    if len(ranked) > MAX_MEMBERS:
        lines.append(f"- ... and {len(ranked) - MAX_MEMBERS} more members")
    top = _top_edges(members, edges)
    if top:
        lines.append("Relationships (strongest first):")
        lines += [f"- {names[u]} -{t}-> {names[v]}" for u, v, t, _w in top]
    shown = [(m, summaries[m["id"]]) for m in ranked if summaries.get(m["id"])][:MAX_SUMMARIES]
    if shown:
        lines.append("Member summaries:")
        lines += [f"- {m['name']}: {' '.join(str(s).split())[:SUMMARY_CHARS]}" for m, s in shown]
    return "\n".join(lines)[:CONTEXT_CHARS]


# ---- extractive report ------------------------------------------------------------------------------

def extractive_rank(size: int, total_weight: float) -> float:
    """0 to 10 from the member count and the edge weight (both on a log scale)."""
    score = 1.5 * math.log2(1 + max(size, 0)) + math.log2(1 + max(total_weight, 0.0))
    return round(min(RANK_MAX, max(0.0, score)), 1)


def extractive_report(community: dict, members: list[dict], edges, summaries: dict) -> dict:
    """`{title, summary, findings, rank, model}` built from member names, top edges and member summaries."""
    ranked = _ranked(members, edges)
    names = {m["id"]: str(m["name"]) for m in members}
    top_edges = _top_edges(members, edges)
    n = len(members)
    if not n:
        cid = str(community.get("id"))
        return {"title": f"Empty community {cid}", "summary": f"Community {cid} has no members.",
                "findings": ["The community has no members."], "rank": 0.0, "model": EXTRACTIVE}
    lead = [str(m["name"]) for m in ranked[:3]]
    title = ", ".join(lead) + (f" and {n - 3} more" if n > 3 else "")
    kinds: dict = defaultdict(int)
    for m in members:
        kinds[_kind(m)] += 1
    kind_text = ", ".join(f"{c} {k}" for k, c in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:4])
    summary = [f"Community of {n} member{'s' if n != 1 else ''} ({kind_text}) at level {community.get('level')}.",
               "Main members: " + ", ".join(lead) + "."]
    if top_edges:
        u, v, t, _w = top_edges[0]
        summary.append(f"Strongest relationship: {names[u]} {t} {names[v]}.")
    first = next((summaries[m["id"]] for m in ranked if summaries.get(m["id"])), "")
    if first:
        summary.append(_first_sentence(first).rstrip(".") + ".")
    findings: list[str] = []
    for m in ranked[:5]:
        text = summaries.get(m["id"]) or m.get("text_masked") or ""
        findings.append(f"{m['name']} ({_kind(m)})" + (f": {_first_sentence(str(text), 200)}" if text.strip() else ""))
    findings += [f"{names[u]} {t} {names[v]}" for u, v, t, _w in top_edges[:3]]
    weight = sum(e[3] for e in top_edges) if top_edges else 0.0
    total = sum(w for _u, _v, _t, w in edges)
    return {"title": title, "summary": " ".join(summary), "findings": findings,
            "rank": extractive_rank(n, max(total, weight)), "model": EXTRACTIVE}


# ---- report generation (LLM within a budget, cache, extractive fallback) ----------------------------

REPORT_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "summary": {"type": "string"},
                   "findings": {"type": "array", "items": {"type": "string"}},
                   "rank": {"type": "number", "minimum": 0, "maximum": 10}},
    "required": ["title", "summary", "findings", "rank"]}

SYSTEM = ("You write a report about one community of related items (code, tickets, documents) in a knowledge graph. "
          "Use only the input. Give a short title, a summary of 3 to 5 sentences that says what the community is "
          "about, 3 to 6 findings (one sentence each), and a rank from 0 (minor) to 10 (central) for how important "
          "the community is. Do not invent facts. Return only JSON that matches the schema.")


def _clamp_rank(value) -> float:
    try:
        rank = float(value)
    except (TypeError, ValueError):
        return 0.0
    if rank != rank:
        return 0.0
    return round(min(RANK_MAX, max(0.0, rank)), 1)


def _llm_report(answer: dict) -> dict:
    title = " ".join(str(answer.get("title", "")).split())
    summary = " ".join(str(answer.get("summary", "")).split())
    if not title or not summary:
        raise ValueError("empty title or summary")
    raw = answer.get("findings")
    findings = [" ".join(str(f).split()) for f in (raw if isinstance(raw, list) else []) if str(f).strip()]
    return {"title": title, "summary": summary, "findings": findings, "rank": _clamp_rank(answer.get("rank"))}


def report_id(community_id: str) -> str:
    return f"report:{community_id}"


def _fields(r: dict) -> dict:
    return {"title": r.get("title"), "summary": r.get("summary"), "findings": list(r.get("findings") or []),
            "rank": r.get("rank"), "model": r.get("model")}


class ReportGenerator:
    """Picks the report of each community: kept, cached, LLM (within budget) or extractive."""

    def __init__(self, llm, budget: int, cache: Path | None, existing: dict[str, dict]):
        self.llm, self.budget, self.cache, self.existing = llm, budget, cache, existing
        self.stats: dict = defaultdict(int)

    def __call__(self, community: dict, context: str, extractive: dict) -> dict:
        rid = report_id(community["id"])
        h = _hash(community.get("content_hash"), PROMPT_VERSION, context)
        old = self.existing.get(rid)
        if old and old.get("content_hash") == h and (old.get("model") != EXTRACTIVE or self.llm is None):
            self.stats["unchanged"] += 1
            return {**_fields(old), "content_hash": h, "changed": False}
        if self.llm is not None:
            key = hashlib.sha256(f"{PROMPT_VERSION}|{self.llm.name}|report|{context}".encode()).hexdigest()[:32]
            report = None
            cached = _cache_get(self.cache, key)
            if cached is not None:
                try:
                    report = _llm_report(cached)
                    self.stats["llm_cache_hits"] += 1
                except Exception:
                    report = None
            if report is None:
                if self.budget > 0:
                    self.budget -= 1
                    try:
                        answer = self.llm.json(SYSTEM, context, REPORT_SCHEMA)
                        report = _llm_report(answer)
                        _cache_put(self.cache, key, answer)
                        self.stats["llm_calls"] += 1
                    except Exception:
                        log.exception("report LLM call failed for %s", community["id"])
                        self.stats["llm_failed"] += 1
                else:
                    self.stats["over_budget"] += 1
            if report is not None:
                new = {**report, "model": self.llm.name}
                changed = not old or old.get("content_hash") != h or _fields(old) != new
                return {**new, "content_hash": h, "changed": changed}
            if old and old.get("content_hash") == h:      # extractive and still current: keep it
                self.stats["unchanged"] += 1
                return {**_fields(old), "content_hash": h, "changed": False}
        self.stats["extractive"] += 1
        return {**{k: extractive[k] for k in ("title", "summary", "findings", "rank")}, "model": EXTRACTIVE,
                "content_hash": h, "changed": True}


def embedding_text(row: dict) -> str:
    return "\n".join([row["title"], row["summary"], *row["findings"]])


# ---- Neo4j ------------------------------------------------------------------------------------------

_LIST_Q = """
MATCH (c:Community) WHERE $level IS NULL OR c.level = $level
RETURN c.id AS cid ORDER BY cid"""

_EXISTING_Q = """
MATCH (r:CommunityReport)-[:REPORTS_ON]->(c:Community) WHERE c.id IN $ids
RETURN r.id AS rid, c.id AS cid, r.content_hash AS content_hash, r.model AS model,
       r.embedding_hash AS embedding_hash, r.title AS title, r.summary AS summary, r.findings AS findings,
       r.rank AS rank, r.acl AS acl, r.level AS level"""

_WRITE_Q = """
UNWIND $rows AS row
MERGE (r:CommunityReport {id: row.id})
SET r.title = row.title, r.summary = row.summary, r.findings = row.findings, r.rank = row.rank,
    r.level = row.level, r.acl = row.acl, r.repo = row.repo, r.model = row.model,
    r.content_hash = row.content_hash, r.community_id = row.community_id, r.generated_at = $stamp
WITH r, row
MATCH (c:Community {id: row.community_id})
MERGE (r)-[:REPORTS_ON]->(c)"""

WRITE_ROWS = 200


def write_reports(w, rows: list[dict], stamp: str | None = None) -> int:
    """Write the rows with `changed=True` as `(:CommunityReport)-[:REPORTS_ON]->(:Community)`. Returns the count.
    The report carries the `acl`, `repo` and `level` of its community."""
    todo = [r for r in rows if r["changed"]]
    stamp = stamp or datetime.now(timezone.utc).isoformat()
    keys = ("id", "community_id", "level", "acl", "repo", "title", "summary", "findings", "rank", "model",
            "content_hash")
    for i in range(0, len(todo), WRITE_ROWS):
        w._run(_WRITE_Q, rows=[{k: r.get(k) for k in keys} for r in todo[i:i + WRITE_ROWS]], stamp=stamp)
    return len(todo)


def embed_reports(w, emb, rows: list[dict], existing: dict[str, dict] | None = None) -> int:
    """Embed the reports whose text changed (hash of model and text, see `vectors._embed_label`)."""
    from .vectors import _embed_label
    existing = existing or {}
    items = [{"id": r["id"], "hash": (existing.get(r["id"]) or {}).get("embedding_hash"), "text": embedding_text(r)}
             for r in rows]
    return _embed_label(w, emb, items, "CommunityReport")["embedded"]


def build_reports(w, llm=None, emb=None, level: int | None = None, budget: int | None = None,
                  cache: Path | None = None) -> dict:
    """Load the communities, make a report for each and write the changed ones. `llm=None` or
    `COMMUNITY_LLM=off` gives extractive reports. Embeds changed reports when `emb` is given."""
    if os.environ.get("COMMUNITY_LLM", "").strip().lower() == "off":
        llm = None
    budget = default_budget() if budget is None else budget
    ensure_schema(w, emb.dimensions if emb is not None else None)
    ids = [r["cid"] for r in w._run(_LIST_Q, level=level).records]
    existing: dict[str, dict] = {}
    gen = ReportGenerator(llm, budget, cache, existing)
    rows: list[dict] = []
    for i in range(0, len(ids), LOAD_BATCH):   # members, edges and summaries of one batch at a time
        batch = ids[i:i + LOAD_BATCH]
        for r in w._run(_EXISTING_Q, ids=batch).records:
            existing[r["rid"]] = {k: r[k] for k in ("content_hash", "model", "embedding_hash", "title", "summary",
                                                    "findings", "rank", "acl", "level")}
        inputs = load_report_inputs(w, batch)
        for cid in sorted(inputs):
            d = inputs[cid]
            com = d["community"]
            args = (com, d["members"], d["edges"], d["summaries"])
            res = gen(com, report_context(*args), extractive_report(*args))
            old = existing.get(report_id(cid))
            if old and not res["changed"] and (list(old.get("acl") or []) != com["acl"] or old.get("level") != com["level"]):
                res["changed"] = True                  # the community moved: copy its acl and level again
            rows.append({**res, "id": report_id(cid), "community_id": cid, "level": com["level"], "acl": com["acl"],
                         "repo": com["repo"]})
    stats = {k: gen.stats.get(k, 0) for k in ("llm_calls", "llm_cache_hits", "llm_failed", "extractive",
                                              "over_budget", "unchanged")}
    stats["total"] = len(rows)
    stats["written"] = write_reports(w, rows)
    stats["embedded"] = embed_reports(w, emb, rows, existing) if emb is not None else 0
    stats["budget_left"] = gen.budget
    return stats
