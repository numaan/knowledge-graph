"""Service catalog: Backstage catalog-info.yaml and CODEOWNERS of a repository into the graph.

    kg-catalog --repo /path/to/checkout --name shop

Run it after kg-ingest-code: services link to the File and Repository nodes of the code graph.
A run is skipped when the catalog files and the list of files did not change (content hash).

Backstage kinds: Component -> :Service, API -> :API, System -> :System, Group -> :Team,
User -> :CatalogUser (merged into :Person by kg-people). Other kinds are ignored.

Graph:
    (:Team) -[:OWNS {via: catalog | codeowners}]-> (:Service | :API | :System | :File)
    (:Service | :API) -[:PART_OF]-> (:System)       (:Team) -[:PART_OF]-> (:Team)  parent group
    (:Service) -[:IMPLEMENTED_IN]-> (:Repository) when catalog-info.yaml is at the root, else -> each (:File) of its folder
    (:Service) -[:PROVIDES_API | :CONSUMES_API]-> (:API)     (:Service) -[:DEPENDS_ON]-> (:Service)
    (:Source:CodeOwners {repo, rules}) one node per repository; kg-people reads it for person owners

Ownership fallback: a Component without spec.owner gets the CODEOWNERS owners of its folder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
from pathlib import Path

import yaml

from ..graph.neo4j_writer import Neo4jWriter
from ..people import email_hash

log = logging.getLogger("kg_pipeline.catalog")

CATALOG_FILES = ("catalog-info.yaml", "catalog-info.yml")
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS")
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".venv", "target"}
KINDS = {"component": "Service", "api": "API", "system": "System", "group": "Team", "user": "CatalogUser"}
DEFAULT_KIND = {"owner": "group", "system": "system", "providesApis": "api", "consumesApis": "api",
                "dependsOn": "component", "parent": "group", "memberOf": "group", "members": "user"}


# ---- Backstage entity references ------------------------------------------------------------------------
def ref(value: str, default_kind: str, namespace: str = "default") -> str:
    """Entity ref in full form kind:namespace/name, lower case ("team-a" -> "group:default/team-a")."""
    kind, _, rest = value.partition(":") if ":" in value else (default_kind, "", value)
    ns, _, name = rest.partition("/") if "/" in rest else (namespace, "", rest)
    return f"{kind}:{ns}/{name}".lower()


def node_id(entity_ref: str) -> str:
    return f"catalog:{entity_ref}"


def find_catalog_files(root: Path) -> list[Path]:
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        out += [Path(dirpath) / f for f in sorted(files) if f in CATALOG_FILES]
    return out


def parse_entities(text: str, folder: str) -> list[dict]:
    """Entities of one catalog-info.yaml (several YAML documents); folder is its path in the repo ('' = root)."""
    out = []
    for doc in yaml.safe_load_all(text):
        if not isinstance(doc, dict) or (doc.get("kind") or "").lower() not in KINDS:
            continue
        meta, spec = doc.get("metadata") or {}, doc.get("spec") or {}
        if not meta.get("name"):
            continue
        out.append({"kind": doc["kind"].lower(), "meta": meta, "spec": spec, "folder": folder,
                    "ref": ref(f"{doc['kind']}:{meta.get('namespace', 'default')}/{meta['name']}", "")})
    return out


def _refs(spec: dict, field: str, ns: str) -> list[str]:
    value = spec.get(field)
    values = value if isinstance(value, list) else [value] if value else []
    return [ref(str(v), DEFAULT_KIND[field], ns) for v in values]


def entity_rows(entities: list[dict], repo: str) -> tuple[list[dict], list[dict]]:
    """(node rows with label, edge rows with type, src, dst, dst_anchor)."""
    nodes, edges, acl = [], [], [f"repo:{repo}"]
    for e in entities:
        meta, spec, ns = e["meta"], e["spec"], (e["meta"].get("namespace") or "default").lower()
        nid, label = node_id(e["ref"]), KINDS[e["kind"]]
        profile = spec.get("profile") or {}
        ann = meta.get("annotations") or {}
        row = {"id": nid, "label": label, "ref": e["ref"], "name": meta["name"], "repo": repo, "folder": e["folder"],
               "title": profile.get("displayName") or meta.get("title") or meta["name"],
               "text": meta.get("description") or "", "type": spec.get("type"), "lifecycle": spec.get("lifecycle"),
               "tags": meta.get("tags") or [], "owner": (_refs(spec, "owner", ns) or [None])[0],
               "system": (_refs(spec, "system", ns) or [None])[0], "acl": acl, "source": "catalog",
               "source_location": ann.get("backstage.io/source-location")}
        if label == "CatalogUser":
            row["email_hash"] = email_hash(profile.get("email"))      # the address itself is never stored
            row["github"] = ann.get("github.com/user-login")
            row["member_of"] = [node_id(r) for r in _refs(spec, "memberOf", ns)]
        if label == "Team":
            row["members"] = _refs(spec, "members", ns)
        if label == "API":
            row["definition_chars"] = len(str(spec.get("definition") or ""))
        nodes.append(row)
        link = lambda t, field, anchor="Source": edges.extend(
            {"type": t, "src": nid, "dst": node_id(r), "dst_anchor": anchor} for r in _refs(spec, field, ns))
        if label in ("Service", "API", "System"):
            for r in _refs(spec, "owner", ns):
                if r.startswith("group:"):
                    edges.append({"type": "OWNS", "src": node_id(r), "dst": nid, "dst_anchor": "Source", "via": "catalog"})
        if label in ("Service", "API"):
            link("PART_OF", "system")
        if label == "Service":
            link("PROVIDES_API", "providesApis")
            link("CONSUMES_API", "consumesApis")
            edges.extend({"type": "DEPENDS_ON", "src": nid, "dst": node_id(r), "dst_anchor": "Source"}
                         for r in _refs(spec, "dependsOn", ns) if r.startswith("component:"))
        if label == "Team":
            link("PART_OF", "parent")
    # Users list their groups, groups list their members: keep both directions on the user.
    users = {n["ref"]: n for n in nodes if n["label"] == "CatalogUser"}
    for team in (n for n in nodes if n["label"] == "Team"):
        for m in team.pop("members"):
            if m in users and team["id"] not in users[m]["member_of"]:
                users[m]["member_of"].append(team["id"])
    return nodes, edges


# ---- CODEOWNERS -------------------------------------------------------------------------------------------
def parse_codeowners(text: str) -> list[tuple[str, list[str]]]:
    """[(pattern, owners)] in file order; the last matching rule wins (GitHub semantics)."""
    rules = []
    for line in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if not line or line.startswith("["):   # GitLab [Section] headers
            continue
        pattern, *owners = line.split()
        rules.append((pattern, owners))
    return rules


def _pattern_re(pattern: str) -> re.Pattern:
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    p = pattern.strip("/")
    body = ""
    i = 0
    while i < len(p):
        if p[i:i + 3] == "**/":
            body += "(?:.*/)?"
            i += 3
        elif p[i:i + 2] == "**":
            body += ".*"
            i += 2
        elif p[i] == "*":
            body += "[^/]*"
            i += 1
        elif p[i] == "?":
            body += "[^/]"
            i += 1
        else:
            body += re.escape(p[i])
            i += 1
    prefix = "^" if anchored else r"^(?:.*/)?"
    # A pattern matches the path itself or anything below it (a directory)
    return re.compile(prefix + body + r"(?:/.*)?$")


def owners_of(path: str, rules: list[tuple[str, list[str]]]) -> tuple[list[str], str | None]:
    for pattern, owners in reversed(rules):
        if _pattern_re(pattern).match(path):
            return owners, pattern
    return [], None


def read_codeowners(root: Path) -> list[tuple[str, list[str]]]:
    for rel in CODEOWNERS_PATHS:
        if (root / rel).is_file():
            return parse_codeowners((root / rel).read_text(encoding="utf-8", errors="replace"))
    return []


def team_handle(owner: str) -> str | None:
    """@org/team -> group name 'team'; a person (@login, email) -> None."""
    o = owner.lstrip("@")
    return o.split("/", 1)[1].lower() if "/" in o and "@" not in o else None


def codeowner_files(w: Neo4jWriter) -> list[tuple[str, list[tuple[str, str]], list[str]]]:
    """Person owners in every repository: [(owner, [(file id, pattern)], acl)]. Used by kg-people."""
    out = []
    for r in w._run("MATCH (c:CodeOwners) RETURN c.repo AS repo, c.rules AS rules, c.acl AS acl").records:
        rules = [tuple(json.loads(x)) for x in r["rules"] or []]
        if not any(team_handle(o) is None for _, owners in rules for o in owners):
            continue
        paths = [x["p"] for x in w._run("MATCH (f:File {repo: $repo}) RETURN f.path AS p", repo=r["repo"]).records]
        files: dict[str, list[tuple[str, str]]] = {}
        for p in paths:
            owners, pattern = owners_of(p, rules)
            for o in owners:
                if team_handle(o) is None:
                    files.setdefault(o, []).append((f"{r['repo']}:{p}", pattern))
        out += [(o, fs, r["acl"]) for o, fs in files.items()]
    return out


# ---- sync -------------------------------------------------------------------------------------------------
def build(root: Path, repo: str, paths: list[str]) -> dict:
    """Everything kg-catalog writes for one checkout, without touching the graph (for tests)."""
    entities = []
    for f in find_catalog_files(root):
        folder = f.parent.relative_to(root).as_posix()
        entities += parse_entities(f.read_text(encoding="utf-8", errors="replace"), "" if folder == "." else folder)
    nodes, edges = entity_rows(entities, repo)
    rules = read_codeowners(root)
    known = {n["name"].lower(): n["id"] for n in nodes if n["label"] == "Team"}
    teams = {}
    for _, owners in rules:
        for o in owners:
            if (t := team_handle(o)) and t not in known:
                teams[t] = {"id": node_id(f"group:default/{t}"), "label": "Team", "ref": f"group:default/{t}", "name": t,
                            "title": o.lstrip("@"), "text": "", "repo": repo, "acl": [f"repo:{repo}"],
                            "source": "codeowners"}
                known[t] = teams[t]["id"]
    nodes += teams.values()
    for p in paths:
        owners, pattern = owners_of(p, rules)
        for o in owners:
            if t := team_handle(o):
                edges.append({"type": "OWNS", "src": known[t], "dst": f"{repo}:{p}", "dst_anchor": "Code",
                              "via": "codeowners", "pattern": pattern})
    for svc in (n for n in nodes if n["label"] == "Service"):
        folder = svc["folder"]
        if not folder:
            edges.append({"type": "IMPLEMENTED_IN", "src": svc["id"], "dst": repo, "dst_anchor": "Code"})
        else:
            edges += [{"type": "IMPLEMENTED_IN", "src": svc["id"], "dst": f"{repo}:{p}", "dst_anchor": "Code"}
                      for p in paths if p.startswith(folder + "/")]
        if not svc["owner"]:  # fallback: CODEOWNERS of the folder
            owners, pattern = owners_of((folder + "/" if folder else "") + "catalog-info.yaml", rules)
            for o in owners:
                if t := team_handle(o):
                    edges.append({"type": "OWNS", "src": known[t], "dst": svc["id"], "dst_anchor": "Source",
                                  "via": "codeowners", "pattern": pattern})
            svc["owner"] = owners[0] if owners else None
            svc["owner_source"] = "codeowners" if owners else None
    # A user owner (spec.owner: user:x) becomes a Person -> OWNS edge in kg-people.
    users = {n["ref"]: n for n in nodes if n["label"] == "CatalogUser"}
    for n in nodes:
        if (n.get("owner") or "").startswith("user:") and n["owner"] in users:
            users[n["owner"]].setdefault("owns", []).append(n["id"])
    codeowners = {"id": f"codeowners:{repo}", "repo": repo, "title": f"CODEOWNERS of {repo}",
                  "rules": [json.dumps([p, o]) for p, o in rules], "acl": [f"repo:{repo}"], "source": "codeowners"}
    return {"nodes": nodes, "edges": edges, "codeowners": codeowners}


def sync(w: Neo4jWriter, root: Path, repo: str, force: bool = False) -> dict:
    paths = [r["p"] for r in w._run("MATCH (f:File {repo: $repo}) RETURN f.path AS p ORDER BY p", repo=repo).records]
    out = build(root, repo, paths)
    digest = hashlib.sha256(json.dumps([out["nodes"], out["edges"], out["codeowners"]], sort_keys=True,
                                       default=str).encode()).hexdigest()[:20]
    old = w._run("MATCH (c:CodeOwners {id: $id}) RETURN c.content_hash AS h", id=f"codeowners:{repo}").records
    if old and old[0]["h"] == digest and not force:
        return {"repo": repo, "unchanged": True}
    by_label: dict[str, list[dict]] = {}
    for n in out["nodes"]:
        by_label.setdefault(n["label"], []).append({k: v for k, v in n.items() if k != "label"})
    ids = [n["id"] for n in out["nodes"]]
    # Entities that left this repository's catalog files are removed.
    w._run("MATCH (n:Source {repo: $repo}) WHERE n.source IN ['catalog', 'codeowners'] AND "
           "(n:Service OR n:API OR n:System OR n:Team OR n:CatalogUser) AND NOT n.id IN $ids DETACH DELETE n",
           repo=repo, ids=ids)
    for label, rows in by_label.items():
        w.upsert((label,), rows)
    w.upsert(("CodeOwners",), [{**out["codeowners"], "content_hash": digest}])
    for etype in ("OWNS", "PART_OF", "PROVIDES_API", "CONSUMES_API", "DEPENDS_ON", "IMPLEMENTED_IN"):
        for anchor in ("Source", "Code"):
            rows = [{"src": e["src"], "dst": e["dst"], "method": e.get("via", "catalog"), "confidence": 1.0,
                     **{k: v for k, v in e.items() if k in ("via", "pattern")}}
                    for e in out["edges"] if e["type"] == etype and e["dst_anchor"] == anchor]
            w.link(etype, rows, dst=anchor, replace_from=ids)
    counts = {}
    for n in out["nodes"]:
        counts[n["label"]] = counts.get(n["label"], 0) + 1
    return {"repo": repo, **{k.lower(): v for k, v in counts.items()}, "edges": len(out["edges"]),
            "codeowners_rules": len(out["codeowners"]["rules"])}


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, required=True, help="checkout of the repository")
    ap.add_argument("--name", help="repository name in the graph (default: folder name)")
    ap.add_argument("--force", action="store_true", help="write even when nothing changed")
    args = ap.parse_args(argv)
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        print(json.dumps(sync(w, args.repo, args.name or args.repo.resolve().name, args.force), indent=2))
