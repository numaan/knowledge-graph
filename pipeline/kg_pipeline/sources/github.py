"""GitHub pull requests, reviews and review comments into the graph.

    kg-github --repo acme/shop --name shop          # PRs updated since the last run
    kg-github --repo acme/shop --name shop --full   # all PRs (newest --max first)

Env: GITHUB_TOKEN (a fine-grained token with read access to pull requests and contents),
GITHUB_API_URL (default https://api.github.com; GitHub Enterprise: https://host/api/v3).
--name is the repository name in the graph (kg-ingest-code --name), so PRs join its files and commits.

Graph:
    (:Source:PullRequest {key, number, title, text, state, author, merged_at, url, base, head})
        -[:CONTAINS]-> (:Commit)                 by sha (the PR commits and the merge or squash commit)
        -[:REFERENCES]-> (:Ticket)               Jira keys in the title, the head branch or the body
        -[:MODIFIES {change, added, deleted}]-> (:File)
    (:Source:Review {author, state, text}) -[:PART_OF]-> (:PullRequest)
    (:Source:ReviewComment {author, text, path, line}) -[:PART_OF]-> (:PullRequest)
        -[:ON]-> (:File) and the smallest (:Symbol) whose current lines contain the line

Rate limits: a 403 or 429 with Retry-After, or with X-RateLimit-Remaining 0, waits and retries; a
response with Remaining 0 makes the next call wait until X-RateLimit-Reset.

Other forges: sync() reads normalised change dicts from a Forge (see GitHubForge.changes). A GitLab
client needs only a class with the same changes(since) method that returns the same dict shape.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator, Protocol

from ..graph.neo4j_writer import Neo4jWriter
from ..people import email_hash
from .history import KEY_RE

log = logging.getLogger("kg_pipeline.github")

Fetch = Callable[[str, dict], tuple[int, dict, Any]]   # (url, headers) -> (status, headers, JSON); tests pass a fake
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
TEXT_CHARS = 8_000
MAX_WAIT = 900   # seconds; a longer rate-limit wait fails the run (the next run continues from the cursor)


class GitHub:
    """REST client: token auth, pagination by Link header, waits on primary and secondary rate limits."""

    def __init__(self, token: str | None = None, base_url: str | None = None, fetch: Fetch | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.base = (base_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        token = token or os.environ.get("GITHUB_TOKEN")
        self._headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                         "User-Agent": "kg-pipeline", **({"Authorization": f"Bearer {token}"} if token else {})}
        self._fetch, self._sleep, self._clock = fetch or self._http, sleep, clock
        self._resume_at = 0.0
        self.calls = 0

    @staticmethod
    def _http(url: str, headers: dict) -> tuple[int, dict, Any]:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, None

    def _wait(self, headers: dict, attempt: int) -> float:
        if headers.get("retry-after"):
            return float(headers["retry-after"])
        if headers.get("x-ratelimit-remaining") == "0" and headers.get("x-ratelimit-reset"):
            return max(1.0, float(headers["x-ratelimit-reset"]) - self._clock())
        return 2.0 ** attempt

    def request(self, url: str, retries: int = 5) -> tuple[Any, dict]:
        for attempt in range(retries):
            if (pause := self._resume_at - self._clock()) > 0:
                self._sleep(min(pause, MAX_WAIT))
            self.calls += 1
            try:
                status, headers, body = self._fetch(url, self._headers)
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"GET {url.split('?')[0]} failed: {e}") from e
                self._sleep(2.0 ** attempt)
                continue
            if status == 200:
                if headers.get("x-ratelimit-remaining") == "0" and headers.get("x-ratelimit-reset"):
                    self._resume_at = float(headers["x-ratelimit-reset"])
                return body, headers
            limited = status == 429 or (status == 403 and (headers.get("retry-after")
                                                           or headers.get("x-ratelimit-remaining") == "0"))
            if not (limited or status >= 500) or attempt == retries - 1:
                raise RuntimeError(f"GET {url.split('?')[0]} failed: HTTP {status}")
            wait = self._wait(headers, attempt)
            if wait > MAX_WAIT:
                raise RuntimeError(f"GitHub rate limit: reset in {wait:.0f} s; run again later")
            log.info("GitHub rate limit or error %s; waiting %.0f s", status, wait)
            self._sleep(wait)
        raise AssertionError("unreachable")

    def pages(self, path: str, **params) -> Iterator[dict]:
        url = f"{self.base}{path}?" + urllib.parse.urlencode({"per_page": 100, **params})
        while url:
            body, headers = self.request(url)
            yield from body or []
            m = LINK_RE.search(headers.get("link", ""))
            url = m.group(1) if m else None


class Forge(Protocol):
    """A source of pull or merge requests in the normalised shape of GitHubForge.changes."""

    def changes(self, since: str | None, limit: int) -> Iterator[dict]: ...


class GitHubForge:
    def __init__(self, api: GitHub, repo: str):
        if not REPO_RE.match(repo):
            raise ValueError(f"invalid GitHub repository {repo!r}; use owner/name")
        self.api, self.repo = api, repo

    def changes(self, since: str | None, limit: int = 1000) -> Iterator[dict]:
        """PRs updated after `since` (ISO time), newest first, with commits, files, reviews and comments."""
        base = f"/repos/{self.repo}/pulls"
        for n, pr in enumerate(self.api.pages(base, state="all", sort="updated", direction="desc")):
            if n >= limit or (since and pr["updated_at"] <= since):
                return
            num = pr["number"]
            yield {
                "number": num, "title": pr.get("title") or "", "body": pr.get("body") or "",
                "state": "merged" if pr.get("merged_at") else pr.get("state"), "draft": pr.get("draft", False),
                "author": (pr.get("user") or {}).get("login"), "url": pr.get("html_url"),
                "created": pr.get("created_at"), "updated": pr["updated_at"], "merged_at": pr.get("merged_at"),
                "closed_at": pr.get("closed_at"), "base": (pr.get("base") or {}).get("ref"),
                "head": (pr.get("head") or {}).get("ref"), "merge_sha": pr.get("merge_commit_sha") if pr.get("merged_at") else None,
                "labels": [l["name"] for l in pr.get("labels") or []],
                "commits": [{"sha": c["sha"], "login": (c.get("author") or {}).get("login"),
                             "name": ((c.get("commit") or {}).get("author") or {}).get("name"),
                             "email": ((c.get("commit") or {}).get("author") or {}).get("email")}
                            for c in self.api.pages(f"{base}/{num}/commits")],
                "files": [{"path": f["filename"], "status": f.get("status"), "added": f.get("additions", 0),
                           "deleted": f.get("deletions", 0), "previous": f.get("previous_filename")}
                          for f in self.api.pages(f"{base}/{num}/files")],
                "reviews": [{"id": r["id"], "author": (r.get("user") or {}).get("login"), "state": r.get("state"),
                             "body": r.get("body") or "", "submitted_at": r.get("submitted_at"), "url": r.get("html_url")}
                            for r in self.api.pages(f"{base}/{num}/reviews")],
                "comments": [{"id": c["id"], "review_id": c.get("pull_request_review_id"),
                              "author": (c.get("user") or {}).get("login"), "body": c.get("body") or "",
                              "path": c.get("path"), "line": c.get("line") or c.get("original_line"),
                              "outdated": c.get("line") is None, "created": c.get("created_at"), "url": c.get("html_url")}
                             for c in self.api.pages(f"{base}/{num}/comments")],
            }


# ---- rows ----------------------------------------------------------------------------------------------
_CHANGE = {"added": "A", "removed": "D", "renamed": "R", "modified": "M", "changed": "M", "copied": "A"}


def rows(change: dict, slug: str, name: str) -> dict[str, list[dict]]:
    """Nodes and edges of one normalised change. slug is owner/repo, name the repository in the graph."""
    pid = f"github:{slug}#{change['number']}"
    acl, key = [f"repo:{name}"], f"{slug}#{change['number']}"
    keys = sorted(set(KEY_RE.findall("\n".join([change["title"], change["head"] or "", change["body"]]))))
    shas = sorted({c["sha"] for c in change["commits"]} | ({change["merge_sha"]} if change["merge_sha"] else set()))
    pr = {"id": pid, "key": key, "number": change["number"], "repo": name, "github_repo": slug, "type": "Pull request",
          "title": change["title"], "text": change["body"][:TEXT_CHARS], "state": change["state"],
          "draft": change["draft"], "author": change["author"], "url": change["url"], "created": change["created"],
          "updated": change["updated"], "merged_at": change["merged_at"], "closed_at": change["closed_at"],
          "base": change["base"], "head": change["head"], "labels": change["labels"], "ticket_keys": keys,
          "commit_shas": shas, "files_changed": len(change["files"]), "acl": acl, "source": "github",
          # login|email hash|name per commit author: GitHub itself joins the login to the git email
          "commit_identities": sorted({f"{c['login'] or ''}|{email_hash(c['email']) or ''}|{c['name'] or ''}"
                                       for c in change["commits"] if c["login"] or c["email"]})}
    reviews = [{"id": f"{pid}/review/{r['id']}", "key": key, "type": "Review", "repo": name, "author": r["author"],
                "state": r["state"], "title": f"Review {r['state'].lower() if r['state'] else ''} by {r['author']} on {key}: {change['title']}",
                "text": r["body"][:TEXT_CHARS], "date": r["submitted_at"], "url": r["url"], "acl": acl, "source": "github"}
               for r in change["reviews"]]
    comments = [{"id": f"{pid}/comment/{c['id']}", "key": key, "type": "Review comment", "repo": name,
                 "author": c["author"], "review_id": f"{pid}/review/{c['review_id']}" if c["review_id"] else None,
                 "title": f"Review comment by {c['author']} on {c['path']}:{c['line'] or ''} ({key})",
                 "text": c["body"][:TEXT_CHARS], "path": c["path"], "line": c["line"], "outdated": c["outdated"],
                 "date": c["created"], "url": c["url"], "acl": acl, "source": "github"} for c in change["comments"]]
    modifies = [{"src": pid, "dst": f"{name}:{f['path']}", "change": _CHANGE.get(f["status"], "M"), "added": f["added"],
                 "deleted": f["deleted"], "method": "github", "confidence": 1.0} for f in change["files"]]
    part_of = [{"src": r["id"], "dst": pid, "method": "github", "confidence": 1.0} for r in reviews + comments]
    return {"pr": [pr], "reviews": reviews, "comments": comments, "MODIFIES": modifies, "PART_OF": part_of}


def comment_targets(comments: list[dict], symbols: dict[str, list[dict]], name: str) -> list[dict]:
    """ON edges: comment -> File (1.0) and -> the smallest current symbol that contains its line (0.7).

    The line is from the PR head; later commits may have moved the code, so the symbol edge is a hint.
    """
    out = []
    for c in comments:
        if not c["path"]:
            continue
        out.append({"src": c["id"], "dst": f"{name}:{c['path']}", "method": "github", "confidence": 1.0})
        hits = [s for s in symbols.get(c["path"], []) if c["line"] and s["start"] <= c["line"] <= s["end"]]
        if hits and not c["outdated"]:
            best = min(hits, key=lambda s: s["end"] - s["start"])
            out.append({"src": c["id"], "dst": best["id"], "method": "github-line", "confidence": 0.7})
    return out


def link_prs(w: Neo4jWriter, pr_ids: list[str] | None = None) -> dict:
    """PR -> Commit by sha and PR -> Ticket by key. Run again after kg-git-history and kg-jira."""
    where = "WHERE p.id IN $ids" if pr_ids is not None else ""
    contains = w._run(f"""
        MATCH (p:PullRequest) {where} UNWIND p.commit_shas AS sha
        MATCH (c:Commit {{repo: p.repo, sha: sha}})
        MERGE (p)-[r:CONTAINS]->(c) SET r.method = 'sha', r.confidence = 1.0 RETURN count(*) AS n""",
                      ids=pr_ids or []).records[0]["n"]
    refs = w._run(f"""
        MATCH (p:PullRequest) {where} UNWIND p.ticket_keys AS key
        MATCH (t:Ticket {{key: key}})
        MERGE (p)-[r:REFERENCES]->(t) SET r.method = 'key', r.confidence = 1.0 RETURN count(*) AS n""",
                  ids=pr_ids or []).records[0]["n"]
    return {"contains": contains, "references": refs}


def sync(w: Neo4jWriter, forge: Forge, slug: str, name: str, full: bool = False, limit: int = 1000) -> dict:
    cursor = f"github:{slug}:updated"
    since = None if full else w.state(cursor)
    out: dict[str, list[dict]] = {"pr": [], "reviews": [], "comments": [], "MODIFIES": [], "PART_OF": []}
    for change in forge.changes(since, limit):
        for k, v in rows(change, slug, name).items():
            out[k] += v
    if not out["pr"]:
        return {"repo": slug, "pull_requests": 0, "since": since}
    pr_ids = [p["id"] for p in out["pr"]]
    # A PR's reviews and comments are rewritten with it; drop the ones GitHub no longer returns.
    w._run("MATCH (x:Source)-[:PART_OF]->(p:PullRequest) WHERE p.id IN $prs AND (x:Review OR x:ReviewComment) "
           "AND NOT x.id IN $keep DETACH DELETE x", prs=pr_ids, keep=[r["id"] for r in out["reviews"] + out["comments"]])
    w.upsert(("PullRequest",), out["pr"])
    w.upsert(("Review",), out["reviews"])
    w.upsert(("ReviewComment",), out["comments"])
    w.link("PART_OF", out["PART_OF"], replace_from=[r["id"] for r in out["reviews"] + out["comments"]])
    w.link("MODIFIES", out["MODIFIES"], dst="Code", replace_from=pr_ids)
    paths = sorted({c["path"] for c in out["comments"] if c["path"]})
    syms = w._run("MATCH (s:Symbol {repo: $repo}) WHERE s.path IN $paths RETURN s.id AS id, s.path AS path, "
                  "s.start_line AS start, s.end_line AS end", repo=name, paths=paths).records
    by_path: dict[str, list[dict]] = {}
    for r in syms:
        by_path.setdefault(r["path"], []).append(r.data())
    on = comment_targets(out["comments"], by_path, name)
    w.link("ON", on, dst="Code", replace_from=[c["id"] for c in out["comments"]])
    links = link_prs(w)  # all PRs: commits and tickets may have arrived after the PR
    w.state(cursor, max(p["updated"] for p in out["pr"]))
    return {"repo": slug, "pull_requests": len(pr_ids), "reviews": len(out["reviews"]), "comments": len(out["comments"]),
            "files": len(out["MODIFIES"]), "on": len(on), **links, "since": since}


def repositories(spec: str, git_urls: list[str] | None = None) -> dict[str, str]:
    """{owner/repo: graph name} from GITHUB_REPOSITORIES ("acme/shop=shop,acme/api") or github.com git URLs."""
    out = {}
    for item in [s.strip() for s in spec.split(",") if s.strip()]:
        slug, _, name = item.partition("=")
        out[slug] = name or slug.split("/")[1]
    for url in git_urls or []:
        m = re.match(r"^(?:https://|git@)github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", url)
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(1).split("/")[1]
    return out


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="GitHub repository owner/name")
    ap.add_argument("--name", help="repository name in the graph (default: the GitHub name)")
    ap.add_argument("--full", action="store_true", help="ignore the cursor")
    ap.add_argument("--max", type=int, default=1000, help="maximum pull requests per run")
    args = ap.parse_args(argv)
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        forge = GitHubForge(GitHub(), args.repo)
        print(json.dumps(sync(w, forge, args.repo, args.name or args.repo.split("/")[1], args.full, args.max), indent=2))
