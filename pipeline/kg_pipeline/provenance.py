"""Provenance of :Source nodes: a deep link and who changed the item when, in the source system.

Every source row carries url, source_updated_at and author; Neo4jWriter.upsert adds ingested_at.
"""
from __future__ import annotations

import re
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("url", "source_updated_at", "ingested_at", "author")
_SCP = re.compile(r"^(?:[\w.-]+@)?([\w.-]+):(?!//)(.+)$")      # git@github.com:org/repo.git


def web_remote(remote: str | None) -> str | None:
    """Browsable https URL of a git remote, without credentials: ssh and scp forms become https."""
    if not remote:
        return None
    remote = remote.strip()
    if m := _SCP.match(remote):
        host, path = m.groups()
    else:
        u = urllib.parse.urlsplit(remote)
        if u.scheme not in ("http", "https", "ssh", "git") or not u.hostname:
            return None
        host, path = u.hostname, u.path.lstrip("/")   # hostname drops user:token@ and the port
    return f"https://{host}/{path.removesuffix('/').removesuffix('.git')}"


def commit_url(web: str | None, sha: str) -> str | None:
    if not web:
        return None
    if "gitlab" in web:
        return f"{web}/-/commit/{sha}"
    if "bitbucket.org" in web:
        return f"{web}/commits/{sha}"
    return f"{web}/commit/{sha}"


def git_remote(repo: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    return web_remote(out)


def file_meta(path: Path) -> dict:
    """author and source_updated_at of a file: the last git commit when the file is in a repository, else mtime."""
    try:
        out = subprocess.run(["git", "-C", str(path.parent), "log", "-1", "--format=%an%x1f%aI", "--", path.name],
                             capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        out = ""
    if out:
        author, date = out.split("\x1f", 1)
        return {"author": author, "source_updated_at": date}
    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return {"author": None, "source_updated_at": mtime.isoformat()}


def file_url(path: Path, rel: str, base: str | None) -> str:
    """base + relative path when a browsable base is given (e.g. a repository tree URL), else a file:// URI."""
    if base:
        return base.rstrip("/") + "/" + urllib.parse.quote(rel)
    return path.resolve().as_uri()
