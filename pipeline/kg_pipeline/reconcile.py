"""Reconciliation: remove what was deleted, archived, moved or rewritten in a source.

Incremental syncs read only what changed (Jira `updated` cursor, git commits after the cursor), so
they cannot see deletes. A reconciliation lists only the ids in the source, compares them with the
graph and removes the rest, with their sections and edges.

    kg-jira --project PREC --reconcile             # deleted issues, issues moved to another project, releases
    kg-docs --confluence ENG --reconcile            # deleted, archived and moved pages
    kg-docs --path ./docs --name d --reconcile      # deleted files; moved files keep their nodes and embeddings
    kg-git-history --repo . --name r --reconcile    # commits that are no longer on the branch (rewritten history)

Safety: when more than MAX_SHARE of the items in the graph would go (a broken listing or a lost
permission looks like a mass delete), nothing is removed unless force=True.
Each run stores its summary in SyncState `reconcile:<source>:<key>`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .graph.neo4j_writer import Neo4jWriter
from .provenance import file_url
from .sources.atlassian import Atlassian, NotFound
from .sources.documents import SUFFIXES

log = logging.getLogger("kg_pipeline.reconcile")

MAX_SHARE = 0.5      # refuse to remove more than half of a project, space, folder or repository ...
MIN_GUARD = 20       # ... when it has more than this many items
MAX_LOOKUPS = 1000   # per-item API calls that classify missing issues and pages


class TooManyRemovals(RuntimeError):
    pass


def _guard(gone: int, total: int, what: str, force: bool) -> None:
    if not force and gone > MIN_GUARD and gone > MAX_SHARE * total:
        raise TooManyRemovals(f"{what}: {gone} of {total} items would be removed; check the listing and "
                              f"run again with force=True (--force) if this is right")


def _record(w: Neo4jWriter, key: str, summary: dict) -> dict:
    summary = {**summary, "at": datetime.now(timezone.utc).isoformat()}
    short = {k: (v[:100] if isinstance(v, list) else dict(list(v.items())[:100]) if isinstance(v, dict) else v)
             for k, v in summary.items()}   # the stored audit keeps the first 100 ids of each list
    w.state(f"reconcile:{key}", json.dumps(short, sort_keys=True, default=str))
    return summary


def _delete(w: Neo4jWriter, ids: list[str]) -> int:
    """Source nodes and their sections (for documents), with all edges."""
    if not ids:
        return 0
    return w._run("UNWIND $ids AS id MATCH (n:Source {id: id}) OPTIONAL MATCH (s:Section)-[:PART_OF]->(n) "
                  "DETACH DELETE s, n RETURN count(DISTINCT n) AS n", ids=ids).records[0]["n"]


# ---- Jira ----------------------------------------------------------------------------------------------
def jira_keys(api: Atlassian, project: str) -> set[str]:
    """All issue keys of a project; the enhanced search returns up to 5000 per page for key-only fields."""
    keys, token = set(), None
    while True:
        page = api.get("/rest/api/3/search/jql", jql=f'project = "{project}"', fields="key", maxResults=5000,
                       nextPageToken=token)
        keys |= {i["key"] for i in page.get("issues", [])}
        token = page.get("nextPageToken")
        if page.get("isLast", token is None) or not token:
            return keys


def classify_issue(api: Atlassian, key: str) -> tuple[str, str | None]:
    """("deleted", None), ("moved", new key) or ("present", key). Jira redirects the old key of a moved issue."""
    try:
        issue = api.get(f"/rest/api/3/issue/{key}", fields="project")
    except NotFound:
        return "deleted", None
    return ("present", key) if issue.get("key") == key else ("moved", issue.get("key"))


def jira(w: Neo4jWriter, api: Atlassian, project: str, force: bool = False) -> dict:
    from .sources.jira import PROJECT_RE, fetch_releases
    if not PROJECT_RE.match(project):
        raise ValueError(f"invalid Jira project key {project!r}")
    graph = {r["key"]: r["id"] for r in w._run(
        "MATCH (t:Ticket {project: $p}) RETURN t.key AS key, t.id AS id", p=project).records}
    listed = jira_keys(api, project)
    missing = sorted(set(graph) - listed)
    _guard(len(missing), len(graph), f"Jira {project}", force)
    deleted, moved, kept = [], {}, []
    for i, key in enumerate(missing):
        state, new = classify_issue(api, key) if i < MAX_LOOKUPS else ("deleted", None)
        if state == "deleted":
            deleted.append(key)
        elif state == "moved":
            moved[key] = new
        else:
            kept.append(key)       # listed late (index lag); the next run sees it again
    removed = _delete(w, [graph[k] for k in deleted + list(moved)])
    releases = {f"jira:release:{project}:{v['id']}" for v in fetch_releases(api, project)}
    old_releases = [r["id"] for r in w._run("MATCH (r:Release {project: $p}) RETURN r.id AS id", p=project).records]
    removed_releases = _delete(w, [i for i in old_releases if i not in releases])
    return _record(w, f"jira:{project}", {
        "project": project, "graph": len(graph), "listed": len(listed), "deleted": deleted, "moved": moved,
        "kept": kept, "removed": removed, "removed_releases": removed_releases,
        "not_in_graph": len(listed - set(graph))})   # the next sync adds them (moved-in issues have a new `updated`)


# ---- Confluence ----------------------------------------------------------------------------------------
def confluence_ids(api: Atlassian, space_key: str) -> tuple[str, set[str]]:
    """(space id, ids of current pages), without page bodies."""
    import urllib.parse
    spaces = api.get("/wiki/api/v2/spaces", keys=space_key).get("results", [])
    if not spaces:
        raise ValueError(f"Confluence space {space_key} not found or not visible")
    path, params, ids = f"/wiki/api/v2/spaces/{spaces[0]['id']}/pages", {"status": "current", "limit": 250}, set()
    while path:
        page = api.get(path, **params)
        ids |= {str(p["id"]) for p in page.get("results", [])}
        nxt = (page.get("_links") or {}).get("next")
        path, _, query = (nxt or "").partition("?")
        params = dict(urllib.parse.parse_qsl(query))
    return str(spaces[0]["id"]), ids


def classify_page(api: Atlassian, page_id: str, space_id: str, space_keys: dict[str, str]) -> tuple[str, str | None]:
    """("deleted" | "archived" | "moved" | "present", detail). Trashed pages count as deleted."""
    try:
        p = api.get(f"/wiki/api/v2/pages/{page_id}")
    except NotFound:
        return "deleted", None
    status = p.get("status", "current")
    if status in ("trashed", "deleted", "purged"):
        return "deleted", status
    if status == "archived":
        return "archived", None
    sid = str(p.get("spaceId", space_id))
    if sid != space_id:
        if sid not in space_keys:
            try:
                space_keys[sid] = api.get(f"/wiki/api/v2/spaces/{sid}").get("key", sid)
            except NotFound:
                space_keys[sid] = sid
        return "moved", space_keys[sid]
    return ("present", None) if status == "current" else ("archived", status)   # draft, historical


def confluence(w: Neo4jWriter, api: Atlassian, space_key: str, force: bool = False) -> dict:
    space_id, listed = confluence_ids(api, space_key)
    graph = {r["id"].rsplit(":", 1)[1]: r["id"] for r in w._run(
        "MATCH (d:Document {collection: $c}) RETURN d.id AS id", c=f"confluence:{space_key}").records}
    missing = sorted(set(graph) - listed)
    _guard(len(missing), len(graph), f"Confluence {space_key}", force)
    out: dict[str, dict | list] = {"deleted": [], "archived": [], "moved": {}, "kept": []}
    keys: dict[str, str] = {}
    for i, pid in enumerate(missing):
        state, detail = classify_page(api, pid, space_id, keys) if i < MAX_LOOKUPS else ("deleted", None)
        if state == "moved":
            out["moved"][pid] = detail       # the sync of the target space adds it under its own id
        else:
            out[{"present": "kept"}.get(state, state)].append(pid)
    gone = out["deleted"] + out["archived"] + list(out["moved"])
    removed = _delete(w, [graph[p] for p in gone])
    return _record(w, f"confluence:{space_key}", {"space": space_key, "graph": len(graph), "listed": len(listed),
                                                  **out, "removed": removed, "not_in_graph": len(listed - set(graph))})


# ---- document folders ----------------------------------------------------------------------------------
def _files(root: Path) -> dict[str, Path]:
    return {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*"))
            if p.suffix.lower() in SUFFIXES and p.is_file()}


def folder_moves(w: Neo4jWriter, root: Path, name: str, url_base: str | None = None) -> dict:
    """Give a moved or renamed file (same content hash, new path) the new id instead of a delete and re-embed."""
    files = _files(root)
    graph = {r["path"]: (r["id"], r["version"]) for r in w._run(
        "MATCH (d:Document {collection: $name, source: 'files'}) RETURN d.id AS id, d.path AS path, d.version AS version",
        name=name).records}
    gone = {v: (p, i) for p, (i, v) in graph.items() if p not in files}
    moved = {}
    for rel in sorted(set(files) - set(graph)):
        version = hashlib.sha256(files[rel].read_bytes()).hexdigest()[:16]
        if version in gone:
            old_path, old_id = gone.pop(version)
            new_id = f"doc:{name}:{rel}"
            w._run("MATCH (d:Document {id: $old}) SET d.id = $new, d.path = $path, d.name = $fname, d.url = $url "
                   "WITH d MATCH (s:Section)-[:PART_OF]->(d) SET s.id = $new + substring(s.id, size($old)), s.url = $url",
                   old=old_id, new=new_id, path=rel, fname=files[rel].name, url=file_url(files[rel], rel, url_base))
            moved[old_path] = rel
    return {"moved": moved, "gone": sorted(p for p, _ in gone.values()), "graph": len(graph), "files": len(files)}


def folder(w: Neo4jWriter, root: Path, name: str, url_base: str | None = None, force: bool = False) -> dict:
    res = folder_moves(w, root, name, url_base)
    _guard(len(res["gone"]), res["graph"], f"folder {name}", force)
    removed = _delete(w, [f"doc:{name}:{p}" for p in res["gone"]])
    return _record(w, f"docs:{name}", {"collection": name, **res, "removed": removed})


# ---- git history ---------------------------------------------------------------------------------------
def reachable(repo: Path, ref: str = "HEAD") -> set[str]:
    out = subprocess.run(["git", "-C", str(repo), "rev-list", ref], capture_output=True, text=True, check=True).stdout
    return set(out.split())


def git_history(w: Neo4jWriter, repo: Path, name: str, ref: str = "HEAD", force: bool = False) -> dict:
    """Remove commits that a force push or rebase took off the branch, and reset a cursor that points at one."""
    shas = reachable(repo, ref)
    if not shas:
        raise ValueError(f"{repo}: {ref} has no commits")
    graph = {r["sha"]: r["id"] for r in w._run(
        "MATCH (c:Commit {repo: $repo}) RETURN c.sha AS sha, c.id AS id", repo=name).records}
    gone = sorted(s for s in graph if s not in shas)
    _guard(len(gone), len(graph), f"git {name}", force)
    removed = _delete(w, [graph[s] for s in gone])
    cursor = w.state(f"git:{name}:head")
    reset = bool(cursor) and cursor not in shas
    if reset:   # the next sync reads the last --max commits again
        w._run("MATCH (s:SyncState {key: $key}) DELETE s", key=f"git:{name}:head")
    return _record(w, f"git:{name}", {"repo": name, "graph": len(graph), "reachable": len(shas),
                                      "rewritten": gone, "removed": removed, "cursor_reset": reset})
