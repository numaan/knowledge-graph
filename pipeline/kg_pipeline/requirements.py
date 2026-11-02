"""Check document requirements against the code graph.

    document requirement ─┐
                          ├─> candidate match ─> expand related code and tests ─> check against evidence
    code graph ───────────┘                                                     ─> supported | contradicted | unresolved

    kg-verify --doc docs/features/P10-03-scenario-builder.md --repo precept --out report.md
    kg-verify --ticket PREC-123 --repo precept          # acceptance criteria of a Jira story

Documents: .md/.txt are read directly; .pdf/.docx/.pptx/.html go through Docling Serve (DOCLING_URL).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .domain.extract import _cache_get, _cache_put
from .graph.neo4j_writer import Neo4jWriter
from .models import LLM, Embedder
from .search import candidates, expand
from .vectors import ensure_indexes

log = logging.getLogger("kg_pipeline.requirements")

PROMPT_VERSION = "requirements-v1"   # extraction prompt; a change re-extracts every document
JUDGE_VERSION = "judge-v2"
SECTION_CHARS = 8_000
EVIDENCE_CHARS = 30_000   # about 8-9k tokens: fits a 16k-token context with the prompt and answer
SYMBOL_CHARS = 8_000      # full code of one candidate symbol (all its chunks), cut at this size
RELATED_FROM = 4          # top candidates whose callers and called code are added as excerpts
EXCERPT_LINES = 12        # lines of context on each side of a call site
DEFINITION_LINES = 30     # lines shown from a definition found in an imported file
VERDICTS = ["supported", "contradicted", "unresolved"]

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"requirements": {"type": "array", "items": {"type": "object", "properties": {
        "ref": {"type": "string", "description": "Identifier used in the document (e.g. FR-3, AC-2), or empty"},
        "statement": {"type": "string", "description": "One atomic, testable requirement"},
        "type": {"type": "string", "enum": ["functional", "non_functional", "business_rule", "constraint"]}},
        "required": ["ref", "statement", "type"]}}},
    "required": ["requirements"],
}
EXTRACT_SYSTEM = """You extract requirements from a software specification.
- One atomic, testable statement per requirement (split "A and B" into two).
- Keep the document's identifiers (FR-1, AC-3, ...) in ref when present.
- Include acceptance criteria and business rules. Skip background text, goals, open questions and
  implementation tasks that are not observable behaviour.
Return only JSON that matches the schema."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "object", "properties": {
            "symbol": {"type": "string"}, "explanation": {"type": "string"}},
            "required": ["symbol", "explanation"]}},
        "parts": {"type": "array", "description": "Each observable part of the requirement and whether the evidence shows it",
                  "items": {"type": "object", "properties": {"part": {"type": "string"}, "found": {"type": "boolean"}},
                            "required": ["part", "found"]}},
        "gaps": {"type": "string", "description": "What part of the requirement has no evidence, if any"},
    },
    "required": ["verdict", "confidence", "reasoning", "evidence", "parts", "gaps"],
}
JUDGE_SYSTEM = """You verify one software requirement against code evidence from a code graph.
Verdicts:
- supported: the evidence shows code that implements the whole requirement.
- contradicted: the evidence shows code that behaves differently from the requirement (wrong value,
  missing check where the related code clearly handles the case, opposite behaviour).
- unresolved: the evidence is not enough to decide, or only part of the requirement is implemented.
Rules:
- Judge only from the evidence. Absence of evidence is "unresolved", never "contradicted".
- In evidence, cite symbols exactly as written after "Symbol:" and explain what each shows.
- Split the requirement into its observable parts (each field, option, condition, behaviour) and
  mark each part found or not found in the evidence. "supported" needs every part found.
- Put any part of the requirement that you could not find in gaps.
- Excerpts show only part of a symbol: callers that use a candidate, or definitions it calls.
Return only JSON that matches the schema."""


# ---- 1. Document requirements ---------------------------------------------------------------------
def read_document(path: Path) -> str:
    if path.suffix.lower() in {".md", ".markdown", ".txt"}:
        return path.read_text(encoding="utf-8", errors="replace")
    url = os.environ.get("DOCLING_URL", "http://localhost:5001").rstrip("/")
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + \
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"to_formats\"\r\n\r\nmd\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{url}/v1/convert/file", body,
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=900) as resp:
        return json.load(resp)["document"]["md_content"]


def read_ticket(w: Neo4jWriter, key: str) -> str:
    rows = w._run("MATCH (t:Ticket {key: $key}) RETURN t.type AS type, t.title AS title, t.text AS text", key=key).records
    if not rows:
        raise SystemExit(f"ticket {key} is not in the graph; run kg-jira first")
    r = rows[0]
    return f"# {r['type']} {key}: {r['title']}\n\n{r['text'] or ''}"


def sections(markdown: str) -> list[tuple[str, str]]:
    """(heading path, text) split on headings, merged up to SECTION_CHARS."""
    parts, heading, buf = [], [], []
    for line in markdown.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m and buf:
            parts.append((" > ".join(heading), "\n".join(buf)))
            buf = []
        if m:
            heading = heading[:len(m.group(1)) - 1] + [m.group(2).strip()]
        buf.append(line)
    if buf:
        parts.append((" > ".join(heading), "\n".join(buf)))
    merged: list[tuple[str, str]] = []
    for h, t in parts:
        if merged and len(merged[-1][1]) + len(t) < SECTION_CHARS:
            merged[-1] = (merged[-1][0], merged[-1][1] + "\n" + t)
        else:
            merged.append((h, t[:SECTION_CHARS]))
    return merged


def extract_requirements(llm: LLM, doc: str, markdown: str, cache: Path | None) -> list[dict]:
    reqs = []
    for heading, text in sections(markdown):
        key = hashlib.sha256(f"{PROMPT_VERSION}|extract|{llm.name}|{text}".encode()).hexdigest()[:32]
        answer = _cache_get(cache, key) or llm.json(EXTRACT_SYSTEM, f"Document: {doc}\nSection: {heading}\n\n{text}",
                                                    EXTRACT_SCHEMA)
        _cache_put(cache, key, answer)
        for r in answer["requirements"]:
            if len(r["statement"].split()) >= 4:
                reqs.append({**r, "section": heading})
    for i, r in enumerate(reqs, 1):
        r["id"] = f"req:{doc}:{r['ref'] or i}"
    return list({r["id"]: r for r in reqs}.values())


# ---- 2-4. Match, expand, judge ----------------------------------------------------------------------
def _whole_symbols(w: Neo4jWriter, evidence: list[dict]) -> list[dict]:
    """One entry per candidate symbol, with the code of all its chunks (not only the matched one)."""
    unique = list({e["symbol_id"]: e for e in reversed(evidence)}.values())[::-1]
    texts: dict[str, list[str]] = {}
    for r in w._run("MATCH (c:Chunk)-[:PART_OF]->(o) WHERE o.id IN $ids "
                    "RETURN o.id AS id, c.text AS text ORDER BY c.start_line",
                    ids=[e["symbol_id"] for e in unique]).records:
        texts.setdefault(r["id"], []).append(r["text"] or "")
    return [{**e, "text": "\n".join(texts.get(e["symbol_id"], [e["text"] or ""]))} for e in unique]


CALLED_NAME = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")
NOT_CALLS = {"if", "for", "while", "switch", "catch", "return", "function", "new", "typeof", "await", "super",
             "then", "map", "filter", "forEach", "reduce", "push", "set", "get", "has", "add", "trim", "join",
             "split", "slice", "find", "some", "every", "keys", "values", "entries", "String", "Number", "Boolean",
             "Set", "Map", "Array", "Object", "JSON", "Promise", "print", "len", "str", "int", "range", "isinstance"}


def _window(text: str, start_line: int, hit: int, before: int, after: int) -> tuple[int, str]:
    lines = text.split("\n")
    lo, hi = max(0, hit - before), min(len(lines), hit + after + 1)
    return start_line + lo, "\n".join(lines[lo:hi])


def _related(w: Neo4jWriter, evidence: list[dict]) -> list[dict]:
    """Excerpts the candidates alone miss.

    - callers: the lines around each call site of a candidate (e.g. the router that renders a view);
    - called code in imported files that the call graph could not resolve, such as a store method
      reached through `useStore(s => s.save)`: the definition of each called name.
    """
    top = evidence[:RELATED_FROM]
    seen = {e["symbol_id"] for e in evidence}
    out: list[dict] = []
    callers = w._run("""
        UNWIND $ids AS id
        MATCH (s:Symbol {id: id})
        OPTIONAL MATCH (owner:Symbol)-[:CONTAINS]->(s)
        UNWIND [x IN [s, owner] WHERE x IS NOT NULL] AS t   // a method's component is rendered by its caller
        MATCH (t)<-[e:CALLS]-(a:Symbol) WHERE e.confidence >= 0.7 AND NOT a.id IN $seen
          AND NOT (a)-[:CONTAINS]->(t)
        WITH t, a ORDER BY a.path LIMIT 12
        MATCH (c:Chunk)-[:PART_OF]->(a)
        RETURN t.name AS target, a.id AS id, a.qualname AS symbol, a.kind AS kind, a.path AS path,
               c.start_line AS start, c.text AS text ORDER BY a.id, c.start_line""",
                     ids=[e["symbol_id"] for e in top], seen=list(seen)).records
    for r in callers:
        lines = (r["text"] or "").split("\n")
        hit = next((i for i, l in enumerate(lines) if re.search(rf"\b{re.escape(r['target'])}\b", l)), None)
        if hit is None or r["id"] in {o["symbol_id"] for o in out}:
            continue
        start, code = _window(r["text"], r["start"], hit, EXCERPT_LINES, EXCERPT_LINES)
        out.append({"symbol_id": r["id"], "symbol": r["symbol"], "kind": "excerpt: caller of " + r["target"],
                    "role": None, "path": r["path"], "start": start, "text": code})

    resolved = {n.rsplit(".", 1)[-1] for e in top for n in e["callees"]}
    for e in top:
        names = {m for m in CALLED_NAME.findall(e["text"] or "")} - NOT_CALLS - resolved
        if not names:
            continue
        rows = w._run("""
            MATCH (s:Symbol {id: $id})<-[:DEFINES]-(f:File)-[:IMPORTS]->(g:File)<-[:PART_OF]-(c:Chunk)
            RETURN DISTINCT g.id AS file, g.path AS path, c.id AS chunk, c.start_line AS start, c.text AS text
            ORDER BY g.path, c.start_line LIMIT 200""", id=e["symbol_id"]).records
        rows += w._run("""
            MATCH (s:Symbol {id: $id})<-[:DEFINES]-(f:File)-[:IMPORTS]->(g:File)-[:DEFINES]->(o)<-[:PART_OF]-(c:Chunk)
            RETURN DISTINCT g.id AS file, g.path AS path, c.id AS chunk, c.start_line AS start, c.text AS text
            ORDER BY g.path, c.start_line LIMIT 200""", id=e["symbol_id"]).records
        defn = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def\s+|function\s+|const\s+|let\s+|var\s+)?"
                          rf"({'|'.join(map(re.escape, sorted(names)))})\s*(?::|=|\()")
        for r in rows:
            lines = (r["text"] or "").split("\n")
            for i, l in enumerate(lines):
                m = defn.match(l)
                if not m or f"{r['chunk']}@{i}" in seen:
                    continue
                seen.add(f"{r['chunk']}@{i}")
                start, code = _window(r["text"], r["start"], i, 2, DEFINITION_LINES)
                out.append({"symbol_id": f"{r['file']}#L{start}", "symbol": f"{Path(r['path']).stem}.{m.group(1)}",
                            "kind": f"excerpt: definition of {m.group(1)} called by {e['symbol']}",
                            "role": None, "path": r["path"], "start": start, "text": code})
    return [{"concepts": [], "rules": [], "callers": [], "callees": [], "tests": [], **o} for o in out]


def _evidence_text(evidence: list[dict]) -> str:
    out, budget = [], EVIDENCE_CHARS
    for e in evidence:
        lines = [f"### Symbol: {e['symbol']} ({e['kind']}{', ' + e['role'] if e['role'] else ''})",
                 f"File: {e['path']}" + (f" from line {e['start']}" if e.get("start") else "")]
        for label in ("concepts", "rules", "callers", "callees"):
            if e[label]:
                lines.append(f"{label.capitalize()}: {', '.join(e[label][:6])}")
        if e["tests"]:
            lines.append(f"Tests reaching it: {', '.join(t['symbol'] for t in e['tests'])}")
        code = e["text"] or ""
        if len(code) > SYMBOL_CHARS:
            code = code[:SYMBOL_CHARS] + "\n... (cut)"
        block = "\n".join(lines) + "\n```\n" + code + "\n```\n"
        if len(block) > budget:
            continue  # a smaller symbol further down may still fit
        out.append(block)
        budget -= len(block)
    return "\n".join(out)


def _match_citation(name: str, evidence: list[dict]) -> dict | None:
    """Exact symbol, qualified suffix, unique short name, or file name: models cite in many forms."""
    n = re.sub(r"^Symbol:\s*", "", name.strip().strip("`"))
    n = re.sub(r"\s*\(.*$", "", n).strip("` ")   # "x (Function)", "x()"
    for test in (lambda e: e["symbol"] == n,
                 lambda e: e["symbol"].endswith("." + n) or n.endswith("." + e["symbol"]),
                 lambda e: e["symbol"].rsplit(".", 1)[-1] == n.rsplit(".", 1)[-1],
                 lambda e: Path(e["path"]).stem == Path(n).stem or e["path"].endswith(n)):
        hits = [e for e in evidence if test(e)]
        if len({h["symbol_id"] for h in hits}) == 1 or (hits and len({h["path"] for h in hits}) == 1):
            return hits[0]  # several excerpts of one file: the best-ranked one stands for the file
    return None


def verify(w: Neo4jWriter, llm: LLM, emb: Embedder, repo: str, req: dict, cache: Path | None, k: int = 8) -> dict:
    vec = emb.query(req["statement"])
    hits = candidates(w, emb, repo, req["statement"], k=k, vector=vec)
    evidence = _whole_symbols(w, expand(w, [h[0] for h in hits]))
    # the best candidates first, then what calls them or what they call, then the rest
    evidence = evidence[:RELATED_FROM] + _related(w, evidence) + evidence[RELATED_FROM:]
    prompt = f"Requirement ({req['type']}): {req['statement']}\n\nEvidence from repository {repo}:\n\n{_evidence_text(evidence)}"
    key = hashlib.sha256(f"{JUDGE_VERSION}|judge|{llm.name}|{prompt}".encode()).hexdigest()[:32]
    answer = _cache_get(cache, key)
    if answer is None:
        answer = llm.json(JUDGE_SYSTEM, prompt, JUDGE_SCHEMA)
        _cache_put(cache, key, answer)
    cited = []
    for c in answer["evidence"]:
        e = _match_citation(c["symbol"], evidence)
        if e and all(x["symbol_id"] != e["symbol_id"] for x in cited):  # citations outside the evidence are dropped
            cited.append({"symbol_id": e["symbol_id"], "symbol": e["symbol"], "path": e["path"],
                          "explanation": c["explanation"], "tests": e["tests"]})
    verdict, gaps = consistent(answer["verdict"], answer["gaps"], answer.get("parts", []), bool(cited))
    return {**req, "repo": repo, "verdict": verdict, "confidence": round(float(answer["confidence"]), 2),
            "reasoning": answer["reasoning"], "gaps": gaps, "cited": cited, "vector": vec,
            "parts": answer.get("parts", []), "candidates": [e["symbol"] for e in evidence]}


def consistent(verdict: str, gaps: str, parts: list[dict], cited: bool) -> tuple[str, str]:
    """Hold the model to its own findings: no citation or a missing part means it cannot be supported."""
    if verdict != "unresolved" and not cited:
        return "unresolved", (gaps + " (no valid citation)").strip()
    missing = [p["part"] for p in parts if not p.get("found")]
    if verdict == "supported" and missing:
        return "unresolved", (gaps + " (parts not found: " + "; ".join(missing) + ")").strip()
    return verdict, gaps


def write(w: Neo4jWriter, doc: str, results: list[dict]) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    w._run("MATCH (r:Requirement {source: $doc}) DETACH DELETE r", doc=doc)
    w._run("""
        UNWIND $rows AS row
        MERGE (r:Requirement {id: row.id})
        SET r.source = $doc, r.ref = row.ref, r.statement = row.statement, r.type = row.type,
            r.section = row.section, r.repo = row.repo, r.verdict = row.verdict, r.confidence = row.confidence,
            r.reasoning = row.reasoning, r.gaps = row.gaps, r.embedding = row.vector, r.checked_at = $stamp""",
           rows=results, doc=doc, stamp=stamp)
    # supported -> IMPLEMENTED_BY, contradicted -> CONTRADICTED_BY, unresolved -> weak IMPLEMENTED_BY (0.3)
    for rel, verdicts in (("IMPLEMENTED_BY", {"supported", "unresolved"}), ("CONTRADICTED_BY", {"contradicted"})):
        rows = [{"rid": r["id"], "sid": c["symbol_id"], "explanation": c["explanation"],
                 "confidence": r["confidence"] if r["verdict"] != "unresolved" else 0.3}
                for r in results if r["verdict"] in verdicts for c in r["cited"]]
        w._run(f"UNWIND $rows AS row MATCH (r:Requirement {{id: row.rid}}) MATCH (s:Code {{id: row.sid}}) "
               f"MERGE (r)-[e:{rel}]->(s) SET e.explanation = row.explanation, e.method = 'llm', "
               f"e.confidence = row.confidence", rows=rows)
    # the ticket or document that states the requirements
    w._run("MATCH (d:Source) WHERE d.id = $doc OR (d:Document AND d.name = $doc) "
           "MATCH (r:Requirement {source: $doc}) MERGE (d)-[:SPECIFIES]->(r)", doc=doc)
    tests = [{"rid": r["id"], "tid": t["id"]} for r in results if r["verdict"] == "supported"
             for c in r["cited"] for t in c["tests"]]
    w._run("UNWIND $rows AS row MATCH (r:Requirement {id: row.rid}) MATCH (t:Symbol {id: row.tid}) "
           "MERGE (r)-[v:VALIDATED_BY]->(t) SET v.method = 'graph', v.confidence = 0.5", rows=tests)


def report(doc: str, repo: str, results: list[dict]) -> str:
    counts = {v: sum(r["verdict"] == v for r in results) for v in VERDICTS}
    lines = [f"# Requirement check: {doc}", "", f"Repository: `{repo}`  ", f"Checked: {len(results)} requirements  ",
             "", "| Verdict | Count |", "|---|---|", *[f"| {v} | {n} |" for v, n in counts.items()], "",
             "| Ref | Requirement | Verdict | Conf. | Evidence | Tests | Gaps |", "|---|---|---|---|---|---|---|"]
    esc = lambda s: (s or "").replace("|", "\\|").replace("\n", " ")
    for r in results:
        ev = "<br>".join(f"`{c['symbol']}`" for c in r["cited"]) or "-"
        tests = "<br>".join(sorted({t["symbol"] for c in r["cited"] for t in c["tests"]})) or "-"
        lines.append(f"| {esc(r['ref']) or '-'} | {esc(r['statement'])} | **{r['verdict']}** | {r['confidence']} "
                     f"| {ev} | {tests} | {esc(r['gaps']) or '-'} |")
    lines += ["", "## Reasoning", ""]
    for r in results:
        lines += [f"- **{esc(r['ref']) or r['id']}** ({r['verdict']}): {esc(r['reasoning'])}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--doc", type=Path, help="specification file (.md, .pdf, .docx, ...)")
    src.add_argument("--ticket", help="Jira key already in the graph (kg-jira), e.g. PREC-123")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", type=Path, help="markdown report path")
    ap.add_argument("--limit", type=int, help="check only the first N requirements")
    ap.add_argument("--cache", type=Path, default=Path(os.environ.get("LLM_CACHE_DIR", ".kg-cache/llm")))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    llm, emb = LLM(), Embedder()
    doc = args.doc.name if args.doc else f"jira:{args.ticket}"
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        ensure_indexes(w, emb.dimensions)
        reqs = extract_requirements(llm, doc, read_document(args.doc) if args.doc else read_ticket(w, args.ticket),
                                    args.cache)[:args.limit]
        log.info("%d requirements extracted from %s", len(reqs), doc)
        results = []
        for i, r in enumerate(reqs, 1):
            res = verify(w, llm, emb, args.repo, r, args.cache)
            log.info("[%d/%d] %s -> %s (%.2f)", i, len(reqs), r["statement"][:70], res["verdict"], res["confidence"])
            results.append(res)
        write(w, doc, results)
    text = report(doc, args.repo, results)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        log.info("report written to %s", args.out)
    else:
        print(text)


if __name__ == "__main__":
    main()
