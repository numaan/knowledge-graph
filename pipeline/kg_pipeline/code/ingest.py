"""Full snapshot ingestion of one repository into the code layer.

    kg-ingest-code --repo /path/to/checkout --name my-repo            # write to Neo4j (NEO4J_* env)
    kg-ingest-code --repo . --name my-repo --json out/ --no-neo4j     # write graph JSON only
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import odm, scip
from .files import PARSED, read_sources
from .model import CodeGraph
from .resolve import Resolver
from .treesitter import chunks, parse

log = logging.getLogger("kg_pipeline.code")


def _commit(repo: Path) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def build_graph(repo: Path, name: str, scip_mode: str = "auto",
                scip_files: list[tuple[Path, str]] | None = None, scip_cache: Path | None = None) -> CodeGraph:
    """scip_mode: auto (run installed indexers), off, or files (use scip_files)."""
    repo = repo.resolve()
    g = CodeGraph(name)
    commit = _commit(repo)
    g.add_node(name, ("Code", "Repository"), name=name, commit=commit)

    files = read_sources(repo, g.counters)
    syntax = {}
    for f in files:
        file_id = f"{name}:{f.path}"
        lines = f.text.splitlines()
        g.add_node(file_id, ("Code", "File"), path=f.path, language=f.language, sha256=f.sha256,
                   lines=len(lines), commit=commit)
        g.add_edge("CONTAINS", name, file_id, "parser")
        g.counters[f"files_{f.language}"] += 1
        if f.language not in PARSED:
            continue
        try:
            s = parse(f.text, f.language)
        except Exception:  # a grammar crash must not stop the snapshot
            log.exception("parse failed: %s", f.path)
            g.counters["files_parse_failed"] += 1
            continue
        syntax[f.path] = s
        g.counters["files_parsed"] += 1
        if s.has_errors:
            g.counters["files_with_syntax_errors"] += 1
        for sym in s.symbols:
            sid = f"{file_id}#{sym.qualname}"
            g.add_node(sid, ("Code", "Symbol", sym.kind), name=sym.name, qualname=sym.qualname, kind=sym.kind,
                       path=f.path, language=f.language, start_line=sym.start_line, end_line=sym.end_line,
                       signature=sym.signature, annotations=sym.annotations or None,
                       supertypes=sym.supertypes or None)
            g.add_edge("DEFINES", file_id, sid, "parser")
            if sym.parent:
                g.add_edge("CONTAINS", f"{file_id}#{sym.parent}", sid, "parser")
        for owner, start, end in chunks(f.text, s):
            cid = f"{file_id}#L{start}-{end}"
            g.add_node(cid, ("Code", "Chunk"), path=f.path, language=f.language, start_line=start, end_line=end,
                       text="\n".join(lines[start - 1:end]))
            g.add_edge("PART_OF", cid, f"{file_id}#{owner}" if owner else file_id, "parser")

    bom_to_xom = odm.build(g, [f for f in files if f.language == "odm"])

    resolver = Resolver(g, files, syntax, repo)
    resolver.link_imports()
    covered: set[str] = set()
    if scip_mode != "off":
        with tempfile.TemporaryDirectory() as tmp:
            paths = scip_files if scip_mode == "files" else scip.run_indexers(repo, Path(tmp), name, cache_dir=scip_cache)
            if paths:
                data = scip.load(paths)
                resolver.link_scip(data)
                covered = data.paths & set(syntax)
                g.nodes[name].props["scip_tools"] = data.tools
    g.counters["files_scip_covered"] = len(covered)
    resolver.link_names(skip_paths=covered)
    resolver.link_bom(bom_to_xom)

    # Edges to nodes that were never created (external or unresolved) are dropped and counted.
    for key, edge in list(g.edges.items()):
        if edge.src not in g.nodes or edge.dst not in g.nodes:
            del g.edges[key]
            g.counters["edges_dropped_dangling"] += 1
    return g


def write_json(g: CodeGraph, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "nodes.jsonl").open("w", encoding="utf-8") as fh:
        for n in g.nodes.values():
            fh.write(json.dumps({"id": n.id, "labels": n.labels, **n.props}) + "\n")
    with (out / "edges.jsonl").open("w", encoding="utf-8") as fh:
        for e in g.edges.values():
            fh.write(json.dumps({"type": e.type, "src": e.src, "dst": e.dst, **e.props}) + "\n")
    (out / "stats.json").write_text(json.dumps(g.stats(), indent=2))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--name", help="repository name in the graph (default: folder name)")
    ap.add_argument("--scip", default="auto", help="auto | off | comma-separated .scip files")
    ap.add_argument("--json", type=Path, help="also write nodes.jsonl / edges.jsonl / stats.json here")
    ap.add_argument("--no-neo4j", action="store_true")
    ap.add_argument("--full-write", action="store_true",
                    help="rewrite every node and edge (default: write only what changed)")
    ap.add_argument("--scip-cache", type=Path, default=Path(os.environ.get("SCIP_CACHE_DIR", ".kg-cache/scip")),
                    help="reuse SCIP indexes of project folders that did not change")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    name = args.name or args.repo.resolve().name
    # --scip a.scip,b.scip=frontend  (optional =folder the index was built in)
    mode, files = ("files", [(Path(p.split("=")[0]), p.split("=")[1] if "=" in p else "")
                             for p in args.scip.split(",")]) if ".scip" in args.scip else (args.scip, None)
    t0 = time.time()
    g = build_graph(args.repo, name, mode, files, args.scip_cache)
    log.info("built graph in %.1fs: %s", time.time() - t0, json.dumps(g.stats()))
    if args.json:
        write_json(g, args.json)
    if not args.no_neo4j:
        from ..graph.neo4j_writer import Neo4jWriter

        with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                         os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]) as w:
            t1 = time.time()
            result = w.write_snapshot(g) if args.full_write else w.write_delta(g)
            log.info("wrote snapshot in %.1fs: %s", time.time() - t1, result)


if __name__ == "__main__":
    main()
