from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Node:
    id: str
    labels: tuple[str, ...]
    props: dict = field(default_factory=dict)


@dataclass
class Edge:
    type: str
    src: str
    dst: str
    props: dict = field(default_factory=dict)


class CodeGraph:
    """In-memory graph for one repository snapshot. Edges are unique per (type, src, dst)."""

    def __init__(self, repo: str):
        self.repo = repo
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple[str, str, str], Edge] = {}
        self.counters: Counter = Counter()

    def add_node(self, id: str, labels: tuple[str, ...], **props) -> Node:
        node = self.nodes.get(id)
        if node is None:
            node = self.nodes[id] = Node(id, labels, {"repo": self.repo})
        node.props.update({k: v for k, v in props.items() if v is not None})
        return node

    def add_edge(self, type: str, src: str, dst: str, method: str, confidence: float = 1.0, **props) -> None:
        key = (type, src, dst)
        current = self.edges.get(key)
        # Keep the strongest evidence when two extractors find the same edge.
        if current is None or confidence > current.props["confidence"]:
            self.edges[key] = Edge(type, src, dst, {"method": method, "confidence": confidence, **props})

    def stats(self) -> dict:
        nodes = Counter(n.labels[-1] for n in self.nodes.values())
        edges = Counter(f"{e.type}/{e.props['method']}" for e in self.edges.values())
        return {"nodes": dict(nodes), "edges": dict(edges), "counters": dict(self.counters)}
