"""Documents and wiki pages into the graph, split into sections.

    kg-docs --path /c/workspace/precept/docs --name precept-docs       # .md .txt .pdf .docx .pptx .html
    kg-docs --confluence ENG                                            # one Confluence Cloud space
    kg-docs --confluence ENG --reconcile                                # remove deleted, archived, moved pages

Markdown and text are read directly; PDF, DOCX, PPTX and HTML go through Docling Serve (DOCLING_URL).
Confluence env: CONFLUENCE_BASE_URL (https://your-site.atlassian.net), CONFLUENCE_EMAIL and
CONFLUENCE_API_TOKEN (or ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN).

A document is skipped when its content hash (files) or version (Confluence) did not change.

Graph:
    (:Source:Document {name, title, url, source, acl}) -[:CHILD_OF]-> (:Document)   page tree
    (:Source:Section {title, text, order}) -[:PART_OF]-> (:Document)
    (:Section) -[:MENTIONS]-> (:Code)        files and symbols named in the text
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from ..graph.neo4j_writer import Neo4jWriter
from ..provenance import file_meta, file_url
from .atlassian import Atlassian
from .jira import link_mentions

log = logging.getLogger("kg_pipeline.documents")

SUFFIXES = {".md", ".markdown", ".txt", ".pdf", ".docx", ".pptx", ".html", ".htm"}
SECTION_CHARS = 3_000   # small sections retrieve better than whole chapters


def split_sections(markdown: str) -> list[tuple[str, str]]:
    """(heading path, text) per heading; long sections are cut at paragraph breaks."""
    out, heading, buf = [], [], []

    def flush():
        text = "\n".join(buf).strip()
        while text:
            cut = len(text) if len(text) <= SECTION_CHARS else (text.rfind("\n\n", 0, SECTION_CHARS) + 1 or SECTION_CHARS)
            out.append((" > ".join(heading), text[:cut].strip()))
            text = text[cut:].strip()

    fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            buf = []
            heading = heading[:len(m.group(1)) - 1] + [m.group(2).strip()]
        buf.append(line)
    flush()
    return [(h, t) for h, t in out if len(t.split()) >= 5]


class _StorageToMarkdown(HTMLParser):
    """Confluence storage format (XHTML with ac: macros) to Markdown."""

    BLOCK = {"p": "\n\n", "div": "\n", "br": "\n", "tr": "\n", "table": "\n"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.lists: list[str] = []
        self.macros: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "script", "ac:parameter"):
            self.skip += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            self.lists.append(tag)
        elif tag == "li":
            self.out.append("\n" + "  " * (len(self.lists) - 1) + ("1. " if self.lists[-1:] == ["ol"] else "- "))
        elif tag == "code":
            self.out.append("`")
        elif tag == "ac:structured-macro":
            self.macros.append(a.get("ac:name", ""))
            if self.macros[-1] == "code":
                self.out.append("\n```\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "ac:parameter"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self.out.append("\n")
        elif tag == "code":
            self.out.append("`")
        elif tag == "ac:structured-macro" and self.macros:
            if self.macros.pop() == "code":
                self.out.append("\n```\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n")
        elif tag in self.BLOCK:
            self.out.append(self.BLOCK[tag])

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)

    def unknown_decl(self, data):  # <![CDATA[ ... ]]> holds the body of code macros
        if data.startswith("CDATA[") and not self.skip:
            self.out.append(data[6:])


def storage_to_markdown(html: str) -> str:
    p = _StorageToMarkdown()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip()


def _write_document(w: Neo4jWriter, doc: dict, markdown: str) -> int:
    sections = split_sections(markdown)
    w.upsert(("Document",), [doc])
    # upsert keeps an acl that kg-permissions wrote (page restriction); sections, new ones included, take
    # the document's stored acl so they are never wider than the page.
    stored = w._run("MATCH (d:Document {id: $id}) RETURN d.acl AS acl", id=doc["id"]).records
    acl = list(stored[0]["acl"]) if stored and stored[0]["acl"] is not None else doc["acl"]
    rows = [{"id": f"{doc['id']}#s{i}", "title": h or doc["title"], "text": t, "order": i, "acl": acl,
             "source": doc["source"], "url": doc.get("url"), "source_updated_at": doc.get("source_updated_at"),
             "author": doc.get("author")} for i, (h, t) in enumerate(sections)]
    w.upsert(("Section",), rows)
    w._run("MATCH (s:Section)-[:PART_OF]->(:Document {id: $doc}) WHERE NOT s.id IN $ids DETACH DELETE s",
           doc=doc["id"], ids=[r["id"] for r in rows])
    w.link("PART_OF", [{"src": r["id"], "dst": doc["id"]} for r in rows])
    link_mentions(w, rows)
    return len(rows)


def _unchanged(w: Neo4jWriter, doc_id: str, version: str) -> bool:
    r = w._run("MATCH (d:Document {id: $id}) RETURN d.version AS v", id=doc_id).records
    return bool(r) and r[0]["v"] == version


def sync_folder(w: Neo4jWriter, root: Path, name: str, read=None, url_base: str | None = None) -> dict:
    """url_base: browsable URL of root (e.g. https://github.com/org/repo/blob/main/docs); default file:// links."""
    from ..requirements import read_document  # Docling for binary formats
    from ..reconcile import folder_moves  # moved files keep their nodes and embeddings
    read = read or read_document
    seen, stats = [], {"documents": 0, "unchanged": 0, "sections": 0, "failed": 0,
                       "moved": len(folder_moves(w, root, name, url_base)["moved"])}
    for path in sorted(p for p in root.rglob("*") if p.suffix.lower() in SUFFIXES and p.is_file()):
        rel = path.relative_to(root).as_posix()
        doc_id = f"doc:{name}:{rel}"
        seen.append(doc_id)
        version = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        if _unchanged(w, doc_id, version):
            stats["unchanged"] += 1
            continue
        try:
            markdown = read(path)
        except Exception as e:  # one broken file must not stop the folder
            log.warning("cannot read %s: %s", rel, e)
            stats["failed"] += 1
            continue
        title = next((l.lstrip("# ").strip() for l in markdown.splitlines() if l.startswith("# ")), path.stem)
        doc = {"id": doc_id, "name": path.name, "title": title, "path": rel, "collection": name, "version": version,
               "format": path.suffix.lower().lstrip("."), "source": "files", "acl": [f"docs:{name}"],
               "url": file_url(path, rel, url_base), **file_meta(path)}
        stats["sections"] += _write_document(w, doc, markdown)
        stats["documents"] += 1
    removed = w._run("MATCH (d:Document {collection: $name, source: 'files'}) WHERE NOT d.id IN $ids "
                     "OPTIONAL MATCH (s:Section)-[:PART_OF]->(d) DETACH DELETE s, d RETURN count(DISTINCT d) AS n",
                     name=name, ids=seen).records[0]["n"]
    from .history import link_references
    return {**stats, "removed": removed, "references": link_references(w)}


def fetch_pages(api: Atlassian, space_key: str):
    spaces = api.get("/wiki/api/v2/spaces", keys=space_key).get("results", [])
    if not spaces:
        raise ValueError(f"Confluence space {space_key} not found or not visible")
    # status=current: the API also returns archived pages by default, and they must not be in the graph
    path, params = f"/wiki/api/v2/spaces/{spaces[0]['id']}/pages", {"body-format": "storage", "limit": 100, "status": "current"}
    while path:
        page = api.get(path, **params)
        yield from page.get("results", [])
        nxt = (page.get("_links") or {}).get("next")  # e.g. /wiki/api/v2/spaces/1/pages?cursor=...
        path, _, query = (nxt or "").partition("?")
        params = dict(urllib.parse.parse_qsl(query))


def sync_confluence(w: Neo4jWriter, api: Atlassian, space_key: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9~_]{1,255}", space_key):
        raise ValueError(f"invalid space key {space_key!r}")
    seen, parents, stats = [], [], {"documents": 0, "unchanged": 0, "sections": 0}
    for p in fetch_pages(api, space_key):
        doc_id = f"confluence:{space_key}:{p['id']}"
        seen.append(doc_id)
        if p.get("parentId"):
            parents.append({"src": doc_id, "dst": f"confluence:{space_key}:{p['parentId']}"})
        version = str((p.get("version") or {}).get("number", ""))
        if _unchanged(w, doc_id, version):
            stats["unchanged"] += 1
            continue
        markdown = f"# {p['title']}\n\n" + storage_to_markdown(((p.get("body") or {}).get("storage") or {}).get("value", ""))
        doc = {"id": doc_id, "name": p["title"], "title": p["title"], "collection": f"confluence:{space_key}",
               "version": version, "url": api.base + "/wiki" + ((p.get("_links") or {}).get("webui") or ""),
               "updated": (p.get("version") or {}).get("createdAt"), "source": "confluence",
               "acl": [f"confluence:{space_key}"], "page_id": p["id"],
               "source_updated_at": (p.get("version") or {}).get("createdAt") or p.get("createdAt"),
               "author": (p.get("version") or {}).get("authorId") or p.get("authorId"),
               "author_id": p.get("authorId"), "owner_id": p.get("ownerId")}   # Atlassian account ids for kg-people
        stats["sections"] += _write_document(w, doc, markdown)
        stats["documents"] += 1
    w.link("CHILD_OF", parents, replace_from=seen)
    removed = w._run("MATCH (d:Document {collection: $c}) WHERE NOT d.id IN $ids OPTIONAL MATCH (s:Section)-[:PART_OF]->(d) "
                     "DETACH DELETE s, d RETURN count(DISTINCT d) AS n", c=f"confluence:{space_key}", ids=seen).records[0]["n"]
    from .history import link_references
    return {**stats, "removed": removed, "references": link_references(w)}


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--path", type=Path, help="folder of documents")
    src.add_argument("--confluence", metavar="SPACE", help="Confluence space key")
    ap.add_argument("--name", help="collection name for --path (default: folder name)")
    ap.add_argument("--url-base", help="browsable URL of --path for deep links (default: file:// links)")
    ap.add_argument("--reconcile", action="store_true",
                    help="only remove deleted, archived and moved pages or files (no download of bodies)")
    ap.add_argument("--force", action="store_true", help="with --reconcile: allow removing more than half")
    args = ap.parse_args(argv)
    with Neo4jWriter(os.environ.get("NEO4J_URI", "bolt://localhost:7687"), os.environ.get("NEO4J_USER", "neo4j"),
                     os.environ["NEO4J_PASSWORD"]) as w:
        from .. import reconcile
        if args.path and args.reconcile:
            res = reconcile.folder(w, args.path, args.name or args.path.resolve().name, args.url_base, args.force)
        elif args.path:
            res = sync_folder(w, args.path, args.name or args.path.resolve().name, url_base=args.url_base)
        elif args.reconcile:
            res = reconcile.confluence(w, Atlassian(prefix="CONFLUENCE"), args.confluence, args.force)
        else:
            res = sync_confluence(w, Atlassian(prefix="CONFLUENCE"), args.confluence)
        print(json.dumps(res, indent=2))
