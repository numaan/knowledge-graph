from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Extension -> language. Languages in PARSED get symbol extraction; the rest become File nodes only.
LANGUAGES = {
    ".py": "python", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".cs": "csharp",
    # IBM ODM rule project artefacts
    ".brl": "odm", ".dta": "odm", ".rfl": "odm", ".ilr": "odm", ".bom": "odm", ".b2x": "odm", ".voc": "odm",
    # Unparsed but useful as File nodes
    ".sql": "sql", ".xml": "xml", ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml",
    ".properties": "properties", ".gradle": "gradle", ".md": "markdown", ".sh": "shell", ".tf": "terraform",
}
PARSED = {"python", "java", "kotlin", "typescript", "tsx", "javascript", "go", "csharp"}
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "out", ".git", ".venv", "venv",
             "__pycache__", ".next", ".gradle", "bin", "obj", "coverage"}
SKIP_SUFFIXES = (".min.js", ".bundle.js", ".map", ".lock", "-lock.json", ".d.ts")
MAX_BYTES = 1_000_000


@dataclass
class SourceFile:
    path: str            # repo-relative, forward slashes
    language: str
    text: str
    sha256: str


def list_paths(repo: Path) -> list[str]:
    """Tracked files when the repo is a git checkout, otherwise a directory walk."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], check=True,
                             capture_output=True).stdout.decode("utf-8", "replace")
        return [p for p in out.split("\0") if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths = []
        for root, dirs, names in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            paths += [Path(root, n).relative_to(repo).as_posix() for n in names]
        return paths


def read_sources(repo: Path, counters) -> list[SourceFile]:
    files = []
    for rel in sorted(list_paths(repo)):
        parts = rel.split("/")
        language = LANGUAGES.get(Path(rel).suffix.lower())
        if language is None:
            counters["files_skipped_unknown_type"] += 1
            continue
        if SKIP_DIRS.intersection(parts[:-1]) or rel.endswith(SKIP_SUFFIXES):
            counters["files_skipped_generated_or_vendor"] += 1
            continue
        full = repo / rel
        try:
            raw = full.read_bytes()
        except OSError:
            counters["files_skipped_unreadable"] += 1
            continue
        if len(raw) > MAX_BYTES or b"\0" in raw[:8192]:
            counters["files_skipped_large_or_binary"] += 1
            continue
        files.append(SourceFile(rel, language, raw.decode("utf-8", "replace"), hashlib.sha256(raw).hexdigest()))
    return files
