"""Turn names into edges: imports, calls, supertypes, BOM-to-Java mappings.

Order of trust: SCIP (method=scip, confidence 1.0) > Tree-sitter name match in the same file
(0.8) > in an imported file or the same Go package (0.7) > unique in the repo (0.4).
Measured against SCIP on a 420-file TypeScript/Python repo: 100% / 99% / 44% correct per tier.
Ambiguous names produce no edge; they are counted instead.
"""
from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath

from .files import SourceFile
from .model import CodeGraph
from .scip import ScipData
from .treesitter import FileSyntax, Symbol

CALLABLE = {"Function", "Method"}
TYPES = {"Class", "Interface", "Enum"}
FAMILY = {"java": "jvm", "kotlin": "jvm", "typescript": "js", "tsx": "js", "javascript": "js"}
JS_EXTS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", "/index.ts", "/index.tsx", "/index.js")
# Too generic to resolve by repo-wide uniqueness; usually library or builtin calls.
COMMON = set("""get set map filter push pop append add remove delete update keys values items join split
format toString equals hashCode length size then catch finally log print println printf apply call bind
error info debug warn next close open read write run start stop init reduce forEach find some every
includes indexOf slice splice replace trim parse stringify sort reverse concat entries emit send copy
clear has contains put of from list dict str int len range super String Error Object Array""".split())


def _family(lang: str) -> str:
    return FAMILY.get(lang, lang)


def _dotted_suffixes(path: str, exts: tuple[str, ...]) -> list[str]:
    p = PurePosixPath(path)
    if p.suffix not in exts:
        return []
    parts = list(p.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts)) if parts[i:]]


class Resolver:
    def __init__(self, graph: CodeGraph, files: list[SourceFile], syntax: dict[str, FileSyntax], repo: Path):
        self.g, self.syntax = graph, syntax
        self.lang = {f.path: f.language for f in files}
        self.paths = set(self.lang)
        self.dotted: dict[str, list[str]] = defaultdict(list)
        for f in files:
            exts = (".py",) if f.language == "python" else (".java", ".kt") if _family(f.language) == "jvm" else ()
            for d in _dotted_suffixes(f.path, exts):
                self.dotted[d].append(f.path)
        self.go_module = None
        for gomod in repo.glob("**/go.mod"):
            if "vendor" not in gomod.parts:
                m = re.search(r"^module\s+(\S+)", gomod.read_text(errors="replace"), re.M)
                if m:
                    self.go_module = (m.group(1), gomod.parent.relative_to(repo).as_posix())
                    break
        self.by_name: dict[tuple[str, str], list[tuple[str, Symbol]]] = defaultdict(list)
        self.spans: dict[str, list[tuple[int, int, str, str]]] = defaultdict(list)
        for path, s in syntax.items():
            for sym in s.symbols:
                self.by_name[(_family(self.lang[path]), sym.name)].append((path, sym))
                self.spans[path].append((sym.start_line, sym.end_line, self.sym_id(path, sym), sym.kind))
        self.imported: dict[str, set[str]] = defaultdict(set)

    def file_id(self, path: str) -> str:
        return f"{self.g.repo}:{path}"

    def sym_id(self, path: str, sym: Symbol) -> str:
        return f"{self.g.repo}:{path}#{sym.qualname}"

    # ---- imports -------------------------------------------------------------------------------
    def _import_targets(self, path: str, module: str) -> list[str]:
        lang = self.lang[path]
        if lang == "python":
            if module.startswith("."):
                dots = len(module) - len(module.lstrip("."))
                base = PurePosixPath(path).parent
                for _ in range(dots - 1):
                    base = base.parent
                rest = module.lstrip(".").replace(".", "/")
                stem = posixpath.join(str(base), rest) if rest else str(base)
                stem = stem.lstrip("./") if stem.startswith("./") else stem
                return [c for c in (f"{stem}.py", f"{stem}/__init__.py") if c in self.paths][:1]
            return self._unique(self.dotted.get(module, []), path)
        if _family(lang) == "jvm":
            return [] if module.endswith("*") else self._unique(self.dotted.get(module, []), path)
        if _family(lang) == "js":
            if module.startswith("."):
                stem = posixpath.normpath(posixpath.join(posixpath.dirname(path), module))
            elif module.startswith(("@/", "~/")):
                stem = "src/" + module[2:]
            else:
                return []
            return [stem + e for e in JS_EXTS if stem + e in self.paths][:1]
        if lang == "go" and self.go_module and module.startswith(self.go_module[0]):
            rel = module[len(self.go_module[0]):].strip("/")
            pkg_dir = posixpath.normpath(posixpath.join(self.go_module[1], rel)) if rel else self.go_module[1]
            return sorted(p for p in self.paths if posixpath.dirname(p) == pkg_dir.strip("./")
                          and p.endswith(".go") and not p.endswith("_test.go"))[:50]
        return []

    def _unique(self, candidates: list[str], path: str) -> list[str]:
        if len(candidates) <= 1:
            return candidates
        top = path.split("/")[0]
        same_root = [c for c in candidates if c.split("/")[0] == top]
        return same_root if len(same_root) == 1 else []

    def link_imports(self) -> None:
        for path, s in self.syntax.items():
            for module in dict.fromkeys(s.imports):
                targets = self._import_targets(path, module)
                for t in targets:
                    if t != path:
                        self.g.add_edge("IMPORTS", self.file_id(path), self.file_id(t), "parser")
                        self.imported[path].add(t)
                if not targets:
                    mid = f"{self.g.repo}:module:{self.lang[path]}:{module}"
                    self.g.add_node(mid, ("Code", "Module"), name=module, language=self.lang[path])
                    self.g.add_edge("IMPORTS", self.file_id(path), mid, "parser")

    # ---- SCIP ----------------------------------------------------------------------------------
    def enclosing(self, path: str, line: int) -> str | None:
        best = None
        for start, end, sid, kind in self.spans.get(path, ()):
            if start <= line <= end and (best is None or end - start < best[0]):
                best = (end - start, sid)
        return best[1] if best else None

    def link_scip(self, scip: ScipData) -> None:
        at_line: dict[tuple[str, int], list[tuple[str, Symbol]]] = defaultdict(list)
        for path, s in self.syntax.items():
            for sym in s.symbols:
                at_line[(path, sym.name_line)].append((path, sym))
        node_of: dict[str, str] = {}
        for scip_symbol, (path, line) in scip.definitions.items():
            here = at_line.get((path, line), ())
            match = next((self.sym_id(p, sym) for p, sym in here if sym.name in scip_symbol), None)
            if match is None and "::local " in scip_symbol and len(here) == 1:
                match = self.sym_id(*here[0])
            if match:
                node_of[scip_symbol] = match
                self.g.nodes[match].props.setdefault("scip_symbol", scip_symbol)
        self.g.counters["scip_definitions_matched"] = len(node_of)
        self.g.counters["scip_definitions_total"] = len(scip.definitions)
        for path, line, scip_symbol in scip.references:
            target = node_of.get(scip_symbol)
            if target is None:
                continue  # external library symbol
            src = self.enclosing(path, line) or self.file_id(path)
            if src == target:
                continue
            kind = self.g.nodes[target].labels[-1]
            etype = "CALLS" if kind in CALLABLE else "USES_TYPE" if kind in TYPES else "REFERENCES"
            self.g.add_edge(etype, src, target, "scip")
        for child, parent in scip.implementations:
            a, b = node_of.get(child), node_of.get(parent)
            if a and b and a != b:
                kind_a, kind_b = self.g.nodes[a].labels[-1], self.g.nodes[b].labels[-1]
                etype = "OVERRIDES" if kind_a in CALLABLE else "IMPLEMENTS" if kind_b == "Interface" else "EXTENDS"
                self.g.add_edge(etype, a, b, "scip")

    # ---- Tree-sitter fallback ------------------------------------------------------------------
    def _pick(self, name: str, path: str, kinds: set[str], allow_common: bool) -> tuple[str, float] | None:
        member = name.startswith(".")      # obj.name(): receiver is not this/self
        name = name.lstrip(".")
        if member and name in COMMON:
            return None
        fam = _family(self.lang[path])
        cands = [(p, s) for p, s in self.by_name.get((fam, name), ()) if s.kind in kinds]
        if not cands:
            return None
        same_file = [c for c in cands if c[0] == path]
        if len(same_file) == 1:
            return self.sym_id(*same_file[0]), 0.8
        nearby = self.imported[path] | ({p for p, _ in cands if posixpath.dirname(p) == posixpath.dirname(path)}
                                        if self.lang[path] == "go" else set())
        near = [c for c in cands if c[0] in nearby]
        if len(near) == 1:
            return self.sym_id(*near[0]), 0.7
        if len(cands) == 1 and (allow_common or name not in COMMON):
            return self.sym_id(*cands[0]), 0.4
        self.g.counters["names_ambiguous"] += 1
        return None

    def link_names(self, skip_paths: set[str]) -> None:
        for path, s in self.syntax.items():
            if path in skip_paths:
                continue
            for sym in s.symbols:
                src = self.sym_id(path, sym)
                for callee in dict.fromkeys(sym.calls):
                    hit = self._pick(callee, path, CALLABLE | TYPES, allow_common=False)
                    if hit and hit[0] != src:
                        kind = self.g.nodes[hit[0]].labels[-1]
                        self.g.add_edge("CALLS" if kind in CALLABLE else "USES_TYPE", src, hit[0],
                                        "treesitter", hit[1])
                for sup in dict.fromkeys(sym.supertypes):
                    hit = self._pick(sup, path, TYPES, allow_common=True)
                    if hit and hit[0] != src:
                        etype = "IMPLEMENTS" if self.g.nodes[hit[0]].labels[-1] == "Interface" else "EXTENDS"
                        self.g.add_edge(etype, src, hit[0], "treesitter", hit[1])

    # ---- ODM -----------------------------------------------------------------------------------
    def link_bom(self, bom_to_xom: dict[str, str]) -> None:
        jvm_types = {}
        for path, s in self.syntax.items():
            if _family(self.lang[path]) == "jvm":
                for sym in s.symbols:
                    if sym.kind in TYPES and sym.parent is None:
                        for d in _dotted_suffixes(path, (".java", ".kt")):
                            jvm_types.setdefault(d, self.sym_id(path, sym))
        for node in list(self.g.nodes.values()):
            if node.labels[-1] != "BomClass":
                continue
            bom = node.props["qualname"]
            xom = bom_to_xom.get(bom, bom)
            target = jvm_types.get(xom)
            if target:
                self.g.add_edge("MAPS_TO", node.id, target, "b2x" if bom in bom_to_xom else "name-match",
                                0.9 if bom in bom_to_xom else 0.7)
