"""Hierarchical summaries: file -> module (folder) -> repository, plus one per domain capability.

    kg-summaries --repo precept                    # LLM within the budget, extractive text for the rest
    kg-summaries --repo precept --no-llm           # extractive text only (no LLM call)
    kg-summaries --repo precept --budget 50

Built bottom-up: a file summary from its symbols, signatures and docstrings; a module summary from
the summaries of its files; the repository summary from the module summaries; a domain summary from
the concepts, rules and summary of one capability. Each summary stores content_hash, a hash of its
inputs, and is generated again only when that hash changes, so an unchanged file costs nothing and a
changed file regenerates only its own chain (file, folder, repository).

The LLM budget bounds the calls of one run (cache hits are free). A summary that does not get an
LLM call gets an extractive text (model "extractive") built from the same inputs; a later run with
budget left replaces it.

Graph:
    (:Summary {id, level, text, content_hash, model, generated_at, embedding, repo, path, target_id})
        -[:SUMMARIZES {method, confidence}]-> (:File) | (:Folder) | (:Repository) | (:Capability)
    (:Folder {id, repo, path, name}) -[:CONTAINS]-> (:File)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import posixpath
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .domain.extract import _cache_get, _cache_put
from .graph.neo4j_writer import Neo4jWriter
from .models import LLM, Embedder

log = logging.getLogger(__name__)

PROMPT_VERSION = "summary-v1"
EXTRACTIVE = "extractive"
DEFAULT_BUDGET = int(os.environ.get("SUMMARY_BUDGET", "300"))
CONTEXT_CHARS = 12_000
MAX_SYMBOLS = 120
LEVELS = ("file", "module", "repo", "domain")
TARGET_LABEL = {"file": "File", "module": "Folder", "repo": "Repository", "domain": "Capability"}

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}

SYSTEM = {
    "file": "You summarise one source file for developers and coding agents. In 2 or 3 sentences, say what the "
            "file is for and what its main classes or functions do. Use the names from the input. Do not invent "
            "behaviour that the input does not show. Return only JSON that matches the schema.",
    "module": "You summarise one folder (module) of a code base from the summaries of its files. In 3 to 5 "
              "sentences, say what the module is responsible for, its main parts and how they work together. "
              "Do not list every file. Return only JSON that matches the schema.",
    "repo": "You summarise a whole repository from the summaries of its modules. In one paragraph of 4 to 7 "
            "sentences, say what the system does, its main modules and how they depend on each other. "
            "Return only JSON that matches the schema.",
    "domain": "You summarise one business capability for product owners and developers, from its domain concepts "
              "and business rules. In 3 to 5 sentences of business language (not code names), say what the "
              "capability does, its main concepts and the rules it enforces. Return only JSON that matches the schema.",
}

_TRIPLE = re.compile(r'^[ \t]*[rRuUbB]{0,2}("""|\'\'\')(.*?)(\1|$)', re.S)
_BLOCK = re.compile(r"/\*\*?(.*?)\*/", re.S)
_LINE_COMMENT = re.compile(r"^\s*(///?|#(?![!\w]))\s?(.*)$")
_DECLARATIONS = ("@", "package ", "import ", "from ", "def ", "async def ", "class ", "func ", "export ", "public ",
                 "private ", "protected ", "internal ", "fun ", "interface ", "const ", "function ")


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _first_sentence(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    m = re.search(r"(?<=[.!?])\s", text)
    text = text[:m.start()] if m else text
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def docstring(text: str | None) -> str:
    """The docstring or leading comment of a code excerpt (first paragraph), or ''."""
    if not text:
        return ""
    lines = text.splitlines()
    found = ""
    for start in range(min(4, len(lines))):          # Python: a triple-quoted string on one of the first lines
        m = _TRIPLE.match("\n".join(lines[start:start + 30]))
        if m:
            found = m.group(2)
            break
    head = "\n".join(lines[:8])
    m = None if found else _BLOCK.search(head)
    if m and head[:m.start()].count("\n") <= 1:      # /** ... */ on the first or second line
        found = re.sub(r"(?m)^\s*\*\s?", "", m.group(1))
    if not found:
        out = []
        for line in lines[:12]:                      # leading // or # comment lines
            c = _LINE_COMMENT.match(line)
            if c:
                out.append(c.group(2))
            elif out or (line.strip() and not line.lstrip().startswith(_DECLARATIONS)):
                break
        found = "\n".join(out)
    para = _first_paragraph(found)
    return "" if re.search(r"(?i)\b(copyright|licen[cs]e[ds]?|spdx)\b", para) else para


def _first_paragraph(text: str) -> str:
    para = re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0]
    para = " ".join(para.split())
    return para if len(para) <= 400 else para[:397].rstrip() + "..."


def _folder(path: str) -> str:
    return posixpath.dirname(path)


def _ids(repo: str) -> dict:
    return {
        "file": lambda file_id: f"summary:file:{file_id}",
        "module": lambda folder: f"summary:module:{repo}:{folder or '/'}",
        "repo": lambda _=None: f"summary:repo:{repo}",
        "domain": lambda cap_id: f"summary:domain:{cap_id}",
    }


def folder_id(repo: str, folder: str) -> str:
    return f"{repo}:folder:{folder or '/'}"


# ---- contexts (LLM input) and extractive texts ---------------------------------------------------------

def _symbol_line(s: dict) -> str:
    role = f" [{s['role']}{' ' + (s.get('http_method') or '') + ' ' + s['route'] if s.get('route') else ''}]" \
        if s.get("role") else ""
    doc = f" -- {s['doc']}" if s.get("doc") else ""
    return f"- {s['kind']} {s['qualname']}{role}: {s.get('signature') or ''}{doc}"


def file_context(repo: str, f: dict) -> str:
    lines = [f"Repository: {repo}", f"File: {f['path']} ({f.get('language') or '?'}, {f.get('lines') or 0} lines)"]
    if f.get("doc"):
        lines.append(f"File comment: {f['doc']}")
    lines.append("Symbols:")
    lines += [_symbol_line(s) for s in f["symbols"][:MAX_SYMBOLS]]
    if len(f["symbols"]) > MAX_SYMBOLS:
        lines.append(f"- ... and {len(f['symbols']) - MAX_SYMBOLS} more")
    return "\n".join(lines)[:CONTEXT_CHARS]


def file_extractive(f: dict) -> str:
    out = [f"{f['path']} ({f.get('language') or 'file'}, {f.get('lines') or 0} lines)."]
    if f.get("doc"):
        out.append(_first_sentence(f["doc"]).rstrip(".") + ".")
    top = [s for s in f["symbols"] if "." not in s["qualname"]] or f["symbols"]
    if top:
        names = [f"{s['kind'].lower()} {s['qualname']}" + (f" ({s['role']})" if s.get("role") else "") for s in top[:8]]
        more = f" and {len(top) - 8} more" if len(top) > 8 else ""
        out.append(f"Defines {', '.join(names)}{more}.")
    routes = [f"{s.get('http_method') or ''} {s['route']}".strip() for s in f["symbols"] if s.get("route")]
    if routes:
        out.append(f"Endpoints: {', '.join(routes[:6])}.")
    return " ".join(out)


def _children(items: list[tuple[str, str]], budget: int) -> list[str]:
    out = []
    for name, text in items:
        line = f"- {name}: {text}"
        if len(line) > budget:
            break
        out.append(line)
        budget -= len(line) + 1
    if len(out) < len(items):
        out.append(f"- ... and {len(items) - len(out)} more")
    return out


def module_context(repo: str, folder: str, files: list[tuple[str, str]], capability: dict | None) -> str:
    lines = [f"Repository: {repo}", f"Module (folder): {folder or '/'}", f"Files: {len(files)}"]
    if capability:
        lines.append(f"Business capability: {capability['name']} -- {capability.get('summary') or ''}")
    lines.append("File summaries:")
    return "\n".join(lines + _children(files, CONTEXT_CHARS - sum(map(len, lines))))


def module_extractive(folder: str, files: list[tuple[str, str]], capability: dict | None) -> str:
    out = [f"Folder {folder or '/'} with {len(files)} file{'s' if len(files) != 1 else ''}."]
    if capability:
        out.append(f"Capability: {capability['name']}.")
    out += [f"{posixpath.basename(p)}: {_first_sentence(t, 160)}" for p, t in files[:5]]
    if len(files) > 5:
        out.append(f"And {len(files) - 5} more files.")
    return " ".join(out)


def repo_context(repo: str, modules: list[tuple[str, str]]) -> str:
    lines = [f"Repository: {repo}", f"Modules: {len(modules)}", "Module summaries (largest first):"]
    return "\n".join(lines + _children(modules, CONTEXT_CHARS - sum(map(len, lines))))


def repo_extractive(repo: str, modules: list[tuple[str, str]], files: int) -> str:
    out = [f"Repository {repo} with {files} files in {len(modules)} folders."]
    if modules:
        out.append("Main folders: " + "; ".join(f"{m or '/'}: {_first_sentence(t, 120)}" for m, t in modules[:6]))
    return " ".join(out)


def domain_context(repo: str, cap: dict) -> str:
    lines = [f"Repository: {repo}", f"Capability: {cap['name']} (module {cap.get('module') or '/'})",
             f"Summary: {cap.get('summary') or ''}", "Concepts:"]
    lines += [f"- {c['name']} ({c.get('kind') or '?'}): {c.get('definition') or ''}" for c in cap["concepts"]]
    lines.append("Business rules:")
    lines += [f"- {r}" for r in cap["rules"]]
    return "\n".join(lines)[:CONTEXT_CHARS]


def domain_extractive(cap: dict) -> str:
    out = [f"{cap['name']}." if not cap.get("summary") else f"{cap['name']}: {cap['summary']}"]
    if cap["concepts"]:
        out.append("Concepts: " + ", ".join(c["name"] for c in cap["concepts"][:10]) + ".")
    if cap["rules"]:
        out.append("Rules: " + " ".join(r.rstrip(".") + "." for r in cap["rules"][:5]))
    return " ".join(out)


# ---- generation -------------------------------------------------------------------------------------------

class Generator:
    """Picks the text of each summary: kept, cached, LLM (within budget) or extractive."""

    def __init__(self, llm: LLM | None, budget: int, cache: Path | None, existing: dict[str, dict]):
        self.llm, self.budget, self.cache, self.existing = llm, budget, cache, existing
        self.stats = defaultdict(int)

    def __call__(self, sid: str, level: str, context: str, extractive: str) -> dict:
        h = _hash(PROMPT_VERSION, level, context)
        old = self.existing.get(sid)
        if old and old.get("content_hash") == h and (old.get("model") != EXTRACTIVE or self.llm is None):
            self.stats["unchanged"] += 1
            return {"text": old["text"], "model": old["model"], "content_hash": h, "changed": False}
        if self.llm is not None:
            key = hashlib.sha256(f"{PROMPT_VERSION}|{self.llm.name}|{level}|{context}".encode()).hexdigest()[:32]
            answer = _cache_get(self.cache, key)
            if answer is not None:
                self.stats["llm_cache_hits"] += 1
            elif self.budget > 0:
                self.budget -= 1
                try:
                    answer = self.llm.json(SYSTEM[level], context, SCHEMA)
                    if not str(answer.get("summary", "")).strip():
                        raise ValueError("empty summary")
                    _cache_put(self.cache, key, answer)
                    self.stats["llm_calls"] += 1
                except Exception:
                    log.exception("summary LLM call failed for %s", sid)
                    self.stats["llm_failed"] += 1
                    answer = None
            else:
                self.stats["over_budget"] += 1
            if answer is not None:
                text = " ".join(str(answer["summary"]).split())
                changed = not old or old.get("text") != text or old.get("content_hash") != h
                return {"text": text, "model": self.llm.name, "content_hash": h, "changed": changed}
            if old and old.get("content_hash") == h:   # extractive and still current: keep it
                self.stats["unchanged"] += 1
                return {"text": old["text"], "model": old["model"], "content_hash": h, "changed": False}
        self.stats["extractive"] += 1
        return {"text": extractive, "model": EXTRACTIVE, "content_hash": h, "changed": True}


def build(repo: str, data: dict, existing: dict[str, dict], llm: LLM | None, budget: int = DEFAULT_BUDGET,
          cache: Path | None = None) -> tuple[list[dict], dict]:
    """All summaries of one repository, bottom-up. Returns (rows, stats); rows have changed=True when
    they must be written. data: {"files": [...], "capabilities": [...]} as loaded by load()."""
    gen = Generator(llm, budget, cache, existing)
    sid = _ids(repo)
    rows: list[dict] = []
    files = sorted(data["files"], key=lambda f: (-len(f["symbols"]), f["path"]))   # most symbols first in the budget
    caps = {c.get("module") or "": c for c in data.get("capabilities", [])}

    file_text: dict[str, str] = {}
    for f in files:
        r = gen(sid["file"](f["id"]), "file", file_context(repo, f), file_extractive(f))
        rows.append({**r, "id": sid["file"](f["id"]), "level": "file", "target_id": f["id"], "path": f["path"]})
        file_text[f["path"]] = r["text"]

    by_folder: dict[str, list[str]] = defaultdict(list)
    for f in data["files"]:
        by_folder[_folder(f["path"])].append(f["path"])
    module_text: dict[str, str] = {}
    for folder in sorted(by_folder, key=lambda m: (-len(by_folder[m]), m)):
        kids = [(p, file_text[p]) for p in sorted(by_folder[folder])]
        cap = caps.get(folder)
        r = gen(sid["module"](folder), "module", module_context(repo, folder, kids, cap),
                module_extractive(folder, kids, cap))
        rows.append({**r, "id": sid["module"](folder), "level": "module", "target_id": folder_id(repo, folder),
                     "path": folder})
        module_text[folder] = r["text"]

    if data["files"]:
        mods = [(m, module_text[m]) for m in sorted(by_folder, key=lambda m: (-len(by_folder[m]), m))]
        r = gen(sid["repo"](), "repo", repo_context(repo, mods), repo_extractive(repo, mods, len(data["files"])))
        rows.append({**r, "id": sid["repo"](), "level": "repo", "target_id": repo, "path": None})

    for cap in sorted(data.get("capabilities", []), key=lambda c: c["id"]):
        r = gen(sid["domain"](cap["id"]), "domain", domain_context(repo, cap), domain_extractive(cap))
        rows.append({**r, "id": sid["domain"](cap["id"]), "level": "domain", "target_id": cap["id"],
                     "path": cap.get("module")})
    stats = dict(gen.stats)
    stats.update(total=len(rows), written=sum(r["changed"] for r in rows), budget_left=gen.budget,
                 **{f"level_{lv}": sum(r["level"] == lv for r in rows) for lv in LEVELS})
    return rows, stats


def folders(repo: str, files: list[dict]) -> list[dict]:
    """:Folder rows (one per folder that holds files) with the ids of their files."""
    by_folder: dict[str, list[str]] = defaultdict(list)
    for f in files:
        by_folder[_folder(f["path"])].append(f["id"])
    return [{"id": folder_id(repo, d), "path": d, "name": posixpath.basename(d) or "/", "files": sorted(ids),
             "content_hash": _hash(sorted(ids))} for d, ids in sorted(by_folder.items())]


# ---- Neo4j ------------------------------------------------------------------------------------------------

SCHEMA_STATEMENTS = [
    "CREATE CONSTRAINT summary_id IF NOT EXISTS FOR (n:Summary) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX summary_repo IF NOT EXISTS FOR (n:Summary) ON (n.repo, n.level)",
    "CREATE INDEX summary_target IF NOT EXISTS FOR (n:Summary) ON (n.target_id)",
    "CREATE CONSTRAINT folder_id IF NOT EXISTS FOR (n:Folder) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX folder_repo IF NOT EXISTS FOR (n:Folder) ON (n.repo, n.path)",
    "CREATE FULLTEXT INDEX summary_text IF NOT EXISTS FOR (n:Summary) ON EACH [n.text]",
]


def ensure_schema(w: Neo4jWriter, dimensions: int | None = None) -> None:
    for s in SCHEMA_STATEMENTS:
        w._run(s)
    if dimensions:
        w._run(f"CREATE VECTOR INDEX summary_embedding IF NOT EXISTS FOR (n:Summary) ON n.embedding "
               f"OPTIONS {{indexConfig: {{`vector.dimensions`: {int(dimensions)}, "
               f"`vector.similarity_function`: 'cosine'}}}}")


def load(w: Neo4jWriter, repo: str) -> tuple[dict, dict[str, dict]]:
    files = [r.data() for r in w._run("""
        MATCH (f:File {repo: $repo})
        OPTIONAL MATCH (f)-[:DEFINES]->(s:Symbol)
        OPTIONAL MATCH (c:Chunk)-[:PART_OF]->(s)
        WITH f, s, c ORDER BY c.start_line
        WITH f, s, head(collect(left(c.text, 1500))) AS head ORDER BY s.start_line
        WITH f, collect(CASE WHEN s IS NULL THEN NULL ELSE {qualname: s.qualname, kind: s.kind, signature: s.signature,
             role: s.role, route: s.route, http_method: s.http_method, head: head} END) AS symbols
        OPTIONAL MATCH (fc:Chunk)-[:PART_OF]->(f)
        WITH f, symbols, fc ORDER BY fc.start_line
        RETURN f.id AS id, f.path AS path, f.language AS language, f.lines AS lines, symbols,
               left(head(collect(fc.text)), 1500) AS head""", repo=repo).records]
    for f in files:
        f["doc"] = docstring(f.pop("head", None))
        for s in f["symbols"]:
            s["doc"] = _first_sentence(docstring(s.pop("head", None))) or None
    caps = [r.data() for r in w._run("""
        MATCH (c:Capability {repo: $repo})
        OPTIONAL MATCH (c)-[:USES_CONCEPT]->(k:DomainConcept)
        WITH c, k ORDER BY k.name
        WITH c, collect(CASE WHEN k IS NULL THEN NULL ELSE {name: k.name, kind: k.kind, definition: k.definition} END) AS concepts
        OPTIONAL MATCH (r:BusinessRule {repo: $repo}) WHERE r.module = c.module
        WITH c, concepts, r ORDER BY r.statement
        RETURN c.id AS id, c.name AS name, c.summary AS summary, c.module AS module, concepts,
               collect(r.statement) AS rules""", repo=repo).records]
    existing = {r["id"]: r.data() for r in w._run(
        "MATCH (s:Summary {repo: $repo}) RETURN s.id AS id, s.content_hash AS content_hash, s.model AS model, "
        "s.text AS text, s.embedding_hash AS embedding_hash", repo=repo).records}
    return {"files": files, "capabilities": caps}, existing


def write(w: Neo4jWriter, repo: str, rows: list[dict], folder_rows: list[dict]) -> dict:
    stamp = datetime.now(timezone.utc).isoformat()
    old_folders = {r["id"]: r["h"] for r in w._run(
        "MATCH (d:Folder {repo: $repo}) RETURN d.id AS id, d.content_hash AS h", repo=repo).records}
    changed_folders = [f for f in folder_rows if old_folders.get(f["id"]) != f["content_hash"]]
    if changed_folders:
        w._run("UNWIND $rows AS row MERGE (d:Folder {id: row.id}) SET d.repo = $repo, d.path = row.path, "
               "d.name = row.name, d.content_hash = row.content_hash, d.ingested_at = $stamp "
               "WITH d, row OPTIONAL MATCH (d)-[old:CONTAINS]->(:File) DELETE old "
               "WITH DISTINCT d, row UNWIND row.files AS fid MATCH (f:File {id: fid}) "
               "MERGE (d)-[r:CONTAINS]->(f) SET r.method = 'parser', r.confidence = 1.0",
               rows=changed_folders, repo=repo, stamp=stamp)
    keep_folders = [f["id"] for f in folder_rows]
    w._run("MATCH (d:Folder {repo: $repo}) WHERE NOT d.id IN $ids DETACH DELETE d", repo=repo, ids=keep_folders)

    todo = [r for r in rows if r["changed"]]
    by_level = defaultdict(list)
    for r in todo:
        by_level[r["level"]].append({k: r[k] for k in ("id", "level", "text", "model", "content_hash", "target_id", "path")})
    for level, batch in by_level.items():
        label = TARGET_LABEL[level]
        w._run(f"UNWIND $rows AS row MERGE (s:Summary {{id: row.id}}) "
               f"SET s.level = row.level, s.text = row.text, s.model = row.model, s.content_hash = row.content_hash, "
               f"s.target_id = row.target_id, s.path = row.path, s.repo = $repo, s.generated_at = $stamp "
               f"WITH s, row OPTIONAL MATCH (s)-[old:SUMMARIZES]->() DELETE old "
               f"WITH DISTINCT s, row MATCH (t:{label} {{id: row.target_id}}) "
               f"MERGE (s)-[r:SUMMARIZES]->(t) SET r.method = CASE row.model WHEN '{EXTRACTIVE}' THEN 'extractive' "
               f"ELSE 'llm' END, r.confidence = 1.0, r.content_hash = row.content_hash",
               rows=batch, repo=repo, stamp=stamp)
    removed = w._run("MATCH (s:Summary {repo: $repo}) WHERE NOT s.id IN $ids DETACH DELETE s RETURN count(*) AS n",
                     repo=repo, ids=[r["id"] for r in rows]).records
    return {"folders_written": len(changed_folders), "summaries_written": len(todo),
            "summaries_removed": removed[0]["n"] if removed else 0}


def summarize(w: Neo4jWriter, repo: str, llm: LLM | None, emb: Embedder | None = None,
              budget: int = DEFAULT_BUDGET, cache: Path | None = None) -> dict:
    """Load, build and write the summaries of one repository; embed the changed ones when emb is given."""
    ensure_schema(w, emb.dimensions if emb else None)
    data, existing = load(w, repo)
    rows, stats = build(repo, data, existing, llm, budget, cache)
    stats.update(write(w, repo, rows, folders(repo, data["files"])))
    if emb is not None:
        from .vectors import _embed_label
        items = [{"id": r["id"], "hash": (existing.get(r["id"]) or {}).get("embedding_hash"),
                  "text": f"{r['level']} {r['path'] or r['target_id']}: {r['text']}"} for r in rows]
        stats["embeddings"] = _embed_label(w, emb, items, "Summary")
    return stats


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--no-llm", action="store_true", help="extractive summaries only")
    ap.add_argument("--no-embed", action="store_true", help="do not embed the summaries")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="maximum LLM calls in this run")
    ap.add_argument("--cache", type=Path, default=Path(os.environ.get("LLM_CACHE_DIR", ".kg-cache/llm")))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        print(json.dumps(summarize(w, args.repo, None if args.no_llm else LLM(), None if args.no_embed else Embedder(),
                                   args.budget, args.cache), indent=2))


if __name__ == "__main__":
    main()
