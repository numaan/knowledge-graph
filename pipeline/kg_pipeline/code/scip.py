"""Precise cross-file references from SCIP indexes.

run_indexers() runs the indexers that are installed for the languages it detects. load() reads any
.scip file and returns definitions and references keyed by repo-relative path and 1-based line.
"""
from __future__ import annotations

import logging
import posixpath
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import scip_pb2

log = logging.getLogger(__name__)

DEFINITION = 0x1

# (marker files, executable, arguments). {out} is the output file, {name} the project name.
INDEXERS = {
    "python": (("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"), "scip-python",
               ["index", ".", "--project-name", "{name}", "--output", "{out}"]),
    "typescript": (("tsconfig.json",), "scip-typescript", ["index", "--output", "{out}"]),
    "javascript": (("package.json",), "scip-typescript", ["index", "--infer-tsconfig", "--output", "{out}"]),
    "java": (("pom.xml", "build.gradle", "build.gradle.kts", "build.sbt"), "scip-java", ["index", "--output", "{out}"]),
    "go": (("go.mod",), "scip-go", ["--output", "{out}"]),
    "csharp": (("*.sln", "*.csproj"), "scip-dotnet", ["index", "--output", "{out}"]),
}


@dataclass
class ScipData:
    # symbol -> (path, line) of its definition inside this repo
    definitions: dict[str, tuple[str, int]] = field(default_factory=dict)
    # (path, line, symbol) for every non-definition occurrence
    references: list[tuple[str, int, str]] = field(default_factory=list)
    # (symbol, implemented or overridden symbol)
    implementations: list[tuple[str, str]] = field(default_factory=list)
    paths: set[str] = field(default_factory=set)
    tools: list[str] = field(default_factory=list)


def _project_roots(repo: Path, markers: tuple[str, ...], max_depth: int = 3) -> list[Path]:
    """Outermost folders that contain a marker file (handles monorepos such as frontend/ + backend/)."""
    from .files import SKIP_DIRS

    roots: list[Path] = []
    frontier = [repo]
    for _ in range(max_depth + 1):
        nxt = []
        for d in frontier:
            if any(next(d.glob(m), None) is not None for m in markers):
                roots.append(d)
                continue
            nxt += [c for c in d.iterdir() if c.is_dir() and c.name not in SKIP_DIRS and not c.name.startswith(".")]
        frontier = nxt
    return roots


def _folder_hash(root: Path, exe: str) -> str:
    """Hash of every source file in a project folder: an index is reused while this does not change."""
    import hashlib
    from .files import SKIP_DIRS

    h = hashlib.sha256(exe.encode())
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]) \
                and p.stat().st_size < 2_000_000:
            h.update(rel.as_posix().encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()[:24]


def run_indexers(repo: Path, out_dir: Path, name: str, timeout: int = 1800,
                 cache_dir: Path | None = None) -> list[tuple[Path, str]]:
    """Run installed indexers. Returns (index file, project folder relative to repo) pairs.

    With cache_dir, a folder whose files did not change reuses its previous index.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
    produced: list[tuple[Path, str]] = []
    done: set[tuple[str, Path]] = set()
    for lang, (markers, exe, args) in INDEXERS.items():
        roots = _project_roots(repo, markers)
        if not roots:
            continue
        binary = shutil.which(exe)
        if binary is None:
            log.warning("SCIP indexer %s not installed; %s falls back to Tree-sitter name matching", exe, lang)
            continue
        for root in roots:
            if (exe, root) in done:
                continue
            rel = root.relative_to(repo).as_posix()
            out = out_dir / f"{lang}-{len(produced)}.scip"
            cached = cache_dir / f"{name}-{lang}-{rel.replace('/', '_')}-{_folder_hash(root, exe)}.scip" if cache_dir else None
            if cached and cached.exists():
                log.info("reusing %s index of %s (no change)", lang, rel)
                produced.append((cached, "" if rel == "." else rel))
                done.add((exe, root))
                continue
            cmd = [binary, *[a.format(out=str(out), name=name) for a in args]]
            log.info("running %s in %s", " ".join(cmd), rel)
            try:
                subprocess.run(cmd, cwd=root, check=True, timeout=timeout, capture_output=True, text=True)
                if cached:
                    for old in cache_dir.glob(f"{name}-{lang}-{rel.replace('/', '_')}-*.scip"):
                        old.unlink()
                    shutil.copyfile(out, cached)
                produced.append((out, "" if rel == "." else rel))
                done.add((exe, root))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                # A failed build is normal (missing dependencies); keep the Tree-sitter fallback.
                log.warning("%s failed in %s: %s", exe, rel, (getattr(e, "stderr", "") or str(e))[-2000:])
    return produced


def _start_line(occ) -> int:
    which = occ.WhichOneof("typed_range")
    if which == "single_line_range":
        return occ.single_line_range.line + 1
    if which == "multi_line_range":
        return occ.multi_line_range.start_line + 1
    return occ.range[0] + 1


def load(indexes: list[tuple[Path, str]]) -> ScipData:
    """indexes: (index file, folder the indexer ran in, relative to the repo root)."""
    data = ScipData()
    for p, prefix in indexes:
        index = scip_pb2.Index()
        index.ParseFromString(p.read_bytes())
        data.tools.append(f"{index.metadata.tool_info.name} {index.metadata.tool_info.version}".strip())
        for doc in index.documents:
            path = posixpath.join(prefix, doc.relative_path.replace("\\", "/"))
            data.paths.add(path)
            for occ in doc.occurrences:
                if not occ.symbol:
                    continue
                # Local symbols (nested functions, closures) are only unique inside their document.
                symbol = f"{path}::{occ.symbol}" if occ.symbol.startswith("local ") else occ.symbol
                line = _start_line(occ)
                if occ.symbol_roles & DEFINITION:
                    data.definitions.setdefault(symbol, (path, line))
                else:
                    data.references.append((path, line, symbol))
            for info in doc.symbols:
                for rel in info.relationships:
                    if rel.is_implementation:
                        data.implementations.append((info.symbol, rel.symbol))
    return data
