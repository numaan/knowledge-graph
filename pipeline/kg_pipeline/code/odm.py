"""IBM ODM rule project artefacts: action rules, decision tables, rule flows, IRL, BOM, vocabulary, B2X.

Best effort: these parsers follow the Rule Designer file layouts and tolerate missing elements.
Validate against your own rule projects before you rely on the edges.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

from .files import SourceFile
from .model import CodeGraph

TEXT_LIMIT = 20_000


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _xml(text: str) -> ET.Element | None:
    try:
        return ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return None


def _child_text(root: ET.Element, name: str) -> str | None:
    for child in root:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return None


def _package_of(path: str) -> str | None:
    """rules/<a>/<b>/rule.brl -> a.b (Rule Designer keeps rule packages as folders under rules/)."""
    parts = PurePosixPath(path).parts
    if "rules" in parts:
        pkg = parts[parts.index("rules") + 1:-1]
        return ".".join(pkg) if pkg else None
    return None


def _rule_node(graph: CodeGraph, f: SourceFile, file_id: str, label: str, name: str, text: str, kind: str):
    node_id = f"{graph.repo}:odm:{f.path}#{name}"
    graph.add_node(node_id, ("Code", label), name=name, kind=kind, path=f.path, language="odm",
                   text=text[:TEXT_LIMIT])
    graph.add_edge("DEFINES", file_id, node_id, "parser")
    pkg = _package_of(f.path)
    if pkg:
        pkg_id = f"{graph.repo}:odm:pkg:{pkg}"
        graph.add_node(pkg_id, ("Code", "RulePackage"), name=pkg)
        graph.add_edge("IN_PACKAGE", node_id, pkg_id, "parser")
    return node_id


def _bom_classes(text: str) -> list[tuple[str, list[str], list[str]]]:
    """(qualified name, supertypes, members) for each class in a .bom file."""
    pkg = (re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M) or [None, None])[1]
    out = []
    for m in re.finditer(r"\b(?:class|interface)\s+(\w+)([^{]*)\{(.*?)^\s*\}", text, re.S | re.M):
        name, head, body = m.groups()
        supers = re.findall(r"(?:extends|implements)\s+([\w.,\s]+)", head)
        supers = [s.strip().split(".")[-1] for group in supers for s in group.split(",") if s.strip()]
        members = re.findall(r"^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?[\w.<>\[\]]+\s+(\w+)\s*[;(]",
                             body, re.M)
        out.append((f"{pkg}.{name}" if pkg else name, supers, members))
    return out


def build(graph: CodeGraph, files: list[SourceFile]) -> dict[str, str]:
    """Add ODM nodes and edges. Returns BOM class -> execution (XOM) class name from .b2x files."""
    terms: dict[str, set[str]] = {}       # BOM class -> verbalised labels from .voc
    bom_to_xom: dict[str, str] = {}
    rules: list[tuple[str, str]] = []     # (node id, rule text) for term matching

    for f in files:
        file_id = f"{graph.repo}:{f.path}"
        ext = PurePosixPath(f.path).suffix.lower()
        stem = PurePosixPath(f.path).stem

        if ext in {".brl", ".dta", ".rfl"}:
            root = _xml(f.text)
            if root is None:
                graph.counters["odm_xml_errors"] += 1
                continue
            name = _child_text(root, "name") or stem
            if ext == ".brl":
                text = _child_text(root, "definition") or ""
                rules.append((_rule_node(graph, f, file_id, "Rule", name, text, _local(root.tag)), text))
            elif ext == ".dta":
                cells = [e.text.strip() for e in root.iter() if _local(e.tag) in {"Text", "Expression"} and e.text]
                text = "\n".join(dict.fromkeys(cells))
                rules.append((_rule_node(graph, f, file_id, "DecisionTable", name, text, "DecisionTable"), text))
            else:
                flow_id = _rule_node(graph, f, file_id, "RuleFlow", name, "", "RuleFlow")
                for e in root.iter():
                    pkg = e.get("Name") if _local(e.tag) == "Package" else None
                    if pkg:
                        pkg_id = f"{graph.repo}:odm:pkg:{pkg}"
                        graph.add_node(pkg_id, ("Code", "RulePackage"), name=pkg)
                        graph.add_edge("RUNS", flow_id, pkg_id, "parser")

        elif ext == ".ilr":
            for m in re.finditer(r"\brule\s+([\w.]+)\s*\{", f.text):
                body = f.text[m.start():m.start() + TEXT_LIMIT]
                rules.append((_rule_node(graph, f, file_id, "Rule", m.group(1), body, "IRL"), body))

        elif ext == ".bom":
            for qualname, supers, members in _bom_classes(f.text):
                bom_id = f"{graph.repo}:odm:bom:{qualname}"
                graph.add_node(bom_id, ("Code", "BomClass"), name=qualname.split(".")[-1], qualname=qualname,
                               path=f.path, members=members)
                graph.add_edge("DEFINES", file_id, bom_id, "parser")
                for sup in supers:
                    graph.add_edge("EXTENDS", bom_id, f"{graph.repo}:odm:bom-name:{sup}", "parser", 0.5,
                                   unresolved=True)

        elif ext == ".voc":
            # loan.Borrower#concept.label = borrower ; loan.Borrower.age#phrase.navigation = {age} of {this}
            for m in re.finditer(r"^([\w.]+)#(concept\.label|phrase\.\w+)\s*=\s*(.+)$", f.text, re.M):
                key, kind, value = m.groups()
                owner = key if kind == "concept.label" else key.rsplit(".", 1)[0]
                label = re.sub(r"\s+", " ", re.sub(r"\{[^}]*\}", " ", value)).strip().lower()
                if len(label) > 2:
                    terms.setdefault(owner, set()).add(label)

        elif ext == ".b2x":
            root = _xml(f.text)
            for cls in (root.iter() if root is not None else []):
                if _local(cls.tag) == "class":
                    business, execution = _child_text(cls, "businessName"), _child_text(cls, "executionName")
                    if business and execution:
                        bom_to_xom[business] = execution

    # Rule -> BOM class when the rule text uses the class's vocabulary.
    for rule_id, text in rules:
        lowered = text.lower()
        for bom_class, labels in terms.items():
            if any(re.search(rf"\b{re.escape(label)}\b", lowered) for label in labels):
                graph.add_edge("USES_TERM", rule_id, f"{graph.repo}:odm:bom:{bom_class}", "odm-vocabulary", 0.6)

    # Drop placeholder edges whose BOM supertype was not found by simple name.
    by_name = {n.props["name"]: n.id for n in graph.nodes.values() if n.labels[-1] == "BomClass"}
    for key, edge in list(graph.edges.items()):
        if edge.props.get("unresolved"):
            del graph.edges[key]
            target = by_name.get(edge.dst.rsplit(":", 1)[-1])
            if target:
                graph.add_edge("EXTENDS", edge.src, target, "parser", 0.8)
    return bom_to_xom
