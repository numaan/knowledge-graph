"""Answer evaluation for kg-eval: ask the MCP `ask` tool a question and score its answer.

Case format (in the `answer:` list of an evaluation file):

    - question: Who may invite an owner to an organisation?
      expect: [create_invitation, routers/orgs.py]   # node ids, qualified names, short names or path suffixes
      facts: [owner, [403, forbidden]]               # each fact must be in the answer; a list gives alternatives

Metrics per case, then the mean over all cases:
- citation recall:   share of `expect` items that one of the citations matches
- citation validity: share of citations whose id is a node in the graph (no invented sources)
- fact coverage:     share of `facts` found in the answer (string match, or an LLM judge with --judge llm)

The cases are skipped, not failed, when KG_MCP_URL is not set or the server has no `ask` tool.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .mcp_client import MCPClient, MCPError

JUDGE_VERSION = "answer-judge-1"
JUDGE_SCHEMA = {"type": "object", "required": ["found"],
                "properties": {"found": {"type": "array", "items": {"type": "boolean"}}}}


def split_id(node_id: str) -> tuple[str, str]:
    """(symbol, path) of a code node id <repo>:<path>#<qualname>; other ids give (id, "")."""
    if "#" in node_id and ":" in node_id.split("#", 1)[0]:
        head, symbol = node_id.split("#", 1)
        return symbol, head.split(":", 1)[1]
    if ":" in node_id and "/" in node_id:  # a file id <repo>:<path>
        return "", node_id.split(":", 1)[1]
    return node_id, ""


def cited(expected: str, citation: dict) -> bool:
    from .evaluate import matches

    cid = str(citation.get("id") or "")
    if cid == expected or (cid and cid.endswith(expected) and len(expected) > 3 and "/" in expected):
        return True
    symbol, path = split_id(cid)
    symbol = citation.get("symbol") or citation.get("qualname") or symbol
    path = citation.get("path") or path
    return matches(expected, symbol, path)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def facts_by_string(answer: str, facts: list) -> list[bool]:
    text = _norm(answer)
    return [any(_norm(str(alt)) in text for alt in (f if isinstance(f, list) else [f])) for f in facts]


def facts_by_llm(llm, answer: str, question: str, facts: list, cache: Path | None) -> list[bool]:
    """Ask the LLM which facts the answer states. Cached by content, so a rerun costs nothing."""
    items = [" or ".join(map(str, f)) if isinstance(f, list) else str(f) for f in facts]
    key = hashlib.sha256(json.dumps([JUDGE_VERSION, llm.name, question, answer, items]).encode()).hexdigest()
    path = cache / f"answer-{key[:32]}.json" if cache else None
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    out = llm.json("You check whether an answer states given facts. Reply with one boolean per fact, in order.",
                   f"Question: {question}\n\nAnswer:\n{answer[:8000]}\n\nFacts:\n" +
                   "\n".join(f"{i + 1}. {t}" for i, t in enumerate(items)), JUDGE_SCHEMA)
    found = [bool(x) for x in out.get("found", [])][:len(items)]
    found += [False] * (len(items) - len(found))
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(found), encoding="utf-8")
    return found


def existing_ids(w, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    rows = w._run("UNWIND $ids AS id WITH id WHERE EXISTS { MATCH (:Code {id: id}) } OR EXISTS { MATCH (:Source {id: id}) } "
                  "OR EXISTS { MATCH (:Domain {id: id}) } OR EXISTS { MATCH (:Requirement {id: id}) } RETURN id",
                  ids=ids).records
    return {r["id"] for r in rows}


def _payload(result: dict) -> tuple[str, list[dict]]:
    data = result.get("data", result) if isinstance(result.get("data"), dict) else result
    citations = [c if isinstance(c, dict) else {"id": c} for c in data.get("citations") or data.get("sources") or []]
    return str(data.get("answer") or ""), citations


def score_case(case: dict, answer: str, citations: list[dict], valid: set[str], found: list[bool]) -> dict:
    expect = case.get("expect", [])
    hits = [x for x in expect if any(cited(x, c) for c in citations)]
    ids = [str(c.get("id")) for c in citations]
    return {"question": case["question"],
            "citation_recall": round(len(hits) / len(expect), 3) if expect else 1.0,
            "citation_validity": round(sum(i in valid for i in ids) / len(ids), 3) if ids else 0.0,
            "fact_coverage": round(sum(found) / len(found), 3) if found else 1.0,
            "missing_citations": [x for x in expect if x not in hits],
            "invalid_citations": [i for i in ids if i not in valid],
            "missing_facts": [f for f, ok in zip(case.get("facts", []), found) if not ok],
            "citations": len(ids), "answer_chars": len(answer)}


def eval_answers(w, repo: str, cases: list[dict], url: str | None = None, judge: str = "string",
                 llm=None, cache: Path | None = None) -> dict:
    url = url or os.environ.get("KG_MCP_URL")
    if not cases:
        return {"cases": 0, "skipped": "no answer cases"}
    if not url:
        return {"cases": len(cases), "skipped": "KG_MCP_URL is not set"}
    try:
        client = MCPClient(url)
        client.initialize()
    except (MCPError, OSError) as e:
        return {"cases": len(cases), "skipped": f"MCP server not reachable: {e}"}
    rows, t0 = [], time.time()
    try:
        if "ask" not in client.tool_names():
            return {"cases": len(cases), "skipped": "the MCP server has no ask tool"}
        for c in cases:
            try:
                answer, citations = _payload(client.call("ask", {"question": c["question"], "repo": repo}))
            except MCPError as e:
                answer, citations = f"error: {e}", []
            facts = c.get("facts", [])
            found = (facts_by_llm(llm, answer, c["question"], facts, cache) if judge == "llm" and facts
                     else facts_by_string(answer, facts))
            valid = existing_ids(w, [str(x.get("id")) for x in citations])
            rows.append(score_case(c, answer, citations, valid, found))
    finally:
        client.close()
    mean = lambda k: round(sum(r[k] for r in rows) / max(1, len(rows)), 3)
    return {"cases": len(rows), "judge": judge, "citation_recall": mean("citation_recall"),
            "citation_validity": mean("citation_validity"), "fact_coverage": mean("fact_coverage"),
            "seconds_per_question": round((time.time() - t0) / max(1, len(rows)), 2), "rows": rows}
