"""Measure retrieval and verification quality against a fixed evaluation set.

    kg-eval eval/precept.yaml                 # print a report
    kg-eval eval/precept.yaml --json out.json # also write the numbers (compare runs in CI)
    kg-eval eval/precept.yaml --min-hit 0.8   # exit 1 below this search hit rate
    kg-eval eval/fixture.yaml --min-hit 0.8 --min-mrr 0.5 --min-impact 1   # CI gates

Metrics: search hit@k and MRR (rank of the first expected symbol), impact recall, requirement
verdict accuracy, and for answer cases citation recall, citation validity and fact coverage of the
MCP `ask` tool (KG_MCP_URL; see eval_answer.py). Run it after every change to parsing, search or prompts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml

from .eval_answer import eval_answers
from .graph.neo4j_writer import Neo4jWriter
from .models import Embedder
from .search import candidates, expand


def matches(expected: str, symbol: str | None, path: str | None) -> bool:
    symbol, path = symbol or "", path or ""
    return (symbol == expected or symbol.endswith("." + expected) or symbol.rsplit(".", 1)[-1] == expected
            or path.endswith(expected) or Path(path).name == expected)


def first_rank(expect: list[str], results: list[dict]) -> int | None:
    for i, r in enumerate(results, 1):
        if any(matches(x, r["symbol"], r["path"]) for x in expect):
            return i
    return None


def eval_search(w: Neo4jWriter, emb: Embedder, repo: str, cases: list[dict], k: int) -> dict:
    rows, t0 = [], time.time()
    for c in cases:
        hits = candidates(w, emb, repo, c["q"], k=k)
        results = expand(w, [h[0] for h in hits])
        rank = first_rank(c["expect"], results)
        rows.append({"q": c["q"], "rank": rank, "top": [r["symbol"] for r in results[:3]]})
    found = [r for r in rows if r["rank"]]
    return {"cases": len(rows), "hit_at_k": round(len(found) / max(1, len(rows)), 3),
            "hit_at_3": round(sum(1 for r in found if r["rank"] <= 3) / max(1, len(rows)), 3),
            "mrr": round(sum(1 / r["rank"] for r in found) / max(1, len(rows)), 3),
            "seconds_per_query": round((time.time() - t0) / max(1, len(rows)), 2), "rows": rows}


def eval_impact(w: Neo4jWriter, repo: str, cases: list[dict]) -> dict:
    rows = []
    for c in cases:
        affected = [r.data() for r in w._run(f"""
            MATCH (t:Symbol {{repo: $repo}}) WHERE t.qualname = $s OR t.name = $s
            MATCH (a:Symbol)-[:CALLS|USES_TYPE|IMPLEMENTS|EXTENDS|OVERRIDES*1..{int(c.get('depth', 3))}]->(t)
            RETURN DISTINCT a.qualname AS symbol, a.path AS path""", repo=repo, s=c["symbol"]).records]
        if "expect" in c:
            ok = all(any(matches(x, a["symbol"], a["path"]) for a in affected) for x in c["expect"])
        else:  # expect_any: substrings, e.g. test name prefixes
            ok = any(x in (a["symbol"] or "") or x in (a["path"] or "") for a in affected for x in c["expect_any"])
        rows.append({"symbol": c["symbol"], "ok": ok, "affected": len(affected)})
    return {"cases": len(rows), "recall": round(sum(r["ok"] for r in rows) / max(1, len(rows)), 3), "rows": rows}


def eval_requirements(w: Neo4jWriter, repo: str, cases: list[dict]) -> dict:
    rows = []
    for c in cases:
        got = w._run("MATCH (r:Requirement {repo: $repo, ref: $ref}) RETURN r.verdict AS v LIMIT 1",
                     repo=repo, ref=c["ref"]).records
        rows.append({"ref": c["ref"], "expected": c["verdict"], "got": got[0]["v"] if got else None})
    checked = [r for r in rows if r["got"]]
    return {"cases": len(rows), "checked": len(checked),
            "accuracy": round(sum(r["got"] == r["expected"] for r in checked) / max(1, len(checked)), 3), "rows": rows}


def report(name: str, res: dict) -> str:
    s, i, q = res["search"], res["impact"], res["requirements"]
    out = [f"# Evaluation: {name}", "",
           "| Metric | Value |", "|---|---|",
           f"| Search hit@{res['k']} | {s['hit_at_k']:.0%} ({s['cases']} questions) |",
           f"| Search hit@3 | {s['hit_at_3']:.0%} |",
           f"| Search MRR | {s['mrr']:.3f} |",
           f"| Seconds per query | {s['seconds_per_query']} |",
           f"| Impact recall | {i['recall']:.0%} ({i['cases']} cases) |",
           f"| Verdict accuracy | {q['accuracy']:.0%} ({q['checked']} of {q['cases']} checked) |", "",
           "## Search misses", ""]
    misses = [r for r in s["rows"] if not r["rank"]]
    out += [f"- {r['q']} → top: {', '.join(map(str, r['top']))}" for r in misses] or ["- none"]
    wrong = [r for r in q["rows"] if r["got"] and r["got"] != r["expected"]]
    out += ["", "## Wrong verdicts", ""] + ([f"- {r['ref']}: expected {r['expected']}, got {r['got']}" for r in wrong] or ["- none"])
    bad = [r for r in i["rows"] if not r["ok"]]
    out += ["", "## Impact misses", ""] + ([f"- {r['symbol']} ({r['affected']} affected)" for r in bad] or ["- none"])
    a = res.get("answer") or {}
    if a.get("skipped"):
        out += ["", "## Answers", "", f"- skipped ({a['cases']} cases): {a['skipped']}"]
    elif a.get("cases"):
        out += ["", "## Answers", "", "| Metric | Value |", "|---|---|",
                f"| Citation recall | {a['citation_recall']:.0%} ({a['cases']} questions) |",
                f"| Citation validity | {a['citation_validity']:.0%} |",
                f"| Fact coverage | {a['fact_coverage']:.0%} ({a['judge']} judge) |",
                f"| Seconds per question | {a['seconds_per_question']} |", ""]
        out += [f"- {r['question']}: missing citations {r['missing_citations']}, missing facts {r['missing_facts']}"
                for r in a["rows"] if r["missing_citations"] or r["missing_facts"] or r["invalid_citations"]] or ["- no misses"]
    return "\n".join(out) + "\n"


def run(w: Neo4jWriter, emb: Embedder, spec: dict, judge: str = "string", mcp_url: str | None = None) -> dict:
    repo, k = spec["repo"], int(spec.get("k", 8))
    llm = None
    if judge == "llm":
        from .models import LLM

        llm = LLM()
    cache = Path(os.environ["LLM_CACHE_DIR"]) if os.environ.get("LLM_CACHE_DIR") else None
    return {"repo": repo, "k": k,
            "search": eval_search(w, emb, repo, spec.get("search", []), k),
            "impact": eval_impact(w, repo, spec.get("impact", [])),
            "requirements": eval_requirements(w, repo, spec.get("requirements", [])),
            "answer": eval_answers(w, repo, spec.get("answer", []), mcp_url, judge, llm, cache)}


def gate_failures(res: dict, gates: dict[str, float]) -> list[str]:
    """Metrics below their threshold. Answer gates apply only when the answer cases ran."""
    a = res.get("answer") or {}
    values = {"min_hit": res["search"]["hit_at_k"], "min_mrr": res["search"]["mrr"],
              "min_impact": res["impact"]["recall"] if res["impact"]["cases"] else None}
    if a.get("cases") and not a.get("skipped"):
        values |= {"min_citation_recall": a["citation_recall"], "min_citation_validity": a["citation_validity"],
                   "min_fact_coverage": a["fact_coverage"]}
    return [f"{name[4:]} {values[name]} < {limit}" for name, limit in gates.items()
            if limit and values.get(name) is not None and values[name] < limit]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--min-hit", type=float, default=0.0, help="exit 1 below this search hit@k")
    ap.add_argument("--min-mrr", type=float, default=0.0, help="exit 1 below this search MRR")
    ap.add_argument("--min-impact", type=float, default=0.0, help="exit 1 below this impact recall")
    ap.add_argument("--min-citation-recall", type=float, default=0.0)
    ap.add_argument("--min-citation-validity", type=float, default=0.0)
    ap.add_argument("--min-fact-coverage", type=float, default=0.0)
    ap.add_argument("--judge", choices=["string", "llm"], default="string", help="fact check for answer cases")
    ap.add_argument("--mcp-url", help="MCP endpoint for answer cases (default: KG_MCP_URL)")
    args = ap.parse_args(argv)
    spec = yaml.safe_load(args.spec.read_text(encoding="utf-8"))
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        res = run(w, Embedder(), spec, args.judge, args.mcp_url)
    print(report(args.spec.name, res))
    if args.json:
        args.json.write_text(json.dumps(res, indent=2), encoding="utf-8")
    failed = gate_failures(res, {k: v for k, v in vars(args).items() if k.startswith("min_")})
    if failed:
        print("Gate failed: " + "; ".join(failed), file=sys.stderr)
        sys.exit(1)
