"""Domain layer from code: deterministic roles plus LLM-extracted concepts, rules and capabilities.

Reads the code layer of one repository from Neo4j, groups files by folder ("module"), asks the LLM
for a fixed JSON schema per module, keeps only answers that cite symbols that exist in that module,
and writes :Domain nodes. LLM answers are cached by (model, prompt version, module content).
"""
from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from ..graph.neo4j_writer import Neo4jWriter
from ..models import LLM
from .roles import classify

log = logging.getLogger(__name__)

PROMPT_VERSION = "domain-v1"
CONTEXT_CHARS = 14_000
EXCERPT_CHARS = 1_500
MAX_SYMBOL_LINES = 150
CONCEPT_KINDS = ["entity", "value_object", "process", "role", "event", "policy", "external_system"]

SCHEMA = {
    "type": "object",
    "properties": {
        "capability": {"type": "string", "description": "Short business name of what this module does"},
        "summary": {"type": "string", "description": "2-3 sentences in business terms"},
        "concepts": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "kind": {"type": "string", "enum": CONCEPT_KINDS},
            "definition": {"type": "string"},
            "symbols": {"type": "array", "items": {"type": "string"}}},
            "required": ["name", "kind", "definition", "symbols"]}},
        "rules": {"type": "array", "items": {"type": "object", "properties": {
            "statement": {"type": "string"}, "symbols": {"type": "array", "items": {"type": "string"}}},
            "required": ["statement", "symbols"]}},
    },
    "required": ["capability", "summary", "concepts", "rules"],
}

SYSTEM = """You are a domain analyst. You read source code and describe the business domain it implements.
Rules:
- Use business language, not implementation language (say "Loan application", not "LoanDto").
- concepts: business things, processes, roles, events and policies that this module implements or uses.
  Skip purely technical items (HTTP clients, loggers, config loaders, UI widgets) unless they are business concepts.
- rules: business rules the code enforces, as one testable sentence each (for example
  "A borrower younger than 18 is rejected"). Only rules you can see in the code excerpts.
- symbols: cite the exact qualified names from the symbol list that implement each concept or rule.
- If the module has no business meaning, return empty concepts and rules.
Return only JSON that matches the schema."""


def norm(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", " ", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).lower()).strip()
    words = key.split()
    if words and len(words[-1]) > 3 and words[-1].endswith("s") and not words[-1].endswith("ss"):
        words[-1] = words[-1][:-1]
    return " ".join(words)


def _load(w: Neo4jWriter, repo: str):
    symbols = [r.data() for r in w._run(
        "MATCH (s:Symbol {repo: $repo}) RETURN s.id AS id, s.name AS name, s.qualname AS qualname, s.kind AS kind, "
        "s.path AS path, s.signature AS signature, s.annotations AS annotations, s.supertypes AS supertypes",
        repo=repo).records]
    chunks = [r.data() for r in w._run(
        "MATCH (c:Chunk {repo: $repo})-[:PART_OF]->(o) RETURN c.path AS path, c.text AS text, "
        "c.start_line AS start, c.end_line AS end, o.qualname AS owner", repo=repo).records]
    return symbols, chunks


def _module_context(repo: str, module: str, syms: list[dict], chunks: list[dict]) -> str:
    lines = [f"Repository: {repo}", f"Module (folder): {module or '/'}",
             f"Files: {', '.join(sorted({s['path'].rsplit('/', 1)[-1] for s in syms}))}", "", "Symbols:"]
    for s in syms[:MAX_SYMBOL_LINES]:
        role = f" [{s['role']}]" if s.get("role") else ""
        lines.append(f"- {s['kind']} {s['qualname']}{role}: {s['signature']}")
    lines += ["", "Code excerpts:"]
    # Business-heavy code first: entities, endpoints, services, then the longest functions.
    weight = {"entity": 0, "endpoint": 1, "service": 2, "controller": 3}
    role_of = {s["qualname"]: s.get("role") for s in syms}
    ordered = sorted(chunks, key=lambda c: (weight.get(role_of.get(c["owner"]), 9), -(c["end"] - c["start"])))
    budget = CONTEXT_CHARS - sum(len(l) for l in lines)
    for c in ordered:
        block = f"### {c['path']}:{c['start']}-{c['end']} ({c['owner'] or 'file'})\n{c['text'][:EXCERPT_CHARS]}\n"
        if len(block) > budget:
            continue
        lines.append(block)
        budget -= len(block)
    return "\n".join(lines)


def _cache_get(cache: Path | None, key: str):
    f = cache / f"{key}.json" if cache else None
    return json.loads(f.read_text()) if f and f.exists() else None


def _cache_put(cache: Path | None, key: str, value: dict) -> None:
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"{key}.json").write_text(json.dumps(value))


def extract(w: Neo4jWriter, repo: str, llm: LLM | None, cache: Path | None = None,
            modules: list[str] | None = None, max_modules: int | None = None) -> dict:
    """Run roles + LLM extraction for one repo and write the :Domain layer. llm=None: roles only."""
    run_id, stamp = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
    symbols, chunks = _load(w, repo)
    stats = defaultdict(int)

    # 1. Deterministic roles on Symbol nodes.
    roles = []
    for s in symbols:
        r = classify(s["kind"], s["annotations"], s["supertypes"])
        if r:
            s.update(r)
            roles.append({"id": s["id"], **r})
            stats[f"role_{r['role']}"] += 1
    w._run("MATCH (s:Symbol {repo: $repo}) REMOVE s.role, s.http_method, s.route", repo=repo)
    w._run("UNWIND $rows AS row MATCH (s:Symbol {id: row.id}) "
           "SET s.role = row.role, s.http_method = row.http_method, s.route = row.route", rows=roles)

    concepts: dict[str, dict] = {}        # norm name -> concept
    realises: dict[tuple[str, str], tuple[str, float]] = {}   # (symbol id, concept key) -> (method, conf)
    rules: list[dict] = []
    capabilities: list[dict] = []

    for s in symbols:
        if s.get("role") == "entity":
            key = norm(s["name"])
            concepts.setdefault(key, {"name": s["name"], "kind": "entity", "definition": "", "method": "parser",
                                      "modules": set()})["modules"].add(posixpath.dirname(s["path"]))
            realises[(s["id"], key)] = ("parser", 0.9)

    # 2. LLM per module.
    by_module: dict[str, list[dict]] = defaultdict(list)
    for s in symbols:
        by_module[posixpath.dirname(s["path"])].append(s)
    chunks_by_module: dict[str, list[dict]] = defaultdict(list)
    for c in chunks:
        chunks_by_module[posixpath.dirname(c["path"])].append(c)
    names = sorted(m for m in by_module if modules is None or any(m == x or m.startswith(x + "/") for x in modules))
    if max_modules is not None:
        names = sorted(names, key=lambda m: -len(by_module[m]))[:max_modules]

    for module in names if llm else []:
        syms = sorted(by_module[module], key=lambda s: (s["path"], s["qualname"]))
        context = _module_context(repo, module, syms, chunks_by_module[module])
        key = hashlib.sha256(f"{PROMPT_VERSION}|{llm.name}|{context}".encode()).hexdigest()[:32]
        answer = _cache_get(cache, key)
        if answer is None:
            try:
                answer = llm.json(SYSTEM, context, SCHEMA)
            except Exception:
                log.exception("LLM failed for module %s", module)
                stats["modules_failed"] += 1
                continue
            _cache_put(cache, key, answer)
            stats["llm_calls"] += 1
        else:
            stats["llm_cache_hits"] += 1
        stats["modules_done"] += 1

        # Accept only symbols that exist in this module (the LLM must not invent evidence).
        by_qual = {s["qualname"]: s["id"] for s in syms}
        by_short = defaultdict(list)
        for s in syms:
            by_short[s["name"]].append(s["id"])

        def cited(names_: list[str]) -> list[str]:
            ids = []
            for n in names_:
                n = n.strip().split("(")[0]
                if n in by_qual:
                    ids.append(by_qual[n])
                elif len(by_short.get(n.rsplit(".", 1)[-1], [])) == 1:
                    ids.append(by_short[n.rsplit(".", 1)[-1]][0])
            return list(dict.fromkeys(ids))

        cap_id = f"{repo}:capability:{module or '/'}"
        capabilities.append({"id": cap_id, "name": answer["capability"], "summary": answer["summary"],
                             "module": module, "files": sorted({s["path"] for s in syms})})
        for c in answer["concepts"]:
            ids = cited(c["symbols"])
            if not ids:
                stats["concepts_dropped_no_evidence"] += 1
                continue
            k = norm(c["name"])
            entry = concepts.setdefault(k, {"name": c["name"], "kind": c["kind"], "definition": c["definition"],
                                            "method": "llm", "modules": set()})
            if not entry["definition"]:
                entry["definition"] = c["definition"]
            entry["modules"].add(module)
            for sid in ids:
                realises.setdefault((sid, k), ("llm", 0.6))
            capabilities[-1].setdefault("concepts", set()).add(k)
        for r in answer["rules"]:
            ids = cited(r["symbols"])
            if not ids:
                stats["rules_dropped_no_evidence"] += 1
                continue
            rid = f"{repo}:rule:{hashlib.sha256(r['statement'].lower().encode()).hexdigest()[:16]}"
            rules.append({"id": rid, "statement": r["statement"], "module": module, "symbols": ids})

    # 3. Write the domain layer, then drop what an older run left behind.
    common = {"repo": repo, "run_id": run_id, "stamp": stamp}
    w._run("UNWIND $rows AS row MERGE (n:Domain {id: row.id}) SET n:DomainConcept, n.repo = $repo, "
           "n.name = row.name, n.kind = row.kind, n.definition = row.definition, n.method = row.method, "
           "n.modules = row.modules, n.run_id = $run_id, n.ingested_at = $stamp",
           rows=[{"id": f"{repo}:concept:{k}", "name": c["name"], "kind": c["kind"], "definition": c["definition"],
                  "method": c["method"], "modules": sorted(c["modules"])} for k, c in concepts.items()], **common)
    w._run("UNWIND $rows AS row MATCH (s:Symbol {id: row.sid}) MATCH (c:DomainConcept {id: row.cid}) "
           "MERGE (s)-[r:REALISES]->(c) SET r.method = row.method, r.confidence = row.conf, r.run_id = $run_id",
           rows=[{"sid": sid, "cid": f"{repo}:concept:{k}", "method": m, "conf": conf}
                 for (sid, k), (m, conf) in realises.items()], **common)
    w._run("UNWIND $rows AS row MERGE (n:Domain {id: row.id}) SET n:BusinessRule, n.repo = $repo, "
           "n.statement = row.statement, n.module = row.module, n.method = 'llm', n.run_id = $run_id, "
           "n.ingested_at = $stamp WITH n, row UNWIND row.symbols AS sid MATCH (s:Symbol {id: sid}) "
           "MERGE (n)-[r:IMPLEMENTED_BY]->(s) SET r.method = 'llm', r.confidence = 0.6, r.run_id = $run_id",
           rows=rules, **common)
    w._run("UNWIND $rows AS row MERGE (n:Domain {id: row.id}) SET n:Capability, n.repo = $repo, n.name = row.name, "
           "n.summary = row.summary, n.module = row.module, n.run_id = $run_id, n.ingested_at = $stamp "
           "WITH n, row UNWIND row.files AS path MATCH (f:File {repo: $repo, path: path}) "
           "MERGE (n)-[r:COVERS]->(f) SET r.run_id = $run_id",
           rows=[{**c, "concepts": sorted(c.get("concepts", ()))} for c in capabilities], **common)
    w._run("UNWIND $rows AS row MATCH (n:Capability {id: row.id}) UNWIND row.concepts AS k "
           "MATCH (c:DomainConcept {id: $repo + ':concept:' + k}) MERGE (n)-[r:USES_CONCEPT]->(c) "
           "SET r.run_id = $run_id",
           rows=[{"id": c["id"], "concepts": sorted(c.get("concepts", ()))} for c in capabilities], **common)
    if llm is not None and modules is None and max_modules is None:
        # Only a full run may delete; a partial run would remove other modules' results.
        w._run("MATCH (n:Domain {repo: $repo}) WHERE n.run_id <> $run_id DETACH DELETE n", **common)
        w._run("MATCH (:Symbol {repo: $repo})-[r:REALISES]->() WHERE r.run_id <> $run_id DELETE r", **common)
    stats.update(concepts=len(concepts), rules=len(rules), capabilities=len(capabilities),
                 realises=len(realises), modules_total=len(names))
    return dict(stats)


def main(argv: list[str] | None = None) -> None:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="Extract the domain layer of a repository from its code graph.")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--module", action="append", help="limit to these folders (repeatable)")
    ap.add_argument("--max-modules", type=int, help="only the N largest modules")
    ap.add_argument("--no-llm", action="store_true", help="roles and entities only")
    ap.add_argument("--cache", type=Path, default=Path(os.environ.get("LLM_CACHE_DIR", ".kg-cache/llm")))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        print(json.dumps(extract(w, args.repo, None if args.no_llm else LLM(), args.cache, args.module,
                                 args.max_modules), indent=2))


if __name__ == "__main__":
    main()
