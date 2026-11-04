"""Precise ACLs on Jira and Confluence nodes from the permissions in Atlassian Cloud.

    kg-permissions --jira PREC --confluence ENG          # run after kg-jira and kg-docs --confluence
    kg-permissions --jira PREC --dry-run                 # print the ACLs, write nothing

Jira: the project's permission scheme (BROWSE_PROJECTS holders), project role actors, the issue
security scheme and the issues that have a security level. Confluence: space read permissions and
the read restrictions of each page and its ancestors.

ACL groups written (the MCP server maps token groups to them with acl-mapping.yaml):
    jira:PREC                    anyone who may browse project PREC (kept for team mappings)
    jira-group:<name>            members of this Atlassian group (lower case)
    jira:users                   any Jira user (permission held by "anyone" or an application role)
    jira:PREC:level:<id>         an issue security level; restricted issues do not get jira:PREC
    confluence:ENG               anyone who may read space ENG
    confluence-group:<name>      members of this group
    acl:deny                     nobody (except "*"): restrictions that name only users, or unknown

When the permissions cannot be read (no admin right, HTTP 403/404), KG_ACL_DEFAULT decides:
deny (default) writes acl:deny, project keeps jira:<KEY> or confluence:<KEY>. A list is never empty,
because an empty acl means public. Only nodes whose acl changed are written.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.parse
from collections import defaultdict

from ..graph.neo4j_writer import Neo4jWriter
from .atlassian import Atlassian
from .jira import PROJECT_RE

log = logging.getLogger("kg_pipeline.permissions")

DENY = "acl:deny"
BROWSE = "BROWSE_PROJECTS"


def default_acl(kind: str, key: str) -> list[str]:
    mode = os.environ.get("KG_ACL_DEFAULT", "deny")
    if mode not in ("deny", "project"):
        raise ValueError(f"KG_ACL_DEFAULT must be deny or project, not {mode!r}")
    return [f"{kind}:{key}"] if mode == "project" else [DENY]


def _group(holder: dict) -> str | None:
    name = (holder.get("group") or {}).get("name") or holder.get("parameter")
    return name.strip().lower() if name else None


# ---- Jira -----------------------------------------------------------------------------------------

def _role_groups(api: Atlassian, project: str) -> dict[str, set[str]]:
    """role id -> group names of the role's actors in this project (user actors are not mapped)."""
    out = {}
    for url in api.get(f"/rest/api/3/project/{project}/role").values():
        role_id = str(url).rstrip("/").rsplit("/", 1)[-1]
        role = api.get(f"/rest/api/3/project/{project}/role/{role_id}")
        out[role_id] = {a["actorGroup"]["name"].lower() for a in role.get("actors", [])
                        if a.get("type") == "atlassian-group-role-actor" and (a.get("actorGroup") or {}).get("name")}
    return out


def _holders(holders: list[dict], roles: dict[str, set[str]], users_entry: str = "jira:users") -> set[str]:
    """Permission holders -> ACL groups. User-type holders (user, reporter, assignee, lead) are skipped."""
    out = set()
    for h in holders:
        t = h.get("type")
        if t == "group" and _group(h):
            out.add(f"jira-group:{_group(h)}")
        elif t == "projectRole":
            out |= {f"jira-group:{g}" for g in roles.get(str(h.get("parameter") or (h.get("projectRole") or {}).get("id")), set())}
        elif t in ("anyone", "applicationRole"):
            out.add(users_entry)
    return out


def jira_project_acl(api: Atlassian, project: str) -> list[str] | None:
    """ACL of an issue without security level; None when the permissions cannot be read."""
    try:
        roles = _role_groups(api, project)
        scheme = api.get(f"/rest/api/3/project/{project}/permissionscheme")
        perms = api.get(f"/rest/api/3/permissionscheme/{scheme['id']}/permission", expand="group").get("permissions", [])
    except (RuntimeError, KeyError) as e:
        log.warning("Jira %s: cannot read permissions (%s)", project, e)
        return None
    groups = _holders([p.get("holder") or {} for p in perms if p.get("permission") == BROWSE], roles)
    return sorted({f"jira:{project}"} | groups)


def jira_level_acls(api: Atlassian, project: str) -> dict[str, list[str]] | None:
    """security level id -> ACL; {} when the project has no security scheme; None when unreadable."""
    try:
        scheme = api.get(f"/rest/api/3/project/{project}/issuesecuritylevelscheme")
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return {}
        log.warning("Jira %s: cannot read the issue security scheme (%s)", project, e)
        return None
    if not scheme or not scheme.get("id"):
        return {}
    try:
        roles = _role_groups(api, project)
        members, start = defaultdict(list), 0
        while True:
            page = api.get("/rest/api/3/issuesecurityschemes/level/member", schemeId=scheme["id"], startAt=start,
                           maxResults=50, expand="group")
            for m in page.get("values", []):
                members[str(m["issueSecurityLevelId"])].append(m.get("holder") or {})
            start += len(page.get("values", []))
            if page.get("isLast", True) or not page.get("values"):
                break
    except (RuntimeError, KeyError) as e:
        log.warning("Jira %s: cannot read security level members (%s)", project, e)
        return None
    levels = {str(l["id"]) for l in scheme.get("levels", [])} | set(members)
    return {lid: sorted({f"jira:{project}:level:{lid}"} | _holders(members.get(lid, []), roles)) for lid in levels}


def jira_issue_levels(api: Atlassian, project: str) -> dict[str, str] | None:
    """issue key -> security level id, for the issues that have one."""
    out, token = {}, None
    try:
        while True:
            page = api.get("/rest/api/3/search/jql", jql=f'project = "{project}" AND level IS NOT EMPTY',
                           fields="security", maxResults=100, nextPageToken=token)
            for issue in page.get("issues", []):
                if (issue.get("fields") or {}).get("security"):
                    out[issue["key"]] = str(issue["fields"]["security"]["id"])
            token = page.get("nextPageToken")
            if page.get("isLast", token is None) or not token:
                return out
    except RuntimeError as e:
        log.warning("Jira %s: cannot list issues with a security level (%s)", project, e)
        return None


def jira_acls(api: Atlassian, project: str) -> tuple[list[str], dict[str, list[str]]]:
    """(ACL for unrestricted issues and releases, {ticket id: ACL} for issues with a security level)."""
    if not PROJECT_RE.match(project):
        raise ValueError(f"invalid Jira project key {project!r}")
    unknown = default_acl("jira", project)
    base = jira_project_acl(api, project)
    issues = jira_issue_levels(api, project)
    if issues is None:                     # we cannot tell restricted issues apart: all are unknown
        return unknown, {}
    levels = jira_level_acls(api, project) if issues else {}
    restricted = {}
    for key, lid in issues.items():
        acl = (levels or {}).get(lid)
        restricted[f"jira:{key}"] = acl if acl else unknown
    return base or unknown, restricted


# ---- Confluence -----------------------------------------------------------------------------------

def _pages(api: Atlassian, path: str, params: dict, prefix: str = ""):
    while path:
        page = api.get(path, **params)
        yield from page.get("results", [])
        nxt = (page.get("_links") or {}).get("next")
        path, _, query = (nxt or "").partition("?")
        if path and prefix and not path.startswith(prefix):
            path = prefix + path              # v1 links are relative to /wiki
        params = dict(urllib.parse.parse_qsl(query))


def confluence_space_acl(api: Atlassian, space_key: str) -> list[str] | None:
    try:
        spaces = api.get("/wiki/api/v2/spaces", keys=space_key).get("results", [])
        if not spaces:
            return None
        names, groups = {}, set()
        for p in _pages(api, f"/wiki/api/v2/spaces/{spaces[0]['id']}/permissions", {"limit": 250}):
            op, principal = p.get("operation") or {}, p.get("principal") or {}
            if op.get("key") == "read" and op.get("targetType") == "space" and principal.get("type") == "group":
                gid = principal["id"]
                if gid not in names:
                    names[gid] = (api.get("/wiki/rest/api/group/by-id", id=gid).get("name") or "").lower()
                if names[gid]:
                    groups.add(f"confluence-group:{names[gid]}")
    except (RuntimeError, KeyError) as e:
        log.warning("Confluence %s: cannot read space permissions (%s)", space_key, e)
        return None
    return sorted({f"confluence:{space_key}"} | groups)


def confluence_page_acls(api: Atlassian, space_key: str, space_acl: list[str]) -> dict[str, list[str]] | None:
    """Document id -> ACL. A read restriction on a page or an ancestor limits the page to the groups
    that every restriction names; restrictions that name only users give acl:deny."""
    expand = "restrictions.read.restrictions.group,restrictions.read.restrictions.user,ancestors"
    try:
        pages = list(_pages(api, "/wiki/rest/api/content", {"spaceKey": space_key, "type": "page", "limit": 100,
                                                            "expand": expand}, prefix="/wiki"))
    except RuntimeError as e:
        log.warning("Confluence %s: cannot read page restrictions (%s)", space_key, e)
        return None
    own = {}
    for p in pages:
        r = ((p.get("restrictions") or {}).get("read") or {}).get("restrictions") or {}
        groups = {g["name"].lower() for g in (r.get("group") or {}).get("results", []) if g.get("name")}
        users = (r.get("user") or {}).get("results", [])
        own[str(p["id"])] = groups if groups or users else None    # None: no read restriction
    out = {}
    for p in pages:
        chain = [str(a["id"]) for a in p.get("ancestors", [])] + [str(p["id"])]
        sets = [own[c] for c in chain if own.get(c) is not None]
        if not sets:
            acl = space_acl
        else:
            allowed = set.intersection(*sets)
            acl = sorted(f"confluence-group:{g}" for g in allowed) or [DENY]
        out[f"confluence:{space_key}:{p['id']}"] = acl
    return out


def confluence_acls(api: Atlassian, space_key: str) -> tuple[list[str], dict[str, list[str]]]:
    unknown = default_acl("confluence", space_key)
    space = confluence_space_acl(api, space_key) or unknown
    pages = confluence_page_acls(api, space_key, space)
    if pages is None:   # restrictions unknown: no page may get the wider space ACL
        return unknown, {}
    return space, pages


# ---- write ----------------------------------------------------------------------------------------

def _write(w: Neo4jWriter, current: list, wanted: dict[str, list[str]], sections: bool = False) -> int:
    changed = [{"id": r["id"], "acl": wanted[r["id"]]} for r in current
               if r["id"] in wanted and sorted(r["acl"] or []) != wanted[r["id"]]]
    if changed:
        w._run("UNWIND $rows AS row MATCH (n:Source {id: row.id}) SET n.acl = row.acl, n.acl_synced_at = datetime() "
               + ("WITH n, row OPTIONAL MATCH (s:Section)-[:PART_OF]->(n) SET s.acl = row.acl" if sections else ""),
               rows=changed)
    return len(changed)


def sync_jira(w: Neo4jWriter, api: Atlassian, project: str, dry_run: bool = False) -> dict:
    base, restricted = jira_acls(api, project)
    nodes = w._run("MATCH (n:Source) WHERE (n:Ticket OR n:Release) AND n.project = $p RETURN n.id AS id, n.acl AS acl",
                   p=project).records
    wanted = {r["id"]: restricted.get(r["id"], base) for r in nodes}
    written = 0 if dry_run else _write(w, nodes, wanted)
    return {"project": project, "acl": base, "restricted": len(restricted), "nodes": len(nodes), "written": written,
            **({"acls": wanted} if dry_run else {})}


def sync_confluence(w: Neo4jWriter, api: Atlassian, space_key: str, dry_run: bool = False) -> dict:
    space, pages = confluence_acls(api, space_key)
    nodes = w._run("MATCH (d:Document {collection: $c}) RETURN d.id AS id, d.acl AS acl",
                   c=f"confluence:{space_key}").records
    wanted = {r["id"]: pages.get(r["id"], space) for r in nodes}
    written = 0 if dry_run else _write(w, nodes, wanted, sections=True)
    return {"space": space_key, "acl": space, "restricted": sum(1 for v in wanted.values() if v != space),
            "documents": len(nodes), "written": written, **({"acls": wanted} if dry_run else {})}


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jira", action="append", default=[], metavar="PROJECT")
    ap.add_argument("--confluence", action="append", default=[], metavar="SPACE")
    ap.add_argument("--dry-run", action="store_true", help="print the ACLs, write nothing")
    args = ap.parse_args(argv)
    if not args.jira and not args.confluence:
        ap.error("give --jira and/or --confluence")
    out = {}
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        if args.jira:
            api = Atlassian()
            out["jira"] = [sync_jira(w, api, p, args.dry_run) for p in args.jira]
        if args.confluence:
            api = Atlassian(prefix="CONFLUENCE")
            out["confluence"] = [sync_confluence(w, api, s, args.dry_run) for s in args.confluence]
    print(json.dumps(out, indent=2))
