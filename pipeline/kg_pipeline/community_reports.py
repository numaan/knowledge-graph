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

import math
from collections import defaultdict

from .communities import EDGE_WEIGHTS, PROMPT_VERSION  # noqa: F401  (PROMPT_VERSION is re-exported)
from .summaries import CONTEXT_CHARS, _first_sentence

EXTRACTIVE = "extractive"
LOAD_BATCH = 200          # community ids per query
MAX_MEMBERS = 40          # members listed in the context (highest degree first)
MAX_EDGES = 30            # relationships listed in the context (highest weight first)
MAX_SUMMARIES = 20        # member summaries in the context
MEMBER_TEXT_CHARS = 300
SUMMARY_CHARS = 500
RANK_MAX = 10.0

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
