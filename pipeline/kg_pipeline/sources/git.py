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


def _git(*args: str, cwd: Path | None = None, token: str | None = None, env_fallback: bool = True,
         restricted: bool = False) -> str:
    """env_fallback: without a token, use GIT_TOKEN. Callers of a ui-origin source pass False so the platform token
    never goes to a host an admin chose.
    restricted: for ui-origin sources. Git does not follow redirects (the SSRF guard checked only the first host)
    and only http and https URLs work. Env-origin sources keep the plain behaviour (ssh and file URLs, redirects)."""
    cmd = ["git"]
    env = None
    if restricted:
        cmd += ["-c", "http.followRedirects=false"]
        env = {**os.environ, "GIT_ALLOW_PROTOCOL": "http:https", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    if not token and env_fallback:
        token = os.environ.get("GIT_TOKEN")
    if token:
        # Pass the token per command; it is never stored in a URL or in the mirror's config.
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraHeader=Authorization: Basic {auth}"]
    return subprocess.run([*cmd, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def mirror(url: str, archive: Path, token: str | None = None, env_fallback: bool = True, restricted: bool = False) -> Path:
    """Create or update a bare mirror at <archive>/git/<name>.git and return its path.

    token: the credential of the source; without it GIT_TOKEN from the environment applies when env_fallback is true."""
    path = archive / "git" / f"{repo_name(url)}.git"
    if path.exists():
        _git("fetch", "--prune", "--tags", cwd=path, token=token, env_fallback=env_fallback, restricted=restricted)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--mirror", url, str(path), token=token, env_fallback=env_fallback, restricted=restricted)
    return path


def head(mirror_path: Path, branch: str) -> str:
    return _git("rev-parse", branch, cwd=mirror_path, env_fallback=False).strip()


@contextlib.contextmanager
def checkout(mirror_path: Path, ref: str):
    """Detached worktree of ref in a temporary folder; removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="kg-worktree-") as tmp:
        work = Path(tmp) / "src"
        _git("worktree", "add", "--force", "--detach", str(work), ref, cwd=mirror_path, env_fallback=False)
        try:
            yield work
        finally:
            _git("worktree", "remove", "--force", str(work), cwd=mirror_path, env_fallback=False)
            _git("worktree", "prune", cwd=mirror_path, env_fallback=False)
