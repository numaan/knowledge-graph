"""Synthetic knowledge graph for scale tests, written into Neo4j through Neo4jWriter.

    cd pipeline
    python -m scale.generate --preset small                 # 3 repos, 10k tickets: a few minutes on a laptop
    python -m scale.generate --preset full                  # 20 repos, 100k tickets, 20k sections, 50k commits
    python -m scale.generate --repos 5 --tickets 20000 --json scale.json

Code per repository: modules -> files -> a service class with methods and helper functions, chunks,
imports, and a call graph (mostly inside a module, some across modules, tests that call services).
Sources: Jira tickets with epics, links, releases and code mentions; documents with sections; commits
that modify files and symbols and reference tickets. With --embed, chunks and sources get vectors from
the fake embedder (kg_pipeline.fake_embed), so serve the MCP server with the same fake embedder:
kg-fake-embedder --dimensions <same>. Everything is deterministic for a given --seed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import time

from kg_pipeline.code.model import CodeGraph
from kg_pipeline.fake_embed import vector
from kg_pipeline.graph.neo4j_writer import Neo4jWriter, _batches
from kg_pipeline.vectors import ensure_indexes

NOUNS = ["order", "invoice", "payment", "customer", "account", "shipment", "product", "cart", "refund", "user",
         "invitation", "report", "subscription", "coupon", "warehouse", "supplier", "notification", "session",
         "audit", "tax", "discount", "review", "catalog", "price", "ledger", "contract", "quote", "delivery"]
VERBS = ["create", "update", "delete", "get", "list", "validate", "approve", "cancel", "export", "import",
         "schedule", "retry", "notify", "sync", "archive", "calculate", "render", "search", "merge", "lock"]
ADJECTIVES = ["empty", "expired", "duplicated", "negative", "locked", "missing", "late", "partial", "foreign"]
PRESETS = {
    "tiny": dict(repos=1, modules=4, files=40, tickets=500, documents=20, sections=10, commits=200),
    "small": dict(repos=3, modules=8, files=200, tickets=10_000, documents=200, sections=10, commits=2_000),
    "full": dict(repos=20, modules=12, files=600, tickets=100_000, documents=2_000, sections=10, commits=50_000),
}


def cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def build_repo(name: str, modules: int, files: int, rnd: random.Random) -> tuple[CodeGraph, list[dict]]:
    """One repository; returns the graph and its symbols (id, qualname, path) for the source layer."""
    g = CodeGraph(name)
    g.add_node(name, ("Code", "Repository"), name=name, commit="0" * 40)
    mods = [f"src/{NOUNS[(i * 7) % len(NOUNS)]}{i}" for i in range(modules)]
    symbols, callables = [], {m: [] for m in mods}
    for f in range(files):
        mod = mods[f % modules]
        noun = NOUNS[f % len(NOUNS)]
        path = f"{mod}/{noun}_{f}.py"
        fid = f"{name}:{path}"
        g.add_node(fid, ("Code", "File"), path=path, language="python", sha256=f"{f:064x}", lines=200)
        g.add_edge("CONTAINS", name, fid, "parser")
        cls = f"{cap(noun)}Service{f}"
        cid = f"{fid}#{cls}"
        g.add_node(cid, ("Code", "Symbol", "Class"), name=cls, qualname=cls, kind="Class", path=path,
                   language="python", start_line=1, end_line=180, signature=f"class {cls}")
        g.add_edge("DEFINES", fid, cid, "parser")
        symbols.append({"id": cid, "qualname": cls, "path": path})
        line = 3
        for verb in rnd.sample(VERBS, 6):
            other = rnd.choice(NOUNS)
            qual, sname = f"{cls}.{verb}_{noun}", f"{verb}_{noun}"
            sid = f"{fid}#{qual}"
            text = (f"    def {sname}(self, {noun}_id: str, {other}: dict) -> dict:\n"
                    f'        """{cap(verb)} the {noun} for a {other}; fails when the {other} is {rnd.choice(ADJECTIVES)}."""\n'
                    f"        record = self.repo.load_{noun}({noun}_id)\n        return self.{verb}_{other}(record)\n")
            g.add_node(sid, ("Code", "Symbol", "Method"), name=sname, qualname=qual, kind="Method", path=path,
                       language="python", start_line=line, end_line=line + 25, signature=f"def {sname}(self, {noun}_id, {other})")
            g.add_edge("DEFINES", fid, sid, "parser")
            g.add_edge("CONTAINS", cid, sid, "parser")
            chunk = f"{fid}#L{line}-{line + 25}"
            g.add_node(chunk, ("Code", "Chunk"), path=path, language="python", start_line=line, end_line=line + 25, text=text)
            g.add_edge("PART_OF", chunk, sid, "parser")
            callables[mod].append(sid)
            symbols.append({"id": sid, "qualname": qual, "path": path})
            line += 28
    # Calls: 70 % inside the module, 30 % to another module, 1-4 per method.
    for mod, ids in callables.items():
        for sid in ids:
            for _ in range(rnd.randint(1, 4)):
                pool = ids if rnd.random() < 0.7 else callables[rnd.choice(mods)]
                dst = rnd.choice(pool)
                if dst != sid:
                    method, conf = ("scip", 1.0) if rnd.random() < 0.6 else ("treesitter", 0.8)
                    g.add_edge("CALLS", sid, dst, method, conf)
                    g.add_edge("IMPORTS", sid.split("#")[0], dst.split("#")[0], "parser")
    # Tests: one test file per module; each test calls 3 service methods.
    for mod, ids in callables.items():
        path = f"tests/test_{mod.split('/')[-1]}.py"
        fid = f"{name}:{path}"
        g.add_node(fid, ("Code", "File"), path=path, language="python", sha256="t" * 64, lines=100)
        g.add_edge("CONTAINS", name, fid, "parser")
        for t in range(max(1, len(ids) // 10)):
            tid = f"{fid}#test_case_{t}"
            g.add_node(tid, ("Code", "Symbol", "Function"), name=f"test_case_{t}", qualname=f"test_case_{t}",
                       kind="Function", path=path, language="python", start_line=t * 6 + 1, end_line=t * 6 + 5)
            g.add_edge("DEFINES", fid, tid, "parser")
            for dst in rnd.sample(ids, min(3, len(ids))):
                g.add_edge("CALLS", tid, dst, "treesitter", 0.8)
    for e in list(g.edges):  # IMPORTS from a file to itself
        if e[0] == "IMPORTS" and e[1] == e[2]:
            del g.edges[e]
    return g, symbols


def tickets(n: int, projects: list[str], symbols: list[dict], rnd: random.Random):
    rows, edges = [], {"CHILD_OF": [], "LINKS": [], "IN_RELEASE": [], "MENTIONS": []}
    releases = [{"id": f"jira:release:{p}:{i}", "project": p, "title": f"{p} {2026 + i // 12}.{i % 12 + 1}",
                 "name": f"{p} {2026 + i // 12}.{i % 12 + 1}", "released": i < 15, "acl": [f"jira:{p}"], "source": "jira"}
                for p in projects for i in range(20)]
    for i in range(1, n + 1):
        p = projects[i % len(projects)]
        key, verb, noun, other = f"{p}-{i}", rnd.choice(VERBS), rnd.choice(NOUNS), rnd.choice(NOUNS)
        sym = rnd.choice(symbols)
        epic = i % 50 == 1
        rows.append({"id": f"jira:{key}", "key": key, "project": p, "type": "Epic" if epic else rnd.choice(["Story", "Bug", "Task"]),
                     "title": f"{cap(verb)} {noun} when the {other} is {rnd.choice(ADJECTIVES)}",
                     "text": f"As a {other} manager I want to {verb} the {noun} so that the {other} stays correct.\n\n"
                             f"## Acceptance criteria\n\n- the {noun} is {verb}d\n- see `{sym['qualname']}` in {sym['path']}",
                     "status": rnd.choice(["To Do", "In Progress", "Done"]), "priority": rnd.choice(["Low", "Medium", "High"]),
                     "labels": [noun], "components": [other], "created": f"2026-0{1 + i % 9}-1{i % 9}T10:00:00.000+0000",
                     "updated": "2026-10-01T10:00:00.000+0000", "url": f"https://jira.example/browse/{key}",
                     "acl": [f"jira:{p}"], "source": "jira"})
        if not epic:
            edges["CHILD_OF"].append({"src": f"jira:{key}", "dst": f"jira:{p}-{(i - 1) // 50 * 50 + 1}"})
        if i > 10 and rnd.random() < 0.2:
            edges["LINKS"].append({"src": f"jira:{key}", "dst": f"jira:{projects[(i - 7) % len(projects)]}-{i - 7}", "type": "relates to"})
        edges["IN_RELEASE"].append({"src": f"jira:{key}", "dst": f"jira:release:{p}:{i % 20}"})
        edges["MENTIONS"].append({"src": f"jira:{key}", "dst": sym["id"], "term": sym["qualname"], "method": "text", "confidence": 0.6})
    return releases, rows, edges


def documents(n: int, per_doc: int, symbols: list[dict], rnd: random.Random, collection: str = "scale"):
    docs, sections, part_of, mentions = [], [], [], []
    for d in range(n):
        noun = NOUNS[d % len(NOUNS)]
        did = f"docs:{collection}:{noun}-guide-{d}.md"
        docs.append({"id": did, "name": f"{noun}-guide-{d}.md", "title": f"{cap(noun)} guide {d}", "collection": collection,
                     "path": f"{noun}-guide-{d}.md", "version": "1", "format": "md", "source": "files", "acl": [f"docs:{collection}"]})
        for s in range(per_doc):
            sym, verb = rnd.choice(symbols), rnd.choice(VERBS)
            sid = f"{did}#s{s}"
            sections.append({"id": sid, "title": f"How to {verb} a {noun}", "order": s, "acl": [f"docs:{collection}"], "source": "files",
                             "text": f"To {verb} a {noun}, call `{sym['qualname']}`. The {noun} must not be "
                                     f"{rnd.choice(ADJECTIVES)}. Runbook step {s} for the {rnd.choice(NOUNS)} team."})
            part_of.append({"src": sid, "dst": did})
            mentions.append({"src": sid, "dst": sym["id"], "term": sym["qualname"], "method": "text", "confidence": 0.6})
    return docs, sections, part_of, mentions


def commits(n: int, repo: str, symbols: list[dict], ticket_keys: list[str], rnd: random.Random):
    rows, modifies, refs = [], [], []
    for c in range(n):
        sha = hashlib.sha1(f"{repo}:{c}".encode()).hexdigest()
        key = rnd.choice(ticket_keys) if ticket_keys else None
        changed = rnd.sample(symbols, min(len(symbols), rnd.randint(1, 4)))
        cid = f"git:{repo}:{sha}"
        rows.append({"id": cid, "sha": sha, "repo": repo, "author": f"dev{c % 40}@example.com",
                     "date": f"2026-{1 + c % 9:02d}-{1 + c % 27:02d}T12:00:00+00:00",
                     "title": f"{key + ': ' if key else ''}{rnd.choice(VERBS)} {rnd.choice(NOUNS)}", "text": "",
                     "ticket_keys": [key] if key else [], "files_changed": len(changed), "acl": [f"repo:{repo}"], "source": "git"})
        for s in changed:
            modifies.append({"src": cid, "dst": f"{repo}:{s['path']}", "change": "M", "added": 5, "deleted": 2,
                             "method": "git", "confidence": 1.0})
            if "#" in s["id"]:
                modifies.append({"src": cid, "dst": s["id"], "method": "git", "confidence": 0.9})
        if key:
            refs.append({"src": cid, "dst": f"jira:{key}", "method": "key", "confidence": 1.0})
    return rows, modifies, refs


def embed(w: Neo4jWriter, dimensions: int, label: str, where: str = "") -> int:
    """Fake vectors for every node of a label (title + text), in batches."""
    rows = w._run(f"MATCH (n:{label}) {where} RETURN n.id AS id, coalesce(n.title, '') + ' ' + coalesce(n.text, '') AS t").records
    for batch in _batches([{"id": r["id"], "v": vector(r["t"], dimensions)} for r in rows], 1000):
        w._run(f"UNWIND $rows AS row MATCH (n:{label} {{id: row.id}}) SET n.embedding = row.v", rows=batch)
    return len(rows)


def generate(w: Neo4jWriter, repos: int, modules: int, files: int, tickets_n: int, documents_n: int,
             sections: int, commits_n: int, dimensions: int = 0, seed: int = 7, prefix: str = "scale",
             project: str = "SC") -> dict:
    rnd, stats, t0 = random.Random(seed), {"phases": {}}, time.time()

    def phase(name, start):
        stats["phases"][name] = round(time.time() - start, 1)

    all_symbols, repo_symbols = [], {}
    for r in range(repos):
        t = time.time()
        name = f"{prefix}-r{r:03d}"
        g, syms = build_repo(name, modules, files, rnd)
        res = w.write_delta(g)
        repo_symbols[name] = syms
        all_symbols += syms
        stats.setdefault("code", []).append({"repo": name, "nodes": res["nodes"], "edges": res["edges"]})
        phase(f"code {name}", t)
    t = time.time()
    projects = [f"{project}{chr(65 + i)}" for i in range(max(1, min(10, tickets_n // 10_000)))]
    releases, ticket_rows, ticket_edges = tickets(tickets_n, projects, all_symbols, rnd)
    w.upsert(("Release",), releases)
    w.upsert(("Ticket",), ticket_rows)
    for etype, rows in ticket_edges.items():
        w.link(etype, rows, dst="Code" if etype == "MENTIONS" else "Source")
    phase("tickets", t)
    t = time.time()
    docs, section_rows, part_of, mentions = documents(documents_n, sections, all_symbols, rnd, prefix)
    w.upsert(("Document",), docs)
    w.upsert(("Section",), section_rows)
    w.link("PART_OF", part_of)
    w.link("MENTIONS", mentions, dst="Code")
    phase("documents", t)
    t = time.time()
    keys = [r["key"] for r in ticket_rows]
    n_commits = 0
    for name, syms in repo_symbols.items():
        rows, modifies, refs = commits(commits_n // max(1, repos), name, syms, keys, rnd)
        w.upsert(("Commit",), rows)
        w.link("MODIFIES", modifies, dst="Code")
        w.link("REFERENCES", refs)
        n_commits += len(rows)
    phase("commits", t)
    if dimensions:
        t = time.time()
        ensure_indexes(w, dimensions)
        stats["embedded"] = {"chunks": embed(w, dimensions, "Chunk", f"WHERE n.repo STARTS WITH '{prefix}-'"),
                             "sources": embed(w, dimensions, "Source", "WHERE (n:Ticket OR n:Section) AND n.embedding IS NULL")}
        phase("embeddings", t)
    counts = w._run("MATCH (n) RETURN count(n) AS n").records[0]["n"]
    rels = w._run("MATCH ()-[r]->() RETURN count(r) AS n").records[0]["n"]
    stats |= {"repos": repos, "symbols": len(all_symbols), "tickets": len(ticket_rows), "sections": len(section_rows),
              "commits": n_commits, "graph_nodes": counts, "graph_relationships": rels, "seconds": round(time.time() - t0, 1)}
    return stats


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", choices=sorted(PRESETS), default="small")
    for k in ("repos", "modules", "files", "tickets", "documents", "sections", "commits"):
        ap.add_argument(f"--{k}", type=int, help=f"override the preset ({k})")
    ap.add_argument("--embed", type=int, default=256, metavar="DIMENSIONS", help="fake vectors of this size (0 = none)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", help="write the statistics to this file")
    args = ap.parse_args(argv)
    p = PRESETS[args.preset] | {k: v for k, v in vars(args).items() if k in PRESETS["small"] and v is not None}
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        stats = generate(w, p["repos"], p["modules"], p["files"], p["tickets"], p["documents"], p["sections"],
                         p["commits"], args.embed, args.seed)
    print(json.dumps(stats, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, indent=2)


if __name__ == "__main__":
    main()
