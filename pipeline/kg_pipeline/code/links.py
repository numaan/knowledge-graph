"""Links between code in all repositories: HTTP calls to routes, tests of routes, message topics.

    kg-links                              # relink every repository in Neo4j (also runs after kg-ingest-code)
    kg-links --config kg-links.yaml       # extra service -> repository map

Reads the facts that kg-ingest-code writes on Symbol nodes (http_calls, endpoint_route, publishes,
subscribes; see httpcalls.py) and the ApiOperation nodes (openapi.py), and writes:

    caller   -CALLS_HTTP->  endpoint Symbol (or ApiOperation when no endpoint matches)
    test     -TESTS->       endpoint Symbol (or ApiOperation)
    endpoint -IMPLEMENTS->  ApiOperation
    symbol   -PUBLISHES / SUBSCRIBES-> :Topic     ApiOperation (AsyncAPI) -DESCRIBES-> :Topic

All edges carry method='heuristic', a confidence, layer='links' and content_hash; a run writes only
changed edges and deletes edges that are gone. Topic nodes (id 'topic:<name>') belong to no repository.

Route match confidence: 0.9 same segments, 0.8 a parameter matches a literal segment, 0.6 one route is a
suffix of the other (unknown mount prefix, at least 2 segments). +0.05 when a base-URL hint maps to the
target repository in kg-links.yaml; x0.8 when 2 or 3 targets tie (more than 3: not linked).

kg-links.yaml (in a repository root, or --config / KG_LINKS_CONFIG):

    services:
      billing:                       # matched against base-URL variable names and hosts (BILLING_URL, billing-svc)
        repo: billing-service
        base_path: /api              # optional prefix that the base URL adds
        hints: [PAYMENTS_HOST]       # optional extra names
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path

import yaml

from .httpcalls import is_test_path, join, norm_route
from .model import CodeGraph

log = logging.getLogger("kg_pipeline.code.links")
MAX_TIES = 3


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def merge_config(*configs: dict | None) -> dict[str, dict]:
    services: dict[str, dict] = {}
    for cfg in configs:
        for name, svc in ((cfg or {}).get("services") or {}).items():
            if isinstance(svc, dict) and svc.get("repo"):
                services[str(name)] = {"repo": str(svc["repo"]), "base_path": str(svc.get("base_path") or ""),
                                       "hints": [str(h) for h in svc.get("hints") or []]}
    return services


def _service(hint: str | None, services: dict[str, dict]) -> dict | None:
    """Service for a base-URL variable name or host: exact name or hint, else the longest name it contains."""
    if not hint:
        return None
    h = _key(hint)
    best = None
    for name, svc in services.items():
        keys = [_key(name)] + [_key(x) for x in svc["hints"]]
        if h in keys:
            return svc
        for k in keys:
            if k and len(k) >= 3 and k in h and (best is None or len(k) > best[0]):
                best = (len(k), svc)
    return best[1] if best else None


def match(call: list[str], route: list[str]) -> tuple[float, str] | None:
    """Confidence and kind of a match between two normalised route segment lists."""
    def same(a: list[str], b: list[str]) -> tuple[bool, bool]:
        loose = False
        for x, y in zip(a, b):
            if x == y:
                continue
            if x == "{}" or y == "{}":
                loose = True
                continue
            return False, False
        return True, loose

    if len(call) == len(route):
        ok, loose = same(call, route)
        return ((0.8, "param") if loose else (0.9, "exact")) if ok else None
    short, long_ = sorted((call, route), key=len)
    if len(short) >= 2 and any(s != "{}" for s in short):
        ok, _ = same(short, long_[-len(short):])
        if ok:
            return 0.6, "suffix"
    return None


def _segments(path: str) -> list[str]:
    return [s for s in norm_route(path).split("/") if s]


def _parse_call(text: str) -> tuple[str, str, str | None]:
    """'POST /api/x @BASE' -> ('POST', '/api/x', 'BASE')."""
    head, _, hint = text.partition(" @")
    verb, _, path = head.partition(" ")
    return verb, path, hint or None


def resolve(symbols: list[dict], operations: list[dict], services: dict[str, dict]) -> tuple[list[dict], dict[str, dict]]:
    """Pure link resolution. Returns (edges, topics); edges are {type, src, dst, **props}."""
    endpoints = [s for s in symbols if s.get("endpoint_route")]
    http_ops = [o for o in operations if o.get("spec") == "openapi"]
    targets_ep = [(s["id"], s["repo"], s.get("endpoint_method") or "ANY", _segments(s["endpoint_route"])) for s in endpoints]
    targets_op = [(o["id"], o["repo"], o.get("method") or "ANY", _segments(o["path_template"])) for o in http_ops]
    edges: dict[tuple[str, str, str], dict] = {}

    def best(verb: str, segs: list[str], targets, repo_filter: set[str] | None, home: str, skip: str):
        scored = []
        for tid, repo, tverb, tsegs in targets:
            if tid == skip or repo_filter and repo not in repo_filter:
                continue
            if verb != "ANY" and tverb != "ANY" and verb != tverb:
                continue
            m = match(segs, tsegs)
            if m:
                scored.append((m[0], m[1], tid, repo))
        if not scored:
            return []
        top = max(s[0] for s in scored)
        tied = [s for s in scored if s[0] == top]
        if len({s[3] for s in tied}) > 1 and any(s[3] == home for s in tied):
            tied = [s for s in tied if s[3] == home]        # prefer the caller's own repository on a tie
        if len(tied) > MAX_TIES:
            return None
        factor = 0.8 if len(tied) > 1 else 1.0
        return [(round(s[0] * factor, 2), s[1], s[2], s[3]) for s in tied]

    def add(etype: str, src: str, dst: str, conf: float, **props) -> None:
        key = (etype, src, dst)
        if key not in edges or conf > edges[key]["confidence"]:
            edges[key] = {"type": etype, "src": src, "dst": dst, "method": "heuristic", "confidence": conf,
                          "layer": "links", **{k: v for k, v in props.items() if v is not None}}

    for s in symbols:
        etype = "TESTS" if is_test_path(s.get("path") or "") else "CALLS_HTTP"
        for call in s.get("http_calls") or []:
            verb, path, hint = _parse_call(call)
            svc = _service(hint, services)
            repos = {svc["repo"]} if svc else None
            full = join(svc["base_path"], path) if svc and svc["base_path"] else path
            segs = _segments(full)
            for targets in (targets_ep, targets_op):     # endpoint first, then the contract
                found = best(verb, segs, targets, repos, s["repo"], s["id"])
                if found is None:
                    break                                   # too ambiguous to link
                for conf, kind, tid, repo in found:
                    add(etype, s["id"], tid, min(0.95, conf + (0.05 if svc else 0)), http_method=verb,
                        route=full, match=kind, cross_repo=repo != s["repo"] or None)
                if found:
                    break

    for o in http_ops:
        segs = _segments(o["path_template"])
        for conf, kind, tid, repo in best(o.get("method") or "ANY", segs, targets_ep, None, o["repo"], "") or []:
            add("IMPLEMENTS", tid, o["id"], conf, match=kind, cross_repo=repo != o["repo"] or None)

    topics: dict[str, dict] = {}

    def topic(name: str, broker: str | None, repo: str) -> str:
        tid = f"topic:{name}"
        t = topics.setdefault(tid, {"id": tid, "name": name, "brokers": set(), "repos": set(), "layer": "links"})
        if broker:
            t["brokers"].add(broker)
        t["repos"].add(repo)
        return tid

    for s in symbols:
        for direction, etype in (("publishes", "PUBLISHES"), ("subscribes", "SUBSCRIBES")):
            for item in s.get(direction) or []:
                broker, _, name = item.partition(":")
                add(etype, s["id"], topic(name, broker, s["repo"]), 0.8, broker=broker)
    for o in operations:
        if o.get("spec") == "asyncapi" and o.get("channel"):
            add("DESCRIBES", o["id"], topic(o["channel"], None, o["repo"]), 0.9,
                action=o.get("method"))
    for t in topics.values():
        t["brokers"], t["repos"] = sorted(t["brokers"]), sorted(t["repos"])
    return list(edges.values()), topics


# ---- inputs ------------------------------------------------------------------------------------

def facts_from_graphs(graphs: list[CodeGraph]) -> tuple[list[dict], list[dict], dict[str, dict]]:
    """Inputs of resolve() from in-memory graphs (tests and --json runs)."""
    symbols, operations, configs = [], [], []
    for g in graphs:
        for n in g.nodes.values():
            p = n.props
            if "ApiOperation" in n.labels:
                operations.append({"id": n.id, **p})
            elif any(p.get(k) for k in ("http_calls", "publishes", "subscribes", "endpoint_route")):
                symbols.append({"id": n.id, **p})
            if "Repository" in n.labels and p.get("links_config"):
                configs.append(json.loads(p["links_config"]))
    return symbols, operations, merge_config(*configs)


def facts_from_neo4j(w) -> tuple[list[dict], list[dict], dict[str, dict]]:
    symbols = [r.data() for r in w._run(
        "MATCH (s:Code) WHERE s.http_calls IS NOT NULL OR s.publishes IS NOT NULL OR s.subscribes IS NOT NULL "
        "OR s.endpoint_route IS NOT NULL RETURN s.id AS id, s.repo AS repo, s.path AS path, s.http_calls AS http_calls, "
        "s.publishes AS publishes, s.subscribes AS subscribes, s.endpoint_method AS endpoint_method, "
        "s.endpoint_route AS endpoint_route").records]
    operations = [r.data() for r in w._run(
        "MATCH (o:ApiOperation) RETURN o.id AS id, o.repo AS repo, o.method AS method, o.path_template AS path_template, "
        "o.channel AS channel, o.spec AS spec").records]
    configs = [json.loads(r["c"]) for r in w._run(
        "MATCH (r:Repository) WHERE r.links_config IS NOT NULL RETURN r.links_config AS c").records]
    return symbols, operations, merge_config(*configs)


def load_config(path: Path | None) -> dict:
    path = path or (Path(os.environ["KG_LINKS_CONFIG"]) if os.environ.get("KG_LINKS_CONFIG") else None)
    if not (path and path.exists()):
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---- Neo4j -------------------------------------------------------------------------------------

def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


def sync(w, config: dict | None = None) -> dict:
    """Recompute every link edge and write only the differences."""
    w.ensure_schema()
    symbols, operations, services = facts_from_neo4j(w)
    services = {**services, **merge_config(config)}
    edges, topics = resolve(symbols, operations, services)

    old_topics = {r["id"]: r["h"] for r in w._run("MATCH (t:Topic) RETURN t.id AS id, t.content_hash AS h").records}
    rows = [{"id": t["id"], "props": {**t, "content_hash": _hash(t)}} for t in topics.values()]
    rows = [r for r in rows if old_topics.get(r["id"]) != r["props"]["content_hash"]]
    if rows:
        w._run("UNWIND $rows AS row MERGE (t:Code {id: row.id}) SET t = row.props, t:Topic", rows=rows)

    old = {(r["a"], r["t"], r["b"]): r["h"] for r in w._run(
        "MATCH (a:Code)-[r]->(b:Code) WHERE r.layer = 'links' RETURN a.id AS a, type(r) AS t, b.id AS b, "
        "r.content_hash AS h").records}
    new = {(e["src"], e["type"], e["dst"]): e for e in edges}
    changed = defaultdict(list)
    for key, e in new.items():
        props = {k: v for k, v in e.items() if k not in ("type", "src", "dst")}
        h = _hash(props)
        if old.get(key) != h:
            changed[e["type"]].append({"src": e["src"], "dst": e["dst"], "props": {**props, "content_hash": h}})
    for etype, batch in changed.items():
        if not re.match(r"^[A-Z_]+$", etype):
            raise ValueError(etype)
        w._run(f"UNWIND $rows AS row MATCH (a:Code {{id: row.src}}) MATCH (b:Code {{id: row.dst}}) "
               f"MERGE (a)-[r:{etype}]->(b) SET r = row.props", rows=batch)
    gone = defaultdict(list)
    for (a, t, b) in old:
        if (a, t, b) not in new:
            gone[t].append({"a": a, "b": b})
    for etype, batch in gone.items():
        w._run(f"UNWIND $rows AS row MATCH (:Code {{id: row.a}})-[r:{etype}]->(:Code {{id: row.b}}) "
               f"WHERE r.layer = 'links' DELETE r", rows=batch)
    stale = [t for t in old_topics if t not in topics]
    if stale:
        w._run("UNWIND $ids AS id MATCH (t:Topic {id: id}) DETACH DELETE t", ids=stale)
    counts = defaultdict(int)
    for e in edges:
        counts[e["type"]] += 1
    return {"edges": dict(counts), "topics": len(topics), "edges_written": sum(map(len, changed.values())),
            "edges_deleted": sum(map(len, gone.values())), "topics_deleted": len(stale)}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="kg-links.yaml with a service -> repository map")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from ..graph.neo4j_writer import Neo4jWriter

    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        log.info("links: %s", sync(w, load_config(args.config)))


if __name__ == "__main__":
    main()
