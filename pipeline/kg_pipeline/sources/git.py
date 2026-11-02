"""Git mirrors in the source archive and throwaway checkouts for parsing."""
from __future__ import annotations

import base64
import contextlib
import os
import subprocess
import tempfile
from pathlib import Path


def repo_name(url: str) -> str:
    return Path(url.rstrip("/")).name.removesuffix(".git")


def _git(*args: str, cwd: Path | None = None) -> str:
    cmd = ["git"]
    token = os.environ.get("GIT_TOKEN")
    if token:
        # Pass the token per command; it is never stored in a URL or in the mirror's config.
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraHeader=Authorization: Basic {auth}"]
    return subprocess.run([*cmd, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def mirror(url: str, archive: Path) -> Path:
    """Create or update a bare mirror at <archive>/git/<name>.git and return its path."""
    path = archive / "git" / f"{repo_name(url)}.git"
    if path.exists():
        _git("fetch", "--prune", "--tags", cwd=path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--mirror", url, str(path))
    return path


def head(mirror_path: Path, branch: str) -> str:
    return _git("rev-parse", branch, cwd=mirror_path).strip()


@contextlib.contextmanager
def checkout(mirror_path: Path, ref: str):
    """Detached worktree of ref in a temporary folder; removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="kg-worktree-") as tmp:
        work = Path(tmp) / "src"
        _git("worktree", "add", "--force", "--detach", str(work), ref, cwd=mirror_path)
        try:
            yield work
        finally:
            _git("worktree", "remove", "--force", str(work), cwd=mirror_path)
            _git("worktree", "prune", cwd=mirror_path)
