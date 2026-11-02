"""Jira Cloud stories, bugs, epics and releases into the graph.

    kg-jira --project PREC                    # incremental: issues updated since the last run
    kg-jira --project PREC --full             # all issues
    kg-jira --project PREC --jql "sprint in openSprints()"

Env: JIRA_BASE_URL (https://your-site.atlassian.net), JIRA_EMAIL, JIRA_API_TOKEN,
JIRA_ACCEPTANCE_FIELD (optional custom field id with acceptance criteria, e.g. customfield_10042).

Graph:
    (:Source:Ticket {key, type, status, title, text, ...})
        -[:CHILD_OF]-> (:Ticket)          parent or epic
        -[:LINKS {type}]-> (:Ticket)       issue links ("blocks", "relates to", ...)
        -[:IN_RELEASE]-> (:Source:Release) fix versions
        -[:MENTIONS]-> (:Code)             files and symbols named in the text (unique matches only)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re

from ..graph.neo4j_writer import Neo4jWriter
from .atlassian import Atlassian, adf_to_markdown

log = logging.getLogger("kg_pipeline.jira")

FIELDS = ["summary", "description", "issuetype", "status", "priority", "labels", "components", "fixVersions",
          "parent", "issuelinks", "created", "updated", "resolutiondate", "comment", "project"]
PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,19}$")
TEXT_CHARS = 12_000
COMMENTS = 10

PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z]{1,5})\b")
NAME_RE = re.compile(r"`([A-Za-z_][\w.]*)(?:\(\))?`|\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+(?:\.[a-z_]\w*)?)\b|\b([a-z]+_[a-z0-9_]+)\b")


def fetch_issues(api: Atlassian, jql: str, acceptance_field: str | None = None):
    fields = FIELDS + ([acceptance_field] if acceptance_field else [])
    token = None
    while True:
        page = api.get("/rest/api/3/search/jql", jql=jql, fields=",".join(fields), maxResults=100, nextPageToken=token)
        yield from page.get("issues", [])
        token = page.get("nextPageToken")
        if page.get("isLast", token is None) or not token:
            return


def fetch_releases(api: Atlassian, project: str) -> list[dict]:
    return api.get(f"/rest/api/3/project/{project}/versions")


def _text(value) -> str:
    if isinstance(value, dict) and value.get("type") == "doc":
        return adf_to_markdown(value).strip()
    return str(value or "").strip()


def ticket_row(api: Atlassian, issue: dict, acceptance_field: str | None) -> dict:
    f = issue["fields"]
    project = f["project"]["key"]
    parts = [_text(f.get("description"))]
    if acceptance_field and f.get(acceptance_field):
        parts.append("## Acceptance criteria\n\n" + _text(f[acceptance_field]))
    comments = (f.get("comment") or {}).get("comments", [])[-COMMENTS:]
    if comments:
        parts.append("## Comments\n\n" + "\n\n".join(_text(c.get("body")) for c in comments))
    status = f.get("status") or {}
    return {
        "id": f"jira:{issue['key']}", "key": issue["key"], "project": project, "title": f.get("summary", ""),
        "text": "\n\n".join(p for p in parts if p)[:TEXT_CHARS],
        "type": (f.get("issuetype") or {}).get("name"), "status": status.get("name"),
        "status_category": (status.get("statusCategory") or {}).get("key"),
        "priority": (f.get("priority") or {}).get("name"), "labels": f.get("labels") or [],
        "components": [c["name"] for c in f.get("components") or []],
        "created": f.get("created"), "updated": f.get("updated"), "resolved": f.get("resolutiondate"),
        "url": f"{api.base}/browse/{issue['key']}", "acl": [f"jira:{project}"], "source": "jira",
    }


def edges(issue: dict) -> dict[str, list[dict]]:
    f, src = issue["fields"], f"jira:{issue['key']}"
    out: dict[str, list[dict]] = {"CHILD_OF": [], "LINKS": [], "IN_RELEASE": []}
    if f.get("parent"):
        out["CHILD_OF"].append({"src": src, "dst": f"jira:{f['parent']['key']}"})
    for link in f.get("issuelinks") or []:
        if "outwardIssue" in link:  # each link appears on both issues; keep the outward side once
            out["LINKS"].append({"src": src, "dst": f"jira:{link['outwardIssue']['key']}", "type": link["type"]["outward"]})
    for v in f.get("fixVersions") or []:
        out["IN_RELEASE"].append({"src": src, "dst": f"jira:release:{f['project']['key']}:{v['id']}"})
    return out


def mentions(text: str) -> tuple[set[str], set[str]]:
    """(paths, names) that look like code in ticket text."""
    paths = set(PATH_RE.findall(text))
    names = {next(g for g in m if g) for m in NAME_RE.findall(text)}
    return paths, {n for n in names if len(n) >= 4}


def link_mentions(w: Neo4jWriter, rows: list[dict]) -> int:
    """Ticket -> File/Symbol for each path or name that matches exactly one code node."""
    links = []
    for r in rows:
        paths, names = mentions(r["title"] + "\n" + r["text"])
        if not paths and not names:
            continue
        found = w._run("""
            WITH $paths AS paths, $names AS names
            CALL (paths, names) {
              UNWIND paths AS p MATCH (f:File) WHERE f.path ENDS WITH p
              WITH p, collect(f.id) AS ids WHERE size(ids) = 1 RETURN ids[0] AS id, p AS term
              UNION
              UNWIND names AS n MATCH (s:Symbol) WHERE s.qualname = n OR (s.name = n AND NOT n CONTAINS '.')
              WITH n, collect(s.id) AS ids WHERE size(ids) = 1 RETURN ids[0] AS id, n AS term
            } RETURN id, term""", paths=sorted(paths), names=sorted(names)).records
        links += [{"src": r["id"], "dst": x["id"], "term": x["term"], "method": "text", "confidence": 0.6} for x in found]
    w.link("MENTIONS", links, dst="Code", replace_from=[r["id"] for r in rows])
    return len(links)


def sync(w: Neo4jWriter, api: Atlassian, project: str, jql: str | None = None, full: bool = False,
         acceptance_field: str | None = None) -> dict:
    if not PROJECT_RE.match(project):
        raise ValueError(f"invalid Jira project key {project!r}")
    cursor_key = f"jira:{project}:updated"
    since = None if full or jql else w.state(cursor_key)
    query = jql or f'project = "{project}"' + (f' AND updated >= "{since}"' if since else "") + " ORDER BY updated ASC"
    releases = [{"id": f"jira:release:{project}:{v['id']}", "project": project, "title": v["name"],
                 "name": v["name"], "released": v.get("released", False), "release_date": v.get("releaseDate"),
                 "description": v.get("description"), "acl": [f"jira:{project}"], "source": "jira"}
                for v in fetch_releases(api, project)]
    w.upsert(("Release",), releases)

    rows, links = [], {"CHILD_OF": [], "LINKS": [], "IN_RELEASE": []}
    for issue in fetch_issues(api, query, acceptance_field):
        rows.append(ticket_row(api, issue, acceptance_field))
        for k, v in edges(issue).items():
            links[k] += v
    w.upsert(("Ticket",), rows)
    ids = [r["id"] for r in rows]
    for etype, dst in (("CHILD_OF", "Source"), ("LINKS", "Source"), ("IN_RELEASE", "Source")):
        w.link(etype, links[etype], dst=dst, replace_from=ids)
    mentioned = link_mentions(w, rows)
    from .history import link_references
    link_references(w)  # commits that name these tickets, ingested before the tickets
    if rows and not jql:
        # Jira JQL takes minutes, not seconds; the >= keeps the last minute's issues in the next run
        w.state(cursor_key, max(r["updated"] for r in rows)[:16].replace("T", " "))
    return {"project": project, "tickets": len(rows), "releases": len(releases), "mentions": mentioned,
            **{k.lower(): len(v) for k, v in links.items()}, "since": since}


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--jql")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args(argv)
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        print(json.dumps(sync(w, Atlassian(), args.project, args.jql, args.full,
                              os.environ.get("JIRA_ACCEPTANCE_FIELD")), indent=2))
