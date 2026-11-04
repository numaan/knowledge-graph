"""People: one :Person node per human, merged from every source that names them.

    kg-people                                   # rebuild people from the graph; writes only changes
    kg-people --aliases people-aliases.yaml     # with explicit merges (env PEOPLE_ALIASES)

Each source stores identities on its own nodes; this stage reads them back and merges them:

| Source              | Node and properties                                                  | Identity keys        |
|---------------------|----------------------------------------------------------------------|----------------------|
| git history         | Commit.author, author_email_hash, author_github (noreply address)    | email, github        |
| Jira                | Ticket.assignee_id / _name / _email_hash, reporter_*                 | atlassian, email     |
| Confluence          | Document.author_id                                                   | atlassian            |
| GitHub              | PullRequest.author, commit_identities, Review(Comment).author        | github, email        |
| Backstage catalog   | CatalogUser (entity ref, email_hash, github annotation)              | catalog, email, github |

Merge rules, in this order. A merge needs confidence >= MERGE_CONFIDENCE (0.9):
1. The same strong key: hashed email, Atlassian account id (Jira and Confluence share it), GitHub
   login or catalog entity ref (1.0). A GitHub commit that carries both a login and an email joins them.
2. An entry of the alias file (people-aliases.yaml) joins all of its keys and names (1.0).
3. The same full name without a shared key is NAME_ONLY_CONFIDENCE (0.6), below the merge threshold:
   it never merges. It becomes a MAYBE_SAME edge for a human to confirm in the alias file.

Emails are never stored: only EMAIL_SALT-salted SHA-256 hashes, and :Person has no embedded text.

Graph:
    (:Source:Person {name, github, atlassian, catalog, names, email_hashes, acl})
        -[:AUTHORED]-> (:Commit | :PullRequest | :Review | :ReviewComment | :Document)
        -[:REVIEWED {state}]-> (:PullRequest)
        -[:ASSIGNED]-> (:Ticket)    -[:REPORTED]-> (:Ticket)
        -[:MEMBER_OF]-> (:Team)     -[:OWNS]-> (:Service | :API | :System | :File)
        -[:MAYBE_SAME {confidence}]-> (:Person)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from .graph.neo4j_writer import Neo4jWriter

log = logging.getLogger("kg_pipeline.people")

MERGE_CONFIDENCE = 0.9
NAME_ONLY_CONFIDENCE = 0.6
EMAIL_SALT = os.environ.get("PEOPLE_EMAIL_SALT", "kg-people-v1")
NOREPLY_RE = re.compile(r"^(?:\d+\+)?([A-Za-z0-9-]+)@users\.noreply\.github\.com$", re.I)
BOTS = re.compile(r"(\[bot\]$|^dependabot|^renovate|^github-actions)", re.I)
EDGE_TYPES = ("AUTHORED", "REVIEWED", "ASSIGNED", "REPORTED", "MEMBER_OF", "OWNS", "MAYBE_SAME")


# ---- identity helpers, used by the sources when they write their nodes --------------------------------
def email_hash(email: str | None) -> str | None:
    """Salted hash of a normalised email; the graph never holds the address itself."""
    if not email or "@" not in email:
        return None
    return hashlib.sha256(f"{EMAIL_SALT}|{email.strip().lower()}".encode()).hexdigest()[:24]


def git_identity(name: str, email: str) -> dict:
    """Commit author properties: the hash of the email, and the login of a GitHub noreply address."""
    m = NOREPLY_RE.match((email or "").strip())
    return {"author_email_hash": email_hash(email), "author_github": m.group(1).lower() if m else None}


def jira_identity(fields: dict) -> dict:
    """Ticket properties for the assignee and reporter (Atlassian account id, display name, email hash)."""
    out = {}
    for role in ("assignee", "reporter"):
        user = fields.get(role) or {}
        out[f"{role}_id"] = user.get("accountId")
        out[f"{role}_name"] = user.get("displayName")
        out[f"{role}_email_hash"] = email_hash(user.get("emailAddress"))
    return out


def norm_name(name: str | None) -> str:
    return " ".join(re.sub(r"[^\w\s'-]", " ", name or "").casefold().split())


# ---- merge ---------------------------------------------------------------------------------------------
class _Union:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def load_aliases(path: str | Path | None) -> list[dict]:
    """people-aliases.yaml: a list under `people:`; see docs/streams/people.md for the format."""
    if not path or not Path(path).exists():
        return []
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return list(data.get("people") or [])


def _alias_keys(entry: dict) -> tuple[set[str], set[str]]:
    keys = {f"email:{h}" for e in entry.get("emails") or [] if (h := email_hash(e))}
    keys |= {f"email:{h}" for h in entry.get("email_hashes") or []}
    keys |= {f"github:{g.lower().lstrip('@')}" for g in entry.get("github") or []}
    keys |= {f"atlassian:{a}" for a in entry.get("atlassian") or []}
    keys |= {f"catalog:{c.lower()}" for c in entry.get("catalog") or []}
    names = {norm_name(n) for n in [entry.get("name"), *(entry.get("names") or [])] if n}
    return keys, names


def resolve(mentions: list[dict], aliases: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Merge identity mentions into people.

    A mention is {keys: [strong keys], name, acl, edge: (TYPE, dst id, dst anchor, props) | None}.
    Returns (person rows, edge rows {type, src, dst, dst_anchor, ...props}).
    """
    uf = _Union()
    for m in mentions:
        keys = m["keys"]
        for k in keys:
            uf.find(k)
        for k in keys[1:]:
            uf.union(keys[0], k)
    # Alias entries join their keys, and any cluster whose names include one of the entry's names.
    names_of: dict[str, set[str]] = defaultdict(set)
    for m in mentions:
        if m["keys"] and m.get("name"):
            names_of[uf.find(m["keys"][0])].add(norm_name(m["name"]))
    alias_root: dict[str, dict] = {}
    for i, entry in enumerate(aliases or []):
        keys, names = _alias_keys(entry)
        anchor = f"alias:{entry.get('id') or i}"
        uf.find(anchor)
        for k in keys:
            uf.union(anchor, k)
        for root, ns in list(names_of.items()):
            if ns & names:
                uf.union(anchor, root)
        alias_root[anchor] = entry
    alias_of = {uf.find(a): e for a, e in alias_root.items()}

    clusters: dict[str, list[dict]] = defaultdict(list)
    for m in mentions:
        if m["keys"]:
            clusters[uf.find(m["keys"][0])].append(m)
    members: dict[str, set[str]] = defaultdict(set)
    for k in list(uf.parent):
        members[uf.find(k)].add(k)

    people, edges, pid_of = [], [], {}
    for root, ms in clusters.items():
        keys = sorted(k for k in members[root] if not k.startswith("alias:"))
        alias = alias_of.get(root)
        pid = f"person:{alias['id']}" if alias and alias.get("id") else \
            "person:" + hashlib.sha256(keys[0].encode()).hexdigest()[:16]
        pid_of[root] = pid
        names = Counter(m["name"] for m in ms if m.get("name"))
        catalog_name = next((m["name"] for m in ms if m.get("display") and m.get("name")), None)
        name = (alias or {}).get("name") or catalog_name or (names.most_common(1)[0][0] if names else keys[0].split(":", 1)[1])
        pick = lambda prefix: sorted(k.split(":", 1)[1] for k in keys if k.startswith(prefix + ":"))
        people.append({
            "id": pid, "name": name, "title": name, "names": sorted(names), "github": pick("github"),
            "atlassian": pick("atlassian"), "catalog": pick("catalog"), "email_hashes": pick("email"),
            "systems": sorted({m["system"] for m in ms}), "bot": any(BOTS.search(n) for n in [name, *pick("github")]),
            "acl": sorted({g for m in ms for g in m.get("acl") or []}), "source": "people",
            "merge_rule": "alias" if alias else "key", "merge_confidence": 1.0,
        })
        seen = set()
        for m in ms:
            if not m.get("edge"):
                continue
            etype, dst, anchor, props = m["edge"]
            if (etype, dst) in seen:
                continue
            seen.add((etype, dst))
            edges.append({"type": etype, "src": pid, "dst": dst, "dst_anchor": anchor, "method": "identity",
                          "confidence": 1.0, **props})

    # Rule 3: the same full name in two clusters is a hint, never a merge.
    by_name: dict[str, list[str]] = defaultdict(list)
    for p in people:
        for n in {norm_name(x) for x in [p["name"], *p["names"]]}:
            if len(n.split()) >= 2:
                by_name[n].append(p["id"])
    pairs = set()
    for n, ids in by_name.items():
        for a in sorted(set(ids)):
            for b in sorted(set(ids)):
                if a < b and (a, b) not in pairs:
                    pairs.add((a, b))
                    edges.append({"type": "MAYBE_SAME", "src": a, "dst": b, "dst_anchor": "Source", "method": "name",
                                  "confidence": NAME_ONLY_CONFIDENCE, "name": n})
    assert NAME_ONLY_CONFIDENCE < MERGE_CONFIDENCE  # name-only matches must never merge
    return people, edges


# ---- read identities from the graph -------------------------------------------------------------------
def _keys(*pairs) -> list[str]:
    return [f"{kind}:{value}" for kind, value in pairs if value]


def mentions_from_graph(w: Neo4jWriter) -> list[dict]:
    rd = lambda q: [r.data() for r in w._run(q).records]
    out: list[dict] = []
    for r in rd("MATCH (c:Commit) RETURN c.id AS id, c.author AS name, c.author_email_hash AS email, "
                "c.author_github AS github, c.acl AS acl"):
        # A commit without an email has no strong key; it is skipped, never merged by name.
        out.append({"keys": _keys(("email", r["email"]), ("github", r["github"])), "name": r["name"], "acl": r["acl"], "system": "git", "edge": ("AUTHORED", r["id"], "Source", {})})
    for r in rd("MATCH (t:Ticket) RETURN t.id AS id, t.acl AS acl, t.assignee_id AS a_id, t.assignee_name AS a_name, "
                "t.assignee_email_hash AS a_email, t.reporter_id AS r_id, t.reporter_name AS r_name, "
                "t.reporter_email_hash AS r_email"):
        for role, etype in (("a", "ASSIGNED"), ("r", "REPORTED")):
            keys = _keys(("atlassian", r[f"{role}_id"]), ("email", r[f"{role}_email"]))
            if keys:
                out.append({"keys": keys, "name": r[f"{role}_name"], "acl": r["acl"], "system": "jira",
                            "edge": (etype, r["id"], "Source", {})})
    for r in rd("MATCH (d:Document) WHERE d.author_id IS NOT NULL RETURN d.id AS id, d.author_id AS a, d.acl AS acl"):
        out.append({"keys": _keys(("atlassian", r["a"])), "name": None, "acl": r["acl"], "system": "confluence",
                    "edge": ("AUTHORED", r["id"], "Source", {})})
    for r in rd("MATCH (p:PullRequest) RETURN p.id AS id, p.author AS login, p.commit_identities AS ids, p.acl AS acl"):
        if r["login"]:
            out.append({"keys": _keys(("github", r["login"].lower())), "name": r["login"], "acl": r["acl"],
                        "system": "github", "edge": ("AUTHORED", r["id"], "Source", {})})
        for ident in r["ids"] or []:  # "login|email_hash|name" from the PR's commits: GitHub joins them
            login, h, name = (ident.split("|") + ["", "", ""])[:3]
            keys = _keys(("github", login.lower()), ("email", h))
            if keys:
                out.append({"keys": keys, "name": name or None, "acl": r["acl"], "system": "github", "edge": None})
    for r in rd("MATCH (x:Source)-[:PART_OF]->(p:PullRequest) WHERE (x:Review OR x:ReviewComment) AND x.author IS NOT NULL "
                "RETURN x.id AS id, x.author AS login, x.state AS state, p.id AS pr, p.author AS pr_author, x.acl AS acl, "
                "x:Review AS review"):
        k = _keys(("github", r["login"].lower()))
        out.append({"keys": k, "name": r["login"], "acl": r["acl"], "system": "github",
                    "edge": ("AUTHORED", r["id"], "Source", {})})
        if r["review"] and r["login"] != r["pr_author"]:
            out.append({"keys": k, "name": r["login"], "acl": r["acl"], "system": "github",
                        "edge": ("REVIEWED", r["pr"], "Source", {"state": r["state"]})})
    for r in rd("MATCH (u:CatalogUser) RETURN u.id AS id, u.ref AS ref, u.title AS name, u.email_hash AS email, "
                "u.github AS github, u.member_of AS teams, u.owns AS owns, u.acl AS acl"):
        keys = _keys(("catalog", (r["ref"] or "").lower()), ("email", r["email"]), ("github", (r["github"] or "").lower()))
        out.append({"keys": keys, "name": r["name"], "display": True, "acl": r["acl"], "system": "catalog", "edge": None})
        for team in r["teams"] or []:
            out.append({"keys": keys, "name": r["name"], "acl": r["acl"], "system": "catalog",
                        "edge": ("MEMBER_OF", team, "Source", {"method_detail": "catalog"})})
        for owned in r["owns"] or []:
            out.append({"keys": keys, "name": r["name"], "acl": r["acl"], "system": "catalog",
                        "edge": ("OWNS", owned, "Source", {"via": "catalog"})})
    # CODEOWNERS entries that name a person (@login or an email); teams are linked by the catalog stage.
    from .sources.catalog import codeowner_files
    for owner, files, acl in codeowner_files(w):
        key = _keys(("email", email_hash(owner))) if "@" in owner.strip("@") else _keys(("github", owner.lstrip("@").lower()))
        for f, pattern in files:
            out.append({"keys": key, "name": owner.lstrip("@") if "@" not in owner.strip("@") else None, "acl": acl,
                        "system": "codeowners", "edge": ("OWNS", f, "Code", {"via": "codeowners", "pattern": pattern})})
    return out


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


def sync(w: Neo4jWriter, aliases_path: str | Path | None = None) -> dict:
    """Rebuild people from the identities in the graph; write only people whose data or edges changed."""
    people, edges = resolve(mentions_from_graph(w), load_aliases(aliases_path))
    by_src: dict[str, list[dict]] = defaultdict(list)
    for e in edges:
        by_src[e["src"]].append(e)
    for p in people:
        p["content_hash"] = _hash(p, sorted((e["type"], e["dst"], e.get("state")) for e in by_src[p["id"]]))
    old = {r["id"]: r["h"] for r in w._run("MATCH (p:Person) RETURN p.id AS id, p.content_hash AS h").records}
    changed = [p for p in people if old.get(p["id"]) != p["content_hash"]]
    ids = [p["id"] for p in changed]
    w.upsert(("Person",), changed)
    changed_ids = set(ids)
    for etype in EDGE_TYPES:
        for anchor in ("Source", "Code"):
            rows = [{k: v for k, v in e.items() if k not in ("type", "dst_anchor")} for e in edges
                    if e["type"] == etype and e["dst_anchor"] == anchor and e["src"] in changed_ids]
            if rows or ids:
                w.link(etype, rows, dst=anchor, replace_from=ids)
    gone = [i for i in old if i not in {p["id"] for p in people}]
    if gone:
        w._run("UNWIND $ids AS id MATCH (p:Person {id: id}) DETACH DELETE p", ids=gone)
    return {"people": len(people), "written": len(changed), "removed": len(gone), "edges": len(edges),
            "maybe_same": sum(e["type"] == "MAYBE_SAME" for e in edges)}


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aliases", default=os.environ.get("PEOPLE_ALIASES", "people-aliases.yaml"))
    args = ap.parse_args(argv)
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        print(json.dumps(sync(w, args.aliases), indent=2))
