"""Contracts layer of one repository: API specs, database schemas, HTTP and topic facts.

Called by build_graph after the code layer is resolved. Edges between repositories are made later by
links.py, because they need every repository in the graph.
"""
from __future__ import annotations

import json

import yaml

from . import dbschema, httpcalls, openapi
from .files import SourceFile
from .model import CodeGraph
from .treesitter import FileSyntax

LINKS_CONFIG = ("kg-links.yaml", "kg-links.yml")


def build(g: CodeGraph, files: list[SourceFile], syntax: dict[str, FileSyntax]) -> None:
    openapi.build(g, files)
    httpcalls.annotate(g, files, syntax)
    dbschema.build(g, files, syntax)
    cfg = next((f for f in files if f.path in LINKS_CONFIG), None)
    if cfg is not None:
        try:
            g.nodes[g.repo].props["links_config"] = json.dumps(yaml.safe_load(cfg.text) or {}, sort_keys=True)
        except yaml.YAMLError:
            g.counters["links_config_invalid"] += 1
