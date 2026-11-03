"""Git history into the graph: commits, the files and symbols they changed, and the tickets they name.

    kg-git-history --repo /path/to/checkout --name precept                # new commits since the last run
    kg-git-history --repo /path/to/checkout --name precept --max 5000 --full
    kg-git-history --repo /path/to/checkout --name precept --reconcile    # drop commits of rewritten history

Run it after kg-ingest-code: symbols are matched against the current code graph.

Graph:
    (:Source:Commit {sha, title, text, author, date})
        -[:MODIFIES {change, added, deleted, confidence}]-> (:File) and (:Symbol)
        -[:REFERENCES]-> (:Ticket)       keys such as PREC-123 in the message
        -[:REFERENCES]-> (:Document)     feature ids such as P14-02 that start a document name

Symbol matching: the newest commit of a file maps its hunks to the current line ranges (exact,
confidence 0.9). Older commits use the function name that git prints in each hunk header (0.6),
because their line numbers no longer match the current code.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from ..graph.neo4j_writer import Neo4jWriter
from ..provenance import commit_url, git_remote, web_remote

log = logging.getLogger("kg_pipeline.history")

KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{0,9}-\d{1,6})\b")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
WORD_RE = re.compile(r"[A-Za-z_]\w*")
TEXT_CHARS = 4_000
SEP, END = "\x1f", "\x1e"


def git_log(repo: Path, since: str | None, max_commits: int) -> str:
    rng = [f"{since}..HEAD"] if since else []
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotepath=off", "log", "--no-merges", "--no-color", "--no-ext-diff",
         "-U0", "-p", f"--max-count={max_commits}", f"--format={END}%H{SEP}%an{SEP}%aI{SEP}%s{SEP}%b{SEP}", *rng, "--"],
        capture_output=True, check=True, text=True, encoding="utf-8", errors="replace").stdout


def parse(log_text: str) -> list[dict]:
    """Commits, newest first, each with files: {path: {change, added, deleted, hunks: [(start, end, header)]}}."""
    commits = []
    for block in log_text.split(END)[1:]:
        sha, author, date, title, body, diff = (block.split(SEP, 5) + [""] * 6)[:6]
        files: dict[str, dict] = {}
        cur = None
        change = "M"
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                change, cur = "M", None
            elif line.startswith("new file mode"):
                change = "A"
            elif line.startswith("deleted file mode"):
                change = "D"
            elif line.startswith("rename from "):
                change = "R"
            elif line.startswith("+++ "):
                path = line[4:]
                if path == "/dev/null":
                    continue
                cur = files.setdefault(path[2:] if path.startswith("b/") else path,
                                       {"change": change, "added": 0, "deleted": 0, "hunks": []})
            elif line.startswith("--- ") and change == "D":
                path = line[4:]
                cur = files.setdefault(path[2:] if path.startswith("a/") else path,
                                       {"change": "D", "added": 0, "deleted": 0, "hunks": []})
            elif cur is not None and (m := HUNK_RE.match(line)):
                start, count = int(m.group(1)), int(m.group(2) or 1)
                cur["hunks"].append((start, start + max(count, 1) - 1, m.group(3)))
            elif cur is not None and line.startswith("+"):
                cur["added"] += 1
            elif cur is not None and line.startswith("-"):
                cur["deleted"] += 1
        text = (title + "\n\n" + body).strip()
        commits.append({"sha": sha.strip(), "author": author, "date": date, "title": title, "text": text[:TEXT_CHARS],
                        "keys": sorted(set(KEY_RE.findall(text))), "files": files})
    return commits


def map_symbols(commits: list[dict], symbols: dict[str, list[dict]]) -> list[dict]:
    """MODIFIES rows from commits (newest first) to symbols of the current graph."""
    seen_file: set[str] = set()
    rows = []
    for c in commits:
        for path, f in c["files"].items():
            syms = symbols.get(path, [])
            exact = path not in seen_file and f["change"] != "D"
            seen_file.add(path)
            hit: dict[str, float] = {}
            for start, end, header in f["hunks"]:
                if exact:
                    for s in syms:
                        if s["start"] <= end and start <= s["end"]:
                            hit[s["id"]] = 0.9
                words = set(WORD_RE.findall(header))
                for s in syms:
                    if s["name"] in words and s["id"] not in hit:
                        hit[s["id"]] = 0.6
            rows += [{"sha": c["sha"], "dst": sid, "confidence": conf} for sid, conf in hit.items()]
    return rows


def _by_path(rows: list[dict]) -> dict[str, list[dict]]:
    by_path = defaultdict(list)
    for r in rows:
        by_path[r["path"]].append(r)
    return by_path


def sync(w: Neo4jWriter, repo_dir: Path, name: str, full: bool = False, max_commits: int = 2000,
         remote: str | None = None) -> dict:
    """remote: clone URL for commit links (default: origin of repo_dir); credentials in it are dropped."""
    cursor = f"git:{name}:head"
    since = None if full else w.state(cursor)
    try:
        commits = parse(git_log(repo_dir, since, max_commits))
    except subprocess.CalledProcessError:
        if not since:
            raise
        log.warning("cursor %s is not in the history (rewritten?); reading the last %d commits", since, max_commits)
        commits, since = parse(git_log(repo_dir, None, max_commits)), None
    if not commits:
        return {"repo": name, "commits": 0, "since": since}
    syms = w._run("MATCH (s:Symbol {repo: $repo}) RETURN s.id AS id, s.name AS name, s.path AS path, "
                  "s.start_line AS start, s.end_line AS end", repo=name).records
    symbols = _by_path([r.data() for r in syms])
    cid = lambda sha: f"git:{name}:{sha}"
    web = web_remote(remote) if remote else git_remote(repo_dir)
    w.upsert(("Commit",), [{"id": cid(c["sha"]), "sha": c["sha"], "repo": name, "author": c["author"], "date": c["date"],
                            "title": c["title"], "text": c["text"], "ticket_keys": c["keys"],
                            "files_changed": len(c["files"]), "acl": [f"repo:{name}"], "source": "git",
                            "url": commit_url(web, c["sha"]), "source_updated_at": c["date"]} for c in commits])
    ids = [cid(c["sha"]) for c in commits]
    file_rows = [{"src": cid(c["sha"]), "dst": f"{name}:{p}", "change": f["change"], "added": f["added"],
                  "deleted": f["deleted"], "method": "git", "confidence": 1.0}
                 for c in commits for p, f in c["files"].items()]
    sym_rows = [{"src": cid(r["sha"]), "dst": r["dst"], "method": "git", "confidence": r["confidence"]}
                for r in map_symbols(commits, symbols)]
    w.link("MODIFIES", file_rows + sym_rows, dst="Code", replace_from=ids)
    refs = link_references(w, ids)
    w.state(cursor, commits[0]["sha"])
    return {"repo": name, "commits": len(commits), "file_changes": len(file_rows), "symbol_changes": len(sym_rows),
            "references": refs, "since": since, "head": commits[0]["sha"]}


def link_references(w: Neo4jWriter, commit_ids: list[str] | None = None) -> int:
    """Commit -> Ticket by key and Commit -> Document by feature id. Also run after kg-jira and kg-docs."""
    where = "WHERE c.id IN $ids" if commit_ids is not None else ""
    n = w._run(f"""
        MATCH (c:Commit) {where} UNWIND c.ticket_keys AS key
        CALL (c, key) {{
          MATCH (t:Ticket {{key: key}}) MERGE (c)-[r:REFERENCES]->(t) SET r.method = 'key', r.confidence = 1.0 RETURN 1 AS x
          UNION
          MATCH (d:Document) WHERE d.name STARTS WITH key + '-' OR d.name STARTS WITH key + '_'
          MERGE (c)-[r:REFERENCES]->(d) SET r.method = 'key', r.confidence = 0.8 RETURN 1 AS x
        }} RETURN count(*) AS n""", ids=commit_ids or []).records
    return n[0]["n"] if n else 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, required=True, help="git checkout")
    ap.add_argument("--name", help="repository name in the graph (default: folder name)")
    ap.add_argument("--full", action="store_true", help="ignore the cursor and read the last --max commits")
    ap.add_argument("--max", type=int, default=2000)
    ap.add_argument("--reconcile", action="store_true", help="only remove commits that are no longer on HEAD")
    ap.add_argument("--force", action="store_true", help="with --reconcile: allow removing more than half")
    args = ap.parse_args(argv)
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        if args.reconcile:
            from .. import reconcile
            print(json.dumps(reconcile.git_history(w, args.repo, args.name or args.repo.resolve().name,
                                                   force=args.force), indent=2))
            return
        print(json.dumps(sync(w, args.repo, args.name or args.repo.resolve().name, args.full, args.max), indent=2))
