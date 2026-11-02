"""Syntax extraction with Tree-sitter: symbols, containment, imports, call sites, supertypes, chunks.

Names found here are unresolved strings. resolve.py turns them into edges.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from tree_sitter import Node as TSNode
from tree_sitter_language_pack import get_parser

IDENT = {"identifier", "simple_identifier", "type_identifier", "property_identifier",
         "field_identifier", "private_property_identifier", "constant"}
# Subtrees that never hold a callee or supertype name.
NOISE = {"type_arguments", "type_parameters", "arguments", "argument_list", "value_arguments",
         "keyword_argument", "call_suffix", "string", "comment"}


@dataclass(frozen=True)
class LangSpec:
    grammar: str
    symbols: dict[str, str]                 # node type -> kind
    calls: dict[str, str | None]            # node type -> field with the callee (None: first named child)
    imports: tuple[str, ...]
    import_pattern: str                     # regex on the import node text; group 1..n are module names
    heritage: tuple[str, ...] = ()          # child node types of a class that list supertypes
    heritage_fields: tuple[str, ...] = ()   # same, by field name


_JS_SYMBOLS = {"class_declaration": "Class", "class": "Class", "function_declaration": "Function",
               "generator_function_declaration": "Function", "method_definition": "Method"}
_JS_CALLS = {"call_expression": "function", "new_expression": "constructor"}
_JS_IMPORT = r"""(?:from\s+|import\s+|require\()\s*['"]([^'"]+)['"]"""

SPECS: dict[str, LangSpec] = {
    "python": LangSpec(
        "python", {"class_definition": "Class", "function_definition": "Function"},
        {"call": "function"}, ("import_statement", "import_from_statement"),
        r"from\s+(\.*[\w.]*)\s+import|^import\s+([\w.]+)", heritage_fields=("superclasses",)),
    "java": LangSpec(
        "java", {"class_declaration": "Class", "record_declaration": "Class", "enum_declaration": "Enum",
                 "interface_declaration": "Interface", "annotation_type_declaration": "Interface",
                 "method_declaration": "Method", "constructor_declaration": "Method"},
        {"method_invocation": "name", "object_creation_expression": "type"}, ("import_declaration",),
        r"import\s+(?:static\s+)?([\w.]+(?:\.\*)?)", heritage=("superclass", "super_interfaces", "extends_interfaces")),
    "kotlin": LangSpec(
        "kotlin", {"class_declaration": "Class", "object_declaration": "Class", "function_declaration": "Function"},
        {"call_expression": None}, ("import_header",), r"import\s+([\w.]+(?:\.\*)?)",
        heritage=("delegation_specifier",)),
    "typescript": LangSpec(
        "typescript", {**_JS_SYMBOLS, "abstract_class_declaration": "Class", "interface_declaration": "Interface",
                       "enum_declaration": "Enum"},
        _JS_CALLS, ("import_statement",), _JS_IMPORT, heritage=("class_heritage", "extends_type_clause")),
    "javascript": LangSpec("javascript", _JS_SYMBOLS, _JS_CALLS, ("import_statement",), _JS_IMPORT,
                           heritage=("class_heritage",)),
    "go": LangSpec(
        "go", {"function_declaration": "Function", "method_declaration": "Method", "type_spec": "Class"},
        {"call_expression": "function"}, ("import_spec",), r'"([^"]+)"'),
    "csharp": LangSpec(
        "csharp", {"class_declaration": "Class", "struct_declaration": "Class", "record_declaration": "Class",
                   "interface_declaration": "Interface", "enum_declaration": "Enum",
                   "method_declaration": "Method", "constructor_declaration": "Method",
                   "local_function_statement": "Function"},
        {"invocation_expression": "function", "object_creation_expression": "type"}, ("using_directive",),
        r"using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)\s*;", heritage=("base_list",)),
}
SPECS["tsx"] = replace(SPECS["typescript"], grammar="tsx")

CHUNK_LINES = 120
MIN_FILE_CODE_LINES = 3
FILLER = re.compile(r"^\s*(import\b|from\s+\S+\s+import\b|package\b|using\b|#include\b|//|#|/?\*|\*/|[})\];,]+$)")
SELF_NAMES = {"this", "self", "base", "super"}


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str
    start_line: int      # 1-based
    end_line: int
    name_line: int       # line of the name token (SCIP definitions point here)
    signature: str
    parent: str | None   # qualname of the enclosing symbol
    calls: list[str] = field(default_factory=list)
    supertypes: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)  # @Entity, [HttpGet("x")], @router.get("/x")


@dataclass
class FileSyntax:
    symbols: list[Symbol]
    imports: list[str]
    has_errors: bool
    top_level_calls: list[str]


def _text(node: TSNode) -> str:
    return node.text.decode("utf-8", "replace")


def _last_ident(node: TSNode | None) -> str | None:
    """Rightmost identifier of a name expression: a.b.c() -> c, new pkg.X<T>() -> X."""
    if node is None or node.type in NOISE:
        return None
    if node.type in IDENT:
        return _text(node)
    for child in reversed(node.named_children):
        name = _last_ident(child)
        if name:
            return name
    return None


def _all_idents(node: TSNode) -> list[str]:
    """Supertype names in a heritage clause; skips generic arguments and keyword arguments."""
    if node.type in NOISE:
        return []
    if node.type in IDENT:
        return [_text(node)]
    if node.type in {"attribute", "member_expression", "scoped_type_identifier", "qualified_name",
                     "scoped_identifier", "navigation_expression"}:
        name = _last_ident(node)
        return [name] if name else []
    return [n for child in node.named_children for n in _all_idents(child)]


ANNOTATION = {"annotation", "marker_annotation", "attribute", "decorator"}


def _annotations(node: TSNode) -> list[str]:
    """Annotations, attributes and decorators that belong to a declaration node."""
    found: list[TSNode] = []
    for child in node.named_children:
        if child.type in {"modifiers", "attribute_list"}:
            found += [c for c in child.named_children if c.type in ANNOTATION]
        elif child.type == "decorator":
            found.append(child)
    # Python wraps decorated definitions; TypeScript puts decorators before the declaration.
    if node.parent is not None and node.parent.type == "decorated_definition":
        found += [c for c in node.parent.named_children if c.type == "decorator"]
    prev = node.prev_named_sibling
    while prev is not None and prev.type == "decorator":
        found.append(prev)
        prev = prev.prev_named_sibling
    if node.parent is not None and node.parent.type == "export_statement":
        found += [c for c in node.parent.named_children if c.type == "decorator"]
    return list(dict.fromkeys(" ".join(_text(a).lstrip("@[").rstrip("]").split())[:200] for a in found))


def _symbol_name(node: TSNode, lang: str) -> TSNode | None:
    named = node.child_by_field_name("name")
    if named is not None:
        return named
    return next((c for c in node.named_children if c.type in IDENT), None)


def _kind(node: TSNode, lang: str, spec: LangSpec) -> str | None:
    kind = spec.symbols.get(node.type)
    if lang == "kotlin" and kind == "Class" and any(c.type == "interface" for c in node.children):
        return "Interface"
    if lang == "go" and node.type == "type_spec":
        body = node.child_by_field_name("type")
        if body is None or body.type not in {"struct_type", "interface_type"}:
            return None  # type aliases and named primitives are not symbols
        return "Interface" if body.type == "interface_type" else "Class"
    # const f = () => {} / const f = function () {}
    if kind is None and node.type == "variable_declarator" and lang in {"typescript", "tsx", "javascript"}:
        value = node.child_by_field_name("value")
        if value is not None and value.type in {"arrow_function", "function_expression", "function"}:
            return "Function"
    return kind


def _go_receiver(node: TSNode) -> str | None:
    receiver = node.child_by_field_name("receiver")
    return _last_ident(receiver) if receiver is not None else None


def parse(text: str, lang: str) -> FileSyntax:
    spec = SPECS[lang]
    source = text.encode("utf-8")
    tree = get_parser(spec.grammar).parse(source)
    lines = text.splitlines() or [""]
    symbols: list[Symbol] = []
    imports: list[str] = []
    top_calls: list[str] = []
    seen: dict[str, int] = {}

    def visit(node: TSNode, owner: Symbol | None) -> None:
        if node.type in spec.imports:
            for match in re.finditer(spec.import_pattern, _text(node), re.M):
                imports.extend(g for g in match.groups() if g)
            return

        if node.type in spec.calls:
            field_name = spec.calls[node.type]
            callee = node.child_by_field_name(field_name) if field_name else (node.named_children or [None])[0]
            name = _last_ident(callee)
            if name:
                # Receiver of obj.method(): Java keeps it in an "object" field, other grammars
                # nest it as the first child of the callee expression.
                receiver = node.child_by_field_name("object")
                if receiver is None and callee is not None and callee.named_child_count > 1:
                    receiver = callee.named_children[0]
                if receiver is not None and _text(receiver) not in SELF_NAMES:
                    name = "." + name  # resolved with care, see resolve._pick
                (owner.calls if owner else top_calls).append(name)

        kind = _kind(node, lang, spec)
        if kind == "Function" and owner is not None and owner.kind in {"Class", "Interface", "Enum"}:
            kind = "Method"
        name_node = _symbol_name(node, lang) if kind else None
        if kind and name_node is not None:
            name = _text(name_node)
            receiver = _go_receiver(node) if node.type == "method_declaration" and lang == "go" else None
            prefix = receiver or (owner.qualname if owner else None)
            qualname = f"{prefix}.{name}" if prefix else name
            # Overloads and redefinitions share a qualname; keep ids unique and stable in file order.
            seen[qualname] = seen.get(qualname, 0) + 1
            if seen[qualname] > 1:
                qualname = f"{qualname}~{seen[qualname]}"
            # Signature: the source line with the name (annotations may come first).
            signature = lines[name_node.start_point[0]].strip()[:300]
            sym = Symbol(name, qualname, kind, node.start_point[0] + 1, node.end_point[0] + 1,
                         name_node.start_point[0] + 1, signature, owner.qualname if owner else None,
                         annotations=_annotations(node))
            for i, child in enumerate(node.children):
                field_of = node.field_name_for_child(i)
                if child.type in spec.heritage or (field_of and field_of in spec.heritage_fields):
                    sym.supertypes += [n for c in child.named_children for n in _all_idents(c)]
            symbols.append(sym)
            owner = sym

        for child in node.children:
            visit(child, owner)

    visit(tree.root_node, None)
    return FileSyntax(symbols, imports, tree.root_node.has_error, top_calls)


def chunks(text: str, syntax: FileSyntax) -> list[tuple[str | None, int, int]]:
    """(owner qualname or None for file level, start_line, end_line) for AST-aligned chunks.

    One chunk per function or method, split when longer than CHUNK_LINES. Classes without
    methods get their own chunks. File-level code outside every symbol (module constants,
    `export const store = create(...)`, scripts) gets file chunks, so no code is left out.
    """
    out: list[tuple[str | None, int, int]] = []
    callables = [s for s in syntax.symbols if s.kind in {"Function", "Method"}]
    owners_with_methods = {s.parent for s in callables}
    targets = callables + [s for s in syntax.symbols
                           if s.kind not in {"Function", "Method"} and s.qualname not in owners_with_methods]
    for sym in targets:
        for start in range(sym.start_line, sym.end_line + 1, CHUNK_LINES):
            out.append((sym.qualname, start, min(start + CHUNK_LINES - 1, sym.end_line)))
    lines = text.split("\n")
    covered = [False] * (len(lines) + 2)
    for sym in syntax.symbols:
        for n in range(sym.start_line, sym.end_line + 1):
            covered[n] = True
    run: list[int] = []
    for n in range(1, len(lines) + 2):
        if n <= len(lines) and not covered[n]:
            run.append(n)
            continue
        # skip runs of blank lines, imports and comments only
        if sum(1 for i in run if lines[i - 1].strip() and not FILLER.match(lines[i - 1])) >= MIN_FILE_CODE_LINES:
            for start in range(run[0], run[-1] + 1, CHUNK_LINES):
                out.append((None, start, min(start + CHUNK_LINES - 1, run[-1])))
        run = []
    return out
